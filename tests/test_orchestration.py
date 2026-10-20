"""Offline tests for the mock game, the seeded orchestrator and the replay tool. No network."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from infra import LLMClient, MockBackend, LLMCallError, read_events, validate_events, AGENT_IDS
from infra.mockgame import run_game as mock_run_game, CHECKPOINTS_AFTER_INTERROGATION
from infra.orchestrate import derive_seed, plan_games, Run, execute, summarise, CONDITIONS
from infra.pricing import pricing_for
from infra.replay import ReplayBackend, ReplayMismatch, render_transcript, write_game_artifacts

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "cases" / "pilot.json"
CONFIG = {"model_id": "gemini-3.1-flash-lite", "decoding_settings": {"temperature": 0.7}, "k": 5, "turn_budget": 20,
          "runs_per_condition": 2, "budget_usd": 1.0, "prompt_versions": {"mock": "v0"}}


def make_llm(game=None):
    return LLMClient(MockBackend(), "mock-model", {"temperature": 0.7}, log=None,
                     pricing=pricing_for("gemini-3.1-flash-lite"), sleep=lambda seconds: None)


def strip_timestamps(events):
    return [{key: value for key, value in event.items() if key != "ts"} for event in events]


class PlanTests(unittest.TestCase):
    def test_seed_derivation_is_stable_and_distinct(self):
        self.assertEqual(derive_seed(1, "raw", 3), derive_seed(1, "raw", 3))
        self.assertNotEqual(derive_seed(1, "raw", 3), derive_seed(1, "raw", 4))
        self.assertNotEqual(derive_seed(1, "raw", 3), derive_seed(1, "ledger", 3))
        self.assertNotEqual(derive_seed(1, "raw", 3), derive_seed(2, "raw", 3))

    def test_plan_covers_every_pair_in_a_seeded_shuffled_order(self):
        plan = plan_games(7, CONDITIONS, 4)
        self.assertEqual(len(plan), 12)
        self.assertEqual({(g["condition"], g["repeat"]) for g in plan}, {(c, r) for c in CONDITIONS for r in range(4)})
        self.assertEqual([g["order"] for g in plan], list(range(12)))
        self.assertEqual(plan, plan_games(7, CONDITIONS, 4))
        self.assertNotEqual([g["plan_index"] for g in plan], [g["plan_index"] for g in plan_games(8, CONDITIONS, 4)])
        self.assertNotEqual([g["condition"] for g in plan], sorted(g["condition"] for g in plan))


class MockGameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="mockgame_", dir=ROOT)
        cls.dir = cls.temporary.name

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_every_condition_writes_a_valid_log_with_harishs_return_shape(self):
        for condition in CONDITIONS:
            with self.subTest(condition=condition):
                result = mock_run_game(CASE, CONFIG, 11, condition, make_llm(), log_dir=self.dir)
                self.assertEqual(set(result), {"game_id", "log_path", "outcome", "first_accusations_correct"})
                events = read_events(result["log_path"])
                self.assertEqual(validate_events(events), {"passed": True, "errors": []})
                kinds = [e["kind"] for e in events]
                self.assertEqual((kinds[0], kinds[-1]), ("game_start", "game_end"))
                self.assertIn("accusation", kinds)
                self.assertTrue(all(e["agent_id"] in AGENT_IDS for e in events if e["kind"] == "llm_call"))
                self.assertEqual(events[0]["condition"], condition)
                if condition == "ledger":
                    self.assertIn("extraction", kinds)
                else:
                    self.assertNotIn("extraction", kinds)

    def test_same_seed_same_trace_and_failures_happen(self):
        first = strip_timestamps(read_events(mock_run_game(CASE, CONFIG, 3, "raw", make_llm(), self.dir)["log_path"]))
        second = strip_timestamps(read_events(mock_run_game(CASE, CONFIG, 3, "raw", make_llm(), self.dir)["log_path"]))
        self.assertEqual(first, second)
        outcomes = {}
        for seed in range(60):
            result = mock_run_game(CASE, CONFIG, seed, "ledger", make_llm(), log_dir=self.dir)
            outcomes.setdefault(result["outcome"], seed)
            events = read_events(result["log_path"])
            self.assertTrue(validate_events(events)["passed"], seed)
            for event in events:
                if event["kind"] == "checkpoint_start":
                    self.assertIn(event["after_interrogation"], CHECKPOINTS_AFTER_INTERROGATION)
                if event["kind"] == "conversation" and event["visibility"] == "private":
                    self.assertIsNone(event["exchange_id"])
            if result["outcome"] == "failed_run":
                self.assertIn("failure", [e["kind"] for e in events])
                self.assertEqual(events[-1]["outcome"], "failed_run")
                self.assertIsNone(events[-1]["first_accusation_correct"])
        self.assertIn("failed_run", outcomes)
        self.assertTrue({"solved_first", "solved_retry", "unsolved"} & set(outcomes))


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="run_", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.run_dir = Path(self.temporary.name) / "run"

    def create(self, **overrides):
        kwargs = dict(run_id="test", master_seed=5, case_path=CASE, config=CONFIG, conditions=("none", "ledger"),
                      runs_per_condition=2)
        kwargs.update(overrides)
        return Run.create(self.run_dir, **kwargs)

    def test_parallel_execution_artifacts_resume_and_summary(self):
        run = self.create()
        summary = execute(run, mock_run_game, make_llm, workers=2)
        self.assertEqual((summary["played_now"], summary["remaining"], summary["games"]), (4, 0, 4))
        rows = run.results()
        self.assertEqual({row["status"] for row in rows}, {"ok"})
        self.assertTrue(all(row["validation_passed"] for row in rows))
        self.assertEqual(sorted(row["plan_index"] for row in rows), [0, 1, 2, 3])
        for row in rows:
            self.assertTrue(Path(row["transcript_path"]).exists())
            exported = json.loads(Path(row["exchanges_path"]).read_text(encoding="utf-8"))
            self.assertEqual(list(exported), ["exchanges", "reported_pairs"])
            self.assertGreater(row["cost_usd"], 0)
            self.assertIsNotNone(row["first_accusation_correct"] if row["outcome"] != "failed_run" else True)
        self.assertEqual(set(summary["by_condition"]), {"none", "ledger"})
        self.assertGreater(summary["cost_usd"], 0)
        again = execute(run, mock_run_game, make_llm, workers=2)
        self.assertEqual((again["played_now"], again["remaining"]), (0, 0))
        self.assertEqual(len(run.results()), 4)
        reopened = Run.open(self.run_dir)
        self.assertEqual(reopened.manifest["plan"], run.manifest["plan"])
        self.assertEqual(reopened.manifest["case_sha256"], run.manifest["case_sha256"])
        with self.assertRaises(FileExistsError):
            self.create()

    def test_crash_in_run_game_is_a_recorded_row(self):
        run = self.create(runs_per_condition=1)

        def broken(case_path, config, seed, condition, llm, log_dir="runs"):
            raise RuntimeError("controller bug")

        summary = execute(run, broken, make_llm, workers=1)
        rows = run.results()
        self.assertEqual([row["status"] for row in rows], ["crashed", "crashed"])
        self.assertIn("RuntimeError: controller bug", rows[0]["error"])
        self.assertEqual(sum(b["crashed"] for b in summary["by_condition"].values()), 2)
        self.assertEqual(summary["remaining"], 0)
        self.assertEqual(sum(b["validation_failures"] for b in summary["by_condition"].values()), 0)

    def test_cost_ceiling_stops_scheduling_new_games(self):
        run = self.create(max_cost_usd=1e-9)
        summary = execute(run, mock_run_game, make_llm, workers=1)
        self.assertEqual(summary["played_now"], 1)
        self.assertTrue(summary["stopped_for_budget"])
        self.assertEqual(summary["remaining"], 3)
        later = execute(run, mock_run_game, make_llm, workers=1, max_cost_usd=10.0)
        self.assertEqual((later["played_now"], later["remaining"]), (3, 0))

    def test_limit_plays_a_prefix_of_the_plan(self):
        run = self.create()
        summary = execute(run, mock_run_game, make_llm, workers=3, limit=1)
        self.assertEqual((summary["played_now"], summary["remaining"]), (1, 3))
        self.assertEqual(run.results()[0]["order"], 0)


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="replay_", dir=ROOT)
        cls.dir = cls.temporary.name
        cls.result = mock_run_game(CASE, CONFIG, 11, "ledger", make_llm(), log_dir=cls.dir)
        cls.events = read_events(cls.result["log_path"])

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_replay_backend_reproduces_every_recorded_call(self):
        backend = ReplayBackend(self.events)
        llm = LLMClient(backend, "mock-model", {"temperature": 0.7}, sleep=lambda seconds: None)
        records = [e for e in self.events if e["kind"] == "llm_call"]
        for record in records:
            arguments = dict(role=record["role"], agent_id=record["agent_id"], purpose=record["purpose"],
                             phase=record["phase"], messages=record["messages"],
                             response_schema=record["response_schema"], game_id=record["game_id"])
            if record["status"] == "ok":
                replayed = llm.call(**arguments)
                self.assertEqual(replayed["text"], record["text"])
                self.assertEqual(replayed["parsed"], record["parsed"])
                self.assertEqual(replayed["usage"], record["attempts"][-1]["usage"])
            else:
                with self.assertRaises(LLMCallError):
                    llm.call(**arguments)
        self.assertTrue(backend.exhausted)

    def test_replay_detects_prompt_changes_and_exhaustion(self):
        backend = ReplayBackend(self.events)
        with self.assertRaises(ReplayMismatch):
            backend.generate(model_id="m", messages=[{"role": "user", "content": "something else"}], decoding={})
        lenient = ReplayBackend(self.events, strict=False)
        out = lenient.generate(model_id="m", messages=[{"role": "user", "content": "something else"}], decoding={})
        self.assertEqual(out["text"], [e for e in self.events if e["kind"] == "llm_call"][0]["text"])
        empty = ReplayBackend([])
        with self.assertRaises(ReplayMismatch):
            empty.generate(model_id="m", messages=[], decoding={})

    def test_transcript_and_artifacts(self):
        transcript = render_transcript(self.events)
        first = next(e for e in self.events if e["kind"] == "interrogation")
        self.assertIn("## Turn 1 (e1)", transcript)
        self.assertIn(first["question"], transcript)
        self.assertIn(first["response"], transcript)
        self.assertIn("## Accusation 1", transcript)
        self.assertIn("## End:", transcript)
        private = [e for e in self.events if e["kind"] == "conversation" and e["visibility"] == "private"]
        if private:
            self.assertIn(private[0]["message"], transcript)
            self.assertNotIn(private[0]["message"], render_transcript(self.events, include_private=False))
        artifacts = write_game_artifacts(self.result["log_path"])
        self.assertTrue(Path(artifacts["transcript"]).read_text(encoding="utf-8").startswith("# Game "))
        exported = json.loads(Path(artifacts["exchanges"]).read_text(encoding="utf-8"))
        visible = [e for e in self.events if e["kind"] == "interrogation"
                   or (e["kind"] == "conversation" and e["visibility"] == "detective")]
        self.assertEqual(len([x for x in exported["exchanges"] if x["kind"] in ("interrogation", "detective_conversation")]),
                         len(visible))


class CliTests(unittest.TestCase):
    def test_run_summary_and_replay_commands(self):
        with tempfile.TemporaryDirectory(prefix="cli_", dir=ROOT) as temporary:
            run_dir = Path(temporary) / "demo"
            result = subprocess.run([sys.executable, "-m", "infra", "run", "--config",
                                     "configs/experiment_config.example.json", "--case", "cases/pilot.json",
                                     "--run-dir", str(run_dir), "--run-id", "demo", "--seed", "5",
                                     "--games-per-condition", "1", "--workers", "2"],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["games"], 3)
            self.assertEqual(set(summary["by_condition"]), set(CONDITIONS))
            result = subprocess.run([sys.executable, "-m", "infra", "summary", str(run_dir)], cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["remaining"], 0)
            log = Run.open(run_dir).results()[0]["log_path"]
            result = subprocess.run([sys.executable, "-m", "infra", "replay", log, "--check"], cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("## Accusation", result.stdout)
            result = subprocess.run([sys.executable, "-m", "infra", "run", "--run-dir", str(run_dir), "--dry-run"],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["remaining"], [])


if __name__ == "__main__":
    unittest.main()

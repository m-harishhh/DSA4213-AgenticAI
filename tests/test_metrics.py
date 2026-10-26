"""Offline tests for infra.metrics and infra.figures on mock runs and hand-built records."""

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from casework import load_case
from infra import LLMClient, MockBackend, read_events
from infra.metrics import (aggregate, bootstrap_ci, collect_records, game_record, markdown_report, mean,
                           percentile, pooled_ratio, proportion, write_outputs, METRICS, crashed_record)
from infra.mockgame import run_game as mock_run_game
from infra.orchestrate import Run, execute
from infra.pricing import pricing_for

ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "cases" / "pilot.json"
CONFIG = {"model_id": "gemini-3.1-flash-lite", "decoding_settings": {"temperature": 0.7}, "k": 5, "turn_budget": 20,
          "runs_per_condition": 3, "budget_usd": 1.0, "prompt_versions": {"mock": "v0"}}
HAVE_MPL = importlib.util.find_spec("matplotlib") is not None


def make_llm(game=None):
    return LLMClient(MockBackend(), "mock-model", {"temperature": 0.7}, log=None,
                     pricing=pricing_for("gemini-3.1-flash-lite"), sleep=lambda seconds: None)


def fake(condition, correct=False, solved_at=None, status="completed", valid=1, wrong=0, cost=0.01, turns=10):
    return {"game_id": f"g_{condition}_{id(object())}", "condition": condition, "seed": 1, "model_id": "m", "k": 5,
            "status": status, "outcome": "solved_first" if solved_at == 1 else "unsolved",
            "failure_stage": None, "failure_reason": None, "first_accusation_made": valid > 0,
            "first_accusation_correct": correct,
            "component_correct": {"culprit": correct, "weapon": True, "location": correct} if valid else None,
            "attempts_used": valid, "solved_at": solved_at, "solved_within_2": solved_at is not None and solved_at <= 2,
            "solved_within_3": solved_at is not None, "valid_accusations": valid, "wrong_valid_accusations": wrong,
            "interrogations_before_first_accusation": turns, "interrogations_used": turns, "exchanges_total": turns,
            "checkpoints_reached": 1, "invitations_sent": 2, "invitations_accepted": 1, "invitations_rejected": 1,
            "detective_conversations": 1, "private_conversations": 0, "contradiction_pairs_reported": 1, "llm_calls": 30,
            "total_tokens": 1000, "cost_usd": cost, "tokens_through_first_accusation": 900,
            "cost_through_first_accusation": cost * 0.9, "cost_accusation_retries": cost * 0.1,
            "cost_post_game_analysis": 0.0, "transport_retries": 0, "format_repairs": 0, "controller_repairs": 0,
            "truncated_calls": 0, "reasoning_tokens": 0, "duration_s": 60.0, "online_evidence_coverage": 0.5,
            "online_essential_coverage": 0.5, "validation_passed": True}


class StatisticsTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(mean([1, None, 3]), 2)
        self.assertIsNone(mean([None]))
        self.assertEqual(proportion([True, False, False, True]), 0.5)
        self.assertIsNone(proportion([]))
        self.assertEqual(pooled_ratio([1, 2], [2, 2]), 0.75)
        self.assertIsNone(pooled_ratio([0], [0]))
        self.assertEqual(percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(percentile([5], 0.975), 5)

    def test_bootstrap_is_seeded_and_brackets_the_point(self):
        import random
        records = [fake("raw", correct=i < 3) for i in range(10)]
        statistic = METRICS["first_accusation_accuracy"][0]
        low, high = bootstrap_ci(records, statistic, 500, random.Random(1))
        self.assertLessEqual(low, statistic(records))
        self.assertGreaterEqual(high, statistic(records))
        self.assertEqual(bootstrap_ci(records, statistic, 500, random.Random(1)), [low, high])
        self.assertEqual(bootstrap_ci([], statistic, 10, random.Random(1)), [None, None])

    def test_aggregate_exact_values_and_denominators(self):
        records = ([fake("raw", correct=True, solved_at=1)] * 2 + [fake("raw", solved_at=3, valid=3, wrong=2)]
                   + [fake("raw", status="failed_run", valid=0)]
                   + [fake("ledger", correct=True, solved_at=1)] * 3 + [fake("ledger", status="crashed", valid=0)])
        summary = aggregate(records, replicates=50, seed=3)
        raw, ledger = summary["conditions"]["raw"], summary["conditions"]["ledger"]
        self.assertEqual((raw["games"], raw["completed"], raw["failed_runs"], raw["crashed"]), (4, 3, 1, 0))
        self.assertEqual(raw["metrics"]["first_accusation_accuracy"]["value"], 0.5)
        self.assertAlmostEqual(raw["metrics"]["first_accusation_accuracy_completed"]["value"], 2 / 3)
        self.assertEqual(raw["metrics"]["solve_rate_within_3"]["value"], 0.75)
        self.assertEqual(raw["metrics"]["solve_rate_within_2"]["value"], 0.5)
        self.assertEqual(raw["metrics"]["wrong_valid_accusation_rate"]["value"], 2 / 5)
        self.assertAlmostEqual(raw["metrics"]["attempts_used_mean_solved"]["value"], (1 + 1 + 3) / 3)
        self.assertEqual(raw["metrics"]["failed_run_rate"]["value"], 0.25)
        self.assertEqual(ledger["metrics"]["first_accusation_accuracy"]["value"], 0.75)
        self.assertEqual(ledger["metrics"]["crash_rate"]["value"], 0.25)
        self.assertEqual(ledger["metrics"]["weapon_accuracy"]["value"], 0.75)
        diff = summary["differences"]["ledger_minus_raw"]["first_accusation_accuracy"]
        self.assertAlmostEqual(diff["value"], 0.25)
        self.assertLessEqual(diff["ci95"][0], 0.25)
        self.assertGreaterEqual(diff["ci95"][1], 0.25)
        self.assertEqual(list(summary["conditions"]), ["raw", "ledger"])
        self.assertIn("ledger_minus_raw", summary["differences"])
        self.assertNotIn("ledger_minus_none", summary["differences"])

    def test_markdown_report_lists_every_condition(self):
        summary = aggregate([fake("none"), fake("raw", correct=True, solved_at=1), fake("ledger")], replicates=20)
        report = markdown_report(summary, title="T")
        self.assertTrue(report.startswith("# T"))
        for name in ("first_accusation_accuracy", "cost_usd_mean", "ledger_minus_raw", "raw_minus_none"):
            self.assertIn(name, report)


class RecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="metrics_", dir=ROOT)
        cls.dir = Path(cls.temporary.name)
        cls.case = load_case(CASE_PATH)
        cls.game_run = Run.create(cls.dir / "run", run_id="m", master_seed=11, case_path=CASE_PATH, config=CONFIG,
                             conditions=("none", "raw", "ledger"), runs_per_condition=3)
        execute(cls.game_run, mock_run_game, make_llm, workers=3)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_records_match_the_game_end_events(self):
        records = collect_records(self.game_run.dir, self.case)
        self.assertEqual(len(records), 9)
        by_id = {r["game_id"]: r for r in records}
        for row in self.game_run.results():
            events = read_events(row["log_path"])
            end = events[-1]
            record = by_id[row["game_id"]]
            self.assertEqual(record["outcome"], end["outcome"])
            self.assertEqual(record["attempts_used"], end["attempts_used"])
            self.assertEqual(record["interrogations_used"], end["interrogations_used"])
            self.assertEqual(record["cost_usd"], end["cost"]["cost_usd"])
            self.assertEqual(record["total_tokens"], end["cost"]["total_tokens"])
            self.assertEqual(record["status"], "failed_run" if end["outcome"] == "failed_run" else "completed")
            self.assertEqual(record["first_accusation_correct"], bool(end["first_accusation_correct"]))
            if record["status"] == "completed":
                self.assertIsNotNone(record["online_evidence_coverage"])
                self.assertGreaterEqual(record["interrogations_before_first_accusation"], 0)
                self.assertEqual(record["solved_within_3"], end["outcome"] in ("solved_first", "solved_retry"))
            self.assertTrue(record["validation_passed"])
            self.assertGreater(record["cost_through_first_accusation"], 0)

    def test_records_without_results_file_and_crashed_rows(self):
        plain = self.dir / "plain"
        plain.mkdir()
        for row in self.game_run.results()[:2]:
            (plain / Path(row["log_path"]).name).write_bytes(Path(row["log_path"]).read_bytes())
        self.assertEqual(len(collect_records(plain)), 2)
        crashed = crashed_record({"game_id": "x", "condition": "raw", "seed": 9, "error": "RuntimeError: boom"})
        self.assertEqual((crashed["status"], crashed["first_accusation_correct"], crashed["cost_usd"]),
                         ("crashed", False, None))
        self.assertEqual(set(crashed), set(collect_records(self.game_run.dir)[0]))

    def test_outputs_and_figures(self):
        records = collect_records(self.game_run.dir, self.case)
        summary = aggregate(records, replicates=100, seed=0)
        out = self.dir / "analysis"
        paths = write_outputs(summary, records, out, title="Mock")
        self.assertTrue(all(Path(p).exists() for p in paths.values()))
        with Path(paths["per_game_csv"]).open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 9)
        self.assertEqual(set(json.loads(Path(paths["metrics_json"]).read_text())["conditions"]), {"none", "raw", "ledger"})
        if HAVE_MPL:
            from infra.figures import make_figures
            written = make_figures(summary, out / "figures")
            self.assertEqual(len(written), 10)
            self.assertTrue(all(Path(p).stat().st_size > 1000 for p in written))

    def test_cli(self):
        result = subprocess.run([sys.executable, "-m", "infra", "metrics", str(self.game_run.dir), "--bootstrap", "50",
                                 "--case", str(CASE_PATH)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("first_accusation_accuracy", result.stdout)
        self.assertTrue((self.game_run.dir / "analysis" / "metrics.json").exists())


if __name__ == "__main__":
    unittest.main()

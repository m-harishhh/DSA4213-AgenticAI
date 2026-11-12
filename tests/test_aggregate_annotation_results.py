"""Tests for qa/aggregate_annotation_results.py: the repeated_question_rate denominator
fix and the separate source_dir for gameplay results.jsonl vs. annotation output.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qa.aggregate_annotation_results import aggregate, load_results, per_game_behaviour_stats  # noqa: E402


def _empty_exchange_entry(**overrides):
    entry = {
        "repeated_question": {"is_repeat": False, "repeat_kind": None, "matches_exchange_id": None},
        "deception": {"contains_false_statement": False, "authorised": None, "claim_id_if_known": None},
        "withholding": {"explicit_refusal": False, "partial_withholding": False},
        "challenge": {"is_challenge": False, "evidence_based": None, "cited_claim_ids": []},
        "corroboration": {"is_request": False, "target_subject": None, "target_time": None},
        "unsupported_additions": [], "confidence": "high",
        "lead_follow_up": {"newly_disclosed_lead_ids": [], "is_follow_up_to_lead_id": None, "follow_up_relevance": None},
    }
    entry.update(overrides)
    return entry


def _behaviour(exchange_behaviour, undiscovered_leads=None):
    return {
        "exchange_behaviour": exchange_behaviour,
        "missed_required_disclosures": [],
        "undiscovered_leads": undiscovered_leads if undiscovered_leads is not None else [],
        "voluntary_interaction": {"checkpoints_reached": 0, "coordination_instances": []},
    }


class PerGameStatsTests(unittest.TestCase):
    def test_undiscovered_leads_reported_distinctly_from_missed_required_disclosures(self):
        behaviour = _behaviour({"e1": _empty_exchange_entry()}, undiscovered_leads=[{"lead_id": "x", "owner": "Y"}])
        stats = per_game_behaviour_stats(behaviour)
        self.assertEqual(stats["undiscovered_leads"], 1)
        self.assertEqual(stats["missed_required_disclosures"], 0)

    def test_undiscovered_leads_defaults_to_zero_for_legacy_behaviour_without_the_key(self):
        # Pre-existing main_01 outputs were written before undiscovered_leads existed
        # in the storage schema -- aggregation over them must not crash.
        behaviour = _behaviour({"e1": _empty_exchange_entry()})
        del behaviour["undiscovered_leads"]
        stats = per_game_behaviour_stats(behaviour)
        self.assertEqual(stats["undiscovered_leads"], 0)


class RepeatedQuestionRateDenominatorTests(unittest.TestCase):
    def setUp(self):
        self.gameplay_dir = tempfile.TemporaryDirectory()
        self.ann_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.gameplay_dir.cleanup)
        self.addCleanup(self.ann_dir.cleanup)
        self.gameplay_path = Path(self.gameplay_dir.name)
        self.ann_path = Path(self.ann_dir.name)

    def _write_game(self, game_id, interrogations_used, exchange_behaviour):
        row = {"game_id": game_id, "condition": "none", "outcome": "solved_first",
              "attempts_used": 1, "interrogations_used": interrogations_used,
              "exchanges_total": len(exchange_behaviour), "cost_usd": 0.01}
        with (self.gameplay_path / "results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        (self.gameplay_path / f"{game_id}.jsonl").write_text(
            json.dumps({"kind": "game_start", "condition": "none", "k": 8}) + "\n")
        (self.ann_path / f"{game_id}.behaviour.json").write_text(json.dumps(_behaviour(exchange_behaviour)))

    def test_rate_per_interrogation_differs_from_rate_per_exchange_when_voluntary_disclosures_present(self):
        # 1 repeat out of 2 interrogation-equivalent exchanges (exchanges_labelled=2),
        # but interrogations_used=1 (the other exchange was a voluntary disclosure,
        # which structurally can never be a repeated question) -- the two denominators
        # must disagree, and the _per_interrogation one must use interrogations_used.
        exchange_behaviour = {
            "e1": _empty_exchange_entry(repeated_question={"is_repeat": True, "repeat_kind": "unnecessary",
                                                            "matches_exchange_id": None}),
            "e2": _empty_exchange_entry(),
        }
        self._write_game("g1", interrogations_used=1, exchange_behaviour=exchange_behaviour)

        report = aggregate(self.ann_path, source_dir=self.gameplay_path)
        game = report["per_game"][0]
        self.assertEqual(game["repeated_question_rate"], 0.5)  # 1 / exchanges_labelled(2)
        self.assertEqual(game["repeated_question_rate_per_interrogation"], 1.0)  # 1 / interrogations_used(1)

    def test_aggregate_reads_results_jsonl_from_separate_source_dir(self):
        self._write_game("g1", interrogations_used=2, exchange_behaviour={"e1": _empty_exchange_entry()})
        report = aggregate(self.ann_path, source_dir=self.gameplay_path)
        self.assertEqual(len(report["per_game"]), 1)
        self.assertEqual(report["missing_behaviour"], [])

    def test_load_results_raises_clearly_when_missing(self):
        # Regression test for the exact bug reported: a missing results.jsonl must not
        # be silently treated as "zero games" -- it's a required input.
        with tempfile.TemporaryDirectory() as empty_dir:
            with self.assertRaises(FileNotFoundError) as ctx:
                load_results(empty_dir)
            self.assertIn("results.jsonl", str(ctx.exception))

    def test_aggregate_raises_clearly_when_results_jsonl_is_missing(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            with self.assertRaises(FileNotFoundError):
                aggregate(self.ann_path, source_dir=empty_dir)


if __name__ == "__main__":
    unittest.main()

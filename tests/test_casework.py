"""Offline acceptance tests for Josh's handoff; python -m unittest discover -s tests -v."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from casework.generate import make_case
from casework.model import public_view, initial_state, claims_for_solver, suspect_view
from casework.solver import solve, triple_set
from casework.validation import validate_case
from casework.certificate import certify
from casework.disclosure import (allowed_content, record_delivery, share_information,
                                communication_options, branch_prompt)
from casework.diagnostics import (eligible_exchanges, evidence_diagnostics,
                                 contradiction_diagnostics)

ROOT = Path(__file__).resolve().parents[1]


class CaseTests(unittest.TestCase):
    def setUp(self):
        self.case = make_case("pilot")

    def assertInvalid(self, case, fragment):
        report = validate_case(case)
        self.assertFalse(report["passed"], report)
        self.assertIn(fragment, " ".join(report["errors"]))

    def test_both_cases_admitted_and_all_branches_budget_feasible(self):
        for kind in ("pilot", "evaluation"):
            with self.subTest(kind=kind):
                report = validate_case(make_case(kind))
                self.assertTrue(report["passed"], report["errors"])
                self.assertEqual(report["certificate"]["successful_terminal_branches"], 108)
                self.assertEqual(report["certificate"]["worst_case_turns"], 14)

    def test_saved_cases_equal_reproducible_authored_templates(self):
        for kind in ("pilot", "evaluation"):
            saved = json.loads((ROOT / "cases" / f"{kind}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, make_case(kind))

    def test_pilot_and_evaluation_are_different(self):
        other = make_case("evaluation")
        self.assertNotEqual(self.case["solution"], other["solution"])
        self.assertNotEqual(self.case["events"], other["events"])
        self.assertIn("candidate", other["status"])

    def test_inconsistent_ground_truth_rejected(self):
        self.case["facts"]["fact_badge_trace"]["value"] = "square"
        self.assertInvalid(self.case, "Fact inconsistent")

    def test_wrong_role_counts_rejected(self):
        self.case["suspects"]["Blair"]["role"] = "truthful"
        self.assertInvalid(self.case, "counts 1/2/2")

    def test_knowledge_boundary_rejected(self):
        self.case["suspects"]["Blair"]["known_fact_ids"].remove("fact_badge_trace")
        self.assertInvalid(self.case, "Knowledge-boundary")

    def test_initial_knowledge_requires_observation_event(self):
        self.case["events"] = [e for e in self.case["events"] if e["id"] != "observe_Blair_fact_badge_trace"]
        self.assertInvalid(self.case, "observation events")

    def test_single_innocent_cannot_start_with_solution(self):
        person = "Ellis"
        for fid in self.case["gold"]["essential_trace_fact_ids"]:
            if fid not in self.case["suspects"][person]["known_fact_ids"]:
                self.case["suspects"][person]["known_fact_ids"].append(fid)
                self.case["events"].append({"id": "test_" + fid, "subject": person,
                    "action": "observe", "time": "21:10", "fact_id": fid})
        self.assertInvalid(self.case, "One innocent initially knows")

    def test_counter_evidence_required(self):
        self.case["gold"]["counter_evidence"]["claim_cover_Blair"] = []
        self.assertInvalid(self.case, "No counter-evidence")

    def test_lead_must_be_discoverable(self):
        self.case["suspects"]["Blair"]["lead_ids"].remove("badge_trace")
        self.assertInvalid(self.case, "Undiscoverable lead")

    def test_false_claim_cannot_be_relabelled_truth(self):
        claim = self.case["claims"]["claim_cover_Blair"]
        claim["kind"] = "truthful"
        claim["fact_id"] = "fact_position_Blair_20:30"
        self.assertInvalid(self.case, "Truth claim/fact mismatch")

    def test_missing_gold_pair_rejected(self):
        self.case["gold"]["contradiction_pairs"].pop()
        self.assertInvalid(self.case, "Gold contradiction pairs")

    def test_empty_coverage_denominator_rejected(self):
        self.case["gold"]["evidence_ids"] = []
        self.assertInvalid(self.case, "Coverage gold")

    def test_malformed_case_is_a_failed_report(self):
        del self.case["solution"]
        self.assertInvalid(self.case, "Malformed")

    def test_short_budget_fails_this_strategy_not_all_possible_policies(self):
        report = certify(self.case, 13)
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed_terminal_branches"], 108)
        self.assertIn("Strategy exceeds", report["failures"][0]["reason"])

    def test_public_field_leak_rejected(self):
        self.case["public"]["solution"] = self.case["solution"]
        self.assertInvalid(self.case, "allowlist")


class SolverTests(unittest.TestCase):
    def setUp(self):
        self.case = make_case("pilot")
        self.public = public_view(self.case)
        self.clues = [lead["claim_id"] for lead in self.case["leads"].values()]

    def test_empty_evidence_has_125_answers_and_six_role_assignments_each(self):
        result = solve(self.public, [])
        self.assertEqual(result["candidate_count"], 125)
        self.assertTrue(all(len(c["role_assignments"]) == 6 for c in result["candidates"]))

    def test_all_discoverable_evidence_uniquely_solves(self):
        result = solve(self.public, claims_for_solver(self.case, self.clues))
        self.assertEqual(triple_set(result), {("Alex", "Knife", "Library")})

    def test_private_solution_and_roles_are_not_solver_inputs(self):
        observations = claims_for_solver(self.case, self.clues)
        before = solve(public_view(self.case), observations)
        self.case["solution"] = {"culprit": "Ellis", "weapon": "Rope", "location": "Kitchen"}
        self.case["suspects"]["Alex"]["role"] = "truthful"
        self.assertEqual(solve(public_view(self.case), observations), before)
        with self.assertRaises(ValueError):
            solve(self.case, observations)

    def test_innocent_lie_does_not_eliminate_correct_solution(self):
        observations = claims_for_solver(self.case, self.clues + ["claim_cover_Blair"])
        result = solve(self.public, observations)
        self.assertEqual(triple_set(result), {("Alex", "Knife", "Library")})
        self.assertTrue(all("Blair" in roles["self_protective"] for roles in result["candidates"][0]["role_assignments"]))

    def test_detected_lie_does_not_identify_culprit(self):
        result = solve(self.public, claims_for_solver(self.case, ["claim_cover_Blair", "claim_witness_Blair"]))
        self.assertGreater(len({c["culprit"] for c in result["candidates"]}), 1)

    def test_four_proven_own_alibi_liars_exceed_role_permissions(self):
        observations = []
        for person in self.public["suspects"][:4]:
            observations.extend([
                {"id": person+"a", "speaker": person, "key": f"position:{person}:20:30", "value": "Library"},
                {"id": person+"b", "speaker": "Ellis", "key": f"position:{person}:20:30", "value": "Study"}])
        self.assertEqual(solve(self.public, observations)["candidate_count"], 0)

    def test_different_times_do_not_conflict(self):
        claims = [{"id": "a", "speaker": "Ellis", "key": "position:Alex:20:30", "value": "Study"},
                  {"id": "b", "speaker": "Ellis", "key": "position:Alex:21:00", "value": "Library"}]
        self.assertGreater(solve(self.public, claims)["candidate_count"], 0)

    def test_non_alibi_contradiction_has_no_compliant_world(self):
        claims = claims_for_solver(self.case, ["claim_badge_trace"])
        claims.append({"id": "unauthorised", "speaker": "Alex", "key": "attacker:badge", "value": "square"})
        self.assertEqual(solve(self.public, claims)["candidate_count"], 0)

    def test_unknown_proposition_is_not_silently_discarded(self):
        with self.assertRaises(ValueError):
            solve(self.public, [{"id": "a", "speaker": "Alex", "key": "unsupported", "value": "x"}])


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.case = make_case("pilot")

    def test_truthful_innocents_cannot_choose_false_branch(self):
        for person in ("Drew", "Ellis"):
            branches = allowed_content(self.case, person, {"kind": "overview"}, initial_state(self.case, person))
            self.assertTrue(all(self.case["claims"][cid]["kind"] == "truthful"
                                for b in branches for cid in b["claim_ids"]))

    def test_self_protective_innocent_can_choose_truth_withhold_or_lie(self):
        branches = allowed_content(self.case, "Blair", {"kind": "whereabouts", "time": "20:30"}, initial_state(self.case, "Blair"))
        self.assertEqual({b["branch_id"] for b in branches}, {"truthful_answer", "withhold_private_detail", "approved_cover"})

    def test_every_overview_branch_exposes_leads(self):
        for person in self.case["public"]["suspects"]:
            branches = allowed_content(self.case, person, {"kind": "overview"}, initial_state(self.case, person))
            self.assertTrue(all(b["lead_ids"] == self.case["suspects"][person]["lead_ids"] for b in branches))

    def test_intended_content_does_not_update_state(self):
        state = initial_state(self.case, "Blair")
        allowed_content(self.case, "Blair", {"kind": "overview"}, state)
        self.assertEqual(state["delivered_claim_ids"], [])
        result = record_delivery(self.case, "Blair", state)
        self.assertEqual(result["disclosed_leads"], [])

    def test_retraction_requires_relevant_evidence_and_prevents_repeating_cover(self):
        state = record_delivery(self.case, "Alex", initial_state(self.case, "Alex"), ["claim_cover_Alex"])
        intent = {"kind": "challenge", "claim_id": "claim_cover_Alex"}
        self.assertEqual(allowed_content(self.case, "Alex", intent, state)[0]["branch_id"], "maintain_until_relevant_challenge")
        branch = allowed_content(self.case, "Alex", intent, state, ["claim_witness_Alex"])[0]
        self.assertEqual(branch["branch_id"], "retract_account_not_confess")
        state = record_delivery(self.case, "Alex", state, branch["claim_ids"], actual_retraction_ids=branch["retract_claim_ids"])
        future = allowed_content(self.case, "Alex", {"kind": "overview"}, state)
        self.assertFalse(any("claim_cover_Alex" in b["claim_ids"] for b in future))

    def test_sharing_is_attributed_and_does_not_copy_private_facts(self):
        state = initial_state(self.case, "Ellis")
        after = share_information(self.case, state, ["claim_cover_Blair"])
        self.assertEqual(after["known_fact_ids"], state["known_fact_ids"])
        self.assertEqual(after["received_claim_ids"], ["claim_cover_Blair"])
        self.assertEqual(state["received_claim_ids"], [])
        self.assertTrue(any(x["kind"] == "attributed_relay" and "Blair told me" in x["text"]
                            for x in communication_options(self.case, "Ellis", after)))

    def test_coordination_never_removes_required_original_clues(self):
        for person in self.case["public"]["suspects"]:
            state = share_information(self.case, initial_state(self.case, person), list(self.case["claims"]))
            for lid in self.case["suspects"][person]["lead_ids"]:
                b = allowed_content(self.case, person, {"kind": "ask_lead", "topic": lid}, state)
                self.assertEqual(len(b), 1)
                self.assertEqual(b[0]["claim_ids"], [self.case["leads"][lid]["claim_id"]])

    def test_private_views_are_copies_and_prompts_have_no_internal_ids(self):
        p = public_view(self.case)
        p["suspects"].clear()
        self.assertEqual(len(self.case["public"]["suspects"]), 5)
        b = allowed_content(self.case, "Blair", {"kind": "overview"}, initial_state(self.case, "Blair"))[0]
        rendered = json.dumps(branch_prompt(self.case, b))
        self.assertNotIn("claim_self_", rendered)
        self.assertNotIn("fact_", rendered)
        self.assertNotIn("solution", suspect_view(self.case, "Blair", initial_state(self.case, "Blair")))


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.case = make_case("pilot")
        self.exchanges = [
            {"exchange_id": "e1", "kind": "interrogation", "claim_ids": ["claim_cover_Blair"]},
            {"exchange_id": "private", "kind": "suspect_conversation", "claim_ids": ["claim_witness_Blair"]},
            {"exchange_id": "invite", "kind": "invitation", "claim_ids": []},
            {"exchange_id": "e2", "kind": "interrogation", "claim_ids": ["claim_witness_Blair"]},
            {"exchange_id": "e3", "kind": "detective_conversation", "claim_ids": ["claim_badge_trace"]}]

    def test_free_conversation_displaces_oldest_and_private_does_not_enter(self):
        self.assertEqual([e["exchange_id"] for e in eligible_exchanges(self.exchanges, "raw", 2)], ["e2", "e3"])
        self.assertEqual([e["exchange_id"] for e in eligible_exchanges(self.exchanges, "none", 10)], ["e3"])
        self.assertEqual(eligible_exchanges(self.exchanges, "raw", 2), eligible_exchanges(self.exchanges, "ledger", 2))

    def test_coverage_deduplicates_and_tracks_false_separately(self):
        self.exchanges.append({"exchange_id": "e4", "kind": "interrogation", "claim_ids": ["claim_badge_trace"]})
        r = evidence_diagnostics(self.case, self.exchanges)
        self.assertEqual((r["coverage_numerator"], r["coverage_denominator"]), (2, 9))
        self.assertEqual(r["approved_false_claims_disclosed"], ["claim_cover_Blair"])

    def test_private_only_counterclaim_is_not_observable(self):
        r = contradiction_diagnostics(self.case, self.exchanges[:2], [], "raw", 5)
        self.assertEqual(r["observable_gold_pair_count"], 0)
        self.assertIsNone(r["recall"])

    def test_ledger_omission_does_not_shrink_denominator(self):
        r = contradiction_diagnostics(self.case, self.exchanges, [], "ledger", 3)
        self.assertEqual(r["observable_gold_pair_count"], 1)
        self.assertEqual(r["recall"], 0)
        self.assertIsNone(r["precision"])

    def test_supported_pair_requires_correct_exchange_citations(self):
        report = {"left": {"exchange_id": "e1", "claim_id": "claim_cover_Blair"},
                  "right": {"exchange_id": "e2", "claim_id": "claim_witness_Blair"}}
        r = contradiction_diagnostics(self.case, self.exchanges, [report], "raw", 3)
        self.assertEqual((r["precision"], r["recall"]), (1, 1))
        report["right"]["exchange_id"] = "private"
        r = contradiction_diagnostics(self.case, self.exchanges, [report], "raw", 3)
        self.assertEqual(r["precision"], 0)
        self.assertEqual(r["unsupported_pair_count"], 1)

    def test_duplicate_exchange_ids_rejected(self):
        with self.assertRaises(ValueError):
            eligible_exchanges(self.exchanges + [self.exchanges[0]], "raw", 5)

    def test_annotation_example_spans_and_scores(self):
        fixture = json.loads((ROOT / "examples" / "annotation_example.json").read_text(encoding="utf-8"))
        for exchange in fixture["exchanges"]:
            for annotation in exchange["annotations"]:
                span = annotation["source_span"]
                self.assertEqual(exchange["delivered_response"][span["start"]:span["end"]], span["text"])
        r = contradiction_diagnostics(self.case, fixture["exchanges"], fixture["reported_pairs"], "ledger", 3)
        self.assertEqual((r["precision"], r["recall"]), (1, 1))


class CommandTests(unittest.TestCase):
    def test_cli_validation_and_manifest(self):
        # Keep test files inside the checkout, including on restricted Windows hosts.
        with tempfile.TemporaryDirectory(prefix="casework_test_", dir=ROOT) as temporary:
            self.assertTrue(Path(temporary).resolve().is_relative_to(ROOT.resolve()))
            out = Path(temporary) / "report.json"
            result = subprocess.run([sys.executable, "-m", "casework", "validate", "cases/pilot.json", "--output", str(out)],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(out.read_text())["passed"])
            config = Path(temporary) / "config.json"
            config.write_text(json.dumps({"model_id": "test-only-no-calls", "decoding_settings": {"temperature": 0.5},
                "k": 5, "turn_budget": 20, "runs_per_condition": 10, "budget_usd": 20,
                "annotation_sample_size": 12, "prompt_versions": {"detective": "test-v1"}}))
            result = subprocess.run([sys.executable, "-m", "casework", "manifest", "cases/evaluation.json",
                "--config", str(config), "--output", str(out)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(out.read_text())
            self.assertEqual(manifest["config"]["budget_usd"], 20)
            self.assertEqual(len(manifest["case_sha256"]), 64)

    def test_incomplete_experiment_config_cannot_be_frozen(self):
        result = subprocess.run([sys.executable, "-m", "casework", "manifest", "cases/evaluation.json",
            "--config", "configs/experiment_config.example.json"], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Fill every final setting", result.stderr)


if __name__ == "__main__":
    unittest.main()

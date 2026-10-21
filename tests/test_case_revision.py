"""Regression checks for referrals, alibi necessity, and eight-exchange feasibility."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from casework.generate import make_case
from casework.model import initial_state, public_view, claims_for_solver, suspect_view
from casework.disclosure import allowed_content, record_delivery, branch_prompt
from casework.solver import solve, triple_set
from casework.validation import validate_case
from casework.certificate import certify

ROOT = Path(__file__).resolve().parents[1]


class RevisedCaseTests(unittest.TestCase):
    def test_independent_alibi_is_necessary_and_sufficient_with_traces(self):
        for kind in ("pilot", "evaluation"):
            c = make_case(kind)
            traces = [lead["claim_id"] for lead in c["leads"].values()
                      if c["claims"][lead["claim_id"]]["key"].split(":")[0] in ("attacker", "weapon", "scene")]
            alibi = next(lead["claim_id"] for lid, lead in c["leads"].items() if lid.startswith("alibi_"))
            public = public_view(c)
            result = solve(public, claims_for_solver(c, traces))
            self.assertEqual(result["candidate_count"], 2)
            self.assertTrue(all(len(x["role_assignments"]) == 6 for x in result["candidates"]))
            without_alibi = [l["claim_id"] for l in c["leads"].values() if l["claim_id"] != alibi]
            self.assertEqual(solve(public, claims_for_solver(c, without_alibi))["candidate_count"], 2)
            self_claim = f"claim_self_{c['solution']['culprit']}_21:00"
            self.assertEqual(solve(public, claims_for_solver(c, without_alibi + [self_claim]))["candidate_count"], 2)
            solved = solve(public, claims_for_solver(c, traces + [alibi]))
            self.assertEqual(triple_set(solved), {tuple(c["solution"][k] for k in ("culprit", "weapon", "location"))})

    def test_referral_records_cue_without_transferring_evidence(self):
        c = make_case()
        referrals = [(lid, l) for lid, l in c["leads"].items() if l["owner"] not in l["cue_speakers"]]
        self.assertEqual(len(referrals), 2)
        for lid, lead in referrals:
            giver = lead["cue_speakers"][0]
            state = initial_state(c, giver)
            branches = allowed_content(c, giver, {"kind": "overview"}, state)
            self.assertTrue(all(lid in b["lead_ids"] for b in branches))
            after = record_delivery(c, giver, state, actual_lead_ids=[lid])
            self.assertIn(lid, after["disclosed_leads"])
            self.assertEqual(after["known_fact_ids"], state["known_fact_ids"])
            self.assertEqual(after["delivered_claim_ids"], [])
            reply = allowed_content(c, giver, {"kind": "ask_lead", "topic": lid}, after)[0]
            self.assertEqual(reply["claim_ids"], [])
            self.assertIn(lead["owner"], branch_prompt(c, reply)["required_leads"][0])
            # Directly asking the holder works even without having found the referral.
            reply = allowed_content(c, lead["owner"], {"kind": "ask_lead", "topic": lid}, initial_state(c, lead["owner"]))[0]
            self.assertEqual(reply["claim_ids"], [lead["claim_id"]])
            outsider = next(p for p in c["suspects"] if p not in lead["cue_speakers"])
            with self.assertRaises(ValueError):
                record_delivery(c, outsider, initial_state(c, outsider), actual_lead_ids=[lid])

    def test_validator_rejects_unknown_or_missing_referral_knowledge(self):
        for mutation in (lambda c: c["leads"]["scene_floor"].update(cue_speakers=["Nobody"]),
                         lambda c: c["suspects"]["Drew"]["lead_ids"].remove("scene_floor")):
            c = make_case(); mutation(c)
            self.assertFalse(validate_case(c)["passed"])

    def test_final_window_includes_checkpoint_displacement_and_still_solves(self):
        for kind in ("pilot", "evaluation"):
            c = make_case(kind); report = certify(c)
            self.assertTrue(report["passed"], report["failures"])
            self.assertEqual(report["memory_k"], 8)
            self.assertLessEqual(report["worst_case_turns"], 20)
            self.assertEqual(report["successful_terminal_branches"], 108)
            # Independently reconstruct the last eight source interactions.
            exchanges = []
            for step in report["example_path"]:
                if step["intent"]["kind"] == "ask_lead":
                    self.assertEqual(step["suspect"], c["leads"][step["intent"]["topic"]]["owner"])
                exchanges.append(step["claim_ids"])
                if step["turn"] in (5, 10, 15):
                    exchanges.append([])
            claims = [cid for ids in exchanges[-8:] for cid in ids]
            self.assertEqual(solve(public_view(c), claims_for_solver(c, claims))["candidate_count"], 1)
            self.assertTrue(any(e["kind"] == "detective_conversation" for e in report["example_final_source_window"]))
        self.assertFalse(certify(make_case(), memory_k=1)["passed"])
        with self.assertRaises(ValueError):
            certify(make_case(), memory_k=0)

    def test_retraction_preserves_the_original_record(self):
        c = make_case(); person = c["solution"]["culprit"]
        false = f"claim_cover_{person}"
        state = record_delivery(c, person, initial_state(c, person), [false])
        intent = {"kind": "challenge", "claim_id": false}
        self.assertEqual(allowed_content(c, person, intent, state)[0]["branch_id"], "maintain_until_relevant_challenge")
        branch = allowed_content(c, person, intent, state, c["gold"]["counter_evidence"][false])[0]
        after = record_delivery(c, person, state, branch["claim_ids"], actual_retraction_ids=branch["retract_claim_ids"])
        self.assertIn(false, after["delivered_claim_ids"])
        self.assertIn(false, after["retracted_claim_ids"])
        self.assertEqual(c["claims"][branch["claim_ids"][0]]["value"], c["solution"]["location"])

    def test_motives_are_private_and_do_not_add_deception_permissions(self):
        c = make_case()
        self.assertNotIn("motive", public_view(c))
        for person in c["suspects"]:
            view = suspect_view(c, person, initial_state(c, person))
            self.assertIn("approach the detective", view["motive"])
            self.assertEqual(view["approved_false_accounts"], [c["claims"][cid]["text"] for cid in c["suspects"][person]["approved_false_claim_ids"]])

    def test_manifest_rejects_old_memory_setting(self):
        config = {"model_id": "mock", "decoding_settings": {}, "k": 5, "turn_budget": 20,
                  "runs_per_condition": 1, "budget_usd": 20, "annotation_sample_size": 0,
                  "prompt_versions": {"detective": "mock"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"; path.write_text(json.dumps(config))
            result = subprocess.run([sys.executable, "-B", "-m", "casework", "manifest", "cases/evaluation.json",
                                     "--config", str(path)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("fixed at k=8", result.stderr)


if __name__ == "__main__":
    unittest.main()

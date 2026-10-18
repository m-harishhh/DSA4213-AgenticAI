"""Tests for detective/action_schema.py. Run from repo root:
    python -m unittest tests.test_action_schema -v
Uses the PILOT case's public view only (never the evaluation answers or private fields).
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework import load_case, public_view
from detective.action_schema import (DecisionContext, decision_json_schema, parse_conversation_reply,
                                     parse_decision, parse_invitation_response)

PUBLIC = public_view(load_case(ROOT / "cases" / "pilot.json"))
WINDOW = {"e1": "Detective: Where were you at 20:30?\nBlair: I was in the Library at 20:30.",
          "e2": "Detective: Did you see Blair?\nDrew: The recording shows Blair in the Gallery at 20:30."}


def ctx(**overrides):
    base = dict(visible_exchanges=WINDOW, turns_left=10)
    base.update(overrides)
    return DecisionContext.from_public(PUBLIC, **base)


class Interrogate(unittest.TestCase):
    def test_valid_question_with_rationale(self):
        r = parse_decision({"action": "interrogate", "suspect": "Blair",
                            "question": "Drew says you were in the Gallery. Where were you really?",
                            "cites": ["e2"], "rationale": "Accounts disagree."}, ctx())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["action"]["cites"], ["e2"])

    def test_case_insensitive_name_is_normalised(self):  # normalize to canonical case
        r = parse_decision({"action": "interrogate", "suspect": " blair ", "question": "Hi?"}, ctx())
        self.assertEqual(r["action"]["suspect"], "Blair")

    def test_unknown_suspect_is_error(self):
        r = parse_decision({"action": "interrogate", "suspect": "Zed", "question": "Hi?"}, ctx())
        self.assertFalse(r["ok"])

    def test_question_over_cap_is_error(self):  # enforce max question length
        r = parse_decision({"action": "interrogate", "suspect": "Blair", "question": "x" * 501}, ctx())
        self.assertFalse(r["ok"])

    def test_rationale_over_cap_is_error(self):  # enforce max rationale length
        r = parse_decision({"action": "interrogate", "suspect": "Blair", "question": "Hi?",
                            "rationale": "x" * 301}, ctx())
        self.assertFalse(r["ok"])

    def test_expired_citation_dropped_with_warning(self):  # drop citations outside memory window
        r = parse_decision({"action": "interrogate", "suspect": "Blair", "question": "Hi?",
                            "cites": ["e1", "e0"]}, ctx())
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"]["cites"], ["e1"])
        self.assertEqual(len(r["warnings"]), 1)

    def test_no_interrogation_when_budget_spent(self):  # proposal §3.5
        r = parse_decision({"action": "interrogate", "suspect": "Blair", "question": "Hi?"}, ctx(turns_left=0))
        self.assertFalse(r["ok"])

    def test_extra_field_is_error(self):
        r = parse_decision({"action": "interrogate", "suspect": "Blair", "question": "Hi?",
                            "intent": "challenge"}, ctx())
        self.assertFalse(r["ok"])

    def test_json_string_and_bad_json(self):
        self.assertTrue(parse_decision('{"action":"interrogate","suspect":"Alex","question":"Hi?"}', ctx())["ok"])
        self.assertFalse(parse_decision("not json", ctx())["ok"])

    def test_record_request_action_does_not_exist(self):  # Josh: no record-request action
        self.assertFalse(parse_decision({"action": "request_record"}, ctx())["ok"])


class Accuse(unittest.TestCase):
    def accuse(self, **extra):
        data = {"action": "accuse", "culprit": "alex", "weapon": "knife", "location": "library"}
        data.update(extra)
        return data

    def test_first_accusation_requires_report(self):  # report bundled with first accusation
        self.assertFalse(parse_decision(self.accuse(), ctx())["ok"])
        r = parse_decision(self.accuse(contradiction_report=[]), ctx())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["action"]["location"], "Library")

    def test_supported_and_unsupported_pairs_both_kept(self):  # keep unverified quotes for evaluation scoring
        good = {"left": {"exchange_id": "e1", "quote": "I was in the Library at 20:30"},
                "right": {"exchange_id": "e2", "quote": "Blair in the Gallery at 20:30"}}
        bad = {"left": {"exchange_id": "e1", "quote": "I never left the Kitchen"},
                "right": {"exchange_id": "e9", "quote": "anything"}}
        r = parse_decision(self.accuse(contradiction_report=[good, bad]), ctx())
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(len(r["action"]["contradiction_report"]), 2)
        self.assertEqual(len(r["warnings"]), 2)

    def test_retry_must_be_distinct(self):  # proposal §3.5
        previous = [("Alex", "Knife", "Library")]
        self.assertFalse(parse_decision(self.accuse(), ctx(previous_accusations=previous))["ok"])
        r = parse_decision(self.accuse(location="Study", contradiction_report=[]),
                           ctx(previous_accusations=previous))
        self.assertTrue(r["ok"], r["errors"])
        self.assertIsNone(r["action"]["contradiction_report"])  # ignored on retries
        self.assertEqual(len(r["warnings"]), 1)

    def test_accusing_allowed_when_budget_spent(self):
        self.assertTrue(parse_decision(self.accuse(contradiction_report=[]), ctx(turns_left=0))["ok"])

    def test_unknown_weapon_is_error(self):
        self.assertFalse(parse_decision(self.accuse(weapon="Pistol", contradiction_report=[]), ctx())["ok"])


class OtherCalls(unittest.TestCase):
    def test_invitation_is_identity_only(self):  # §3.4
        self.assertTrue(parse_invitation_response({"accept": True}, ctx())["ok"])
        self.assertFalse(parse_invitation_response({"accept": True, "message": "sure"}, ctx())["ok"])

    def test_reply_cap(self):  # enforce max conversation reply length
        self.assertTrue(parse_conversation_reply({"message": "Tell me more later."}, ctx())["ok"])
        self.assertFalse(parse_conversation_reply({"message": "x" * 501}, ctx())["ok"])

    def test_json_schema_builds(self):
        schema = decision_json_schema()
        self.assertIn("oneOf", schema)


if __name__ == "__main__":
    unittest.main()

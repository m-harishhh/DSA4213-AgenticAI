"""Tests for detective/agent.py and detective/prompts.py. Run from repo root:
    python -m unittest tests.test_detective_agent -v
Offline (MockBackend). The full pilot case is loaded only to check that private fields never leak.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework import load_case, public_view
from detective import PROMPT_VERSION, DecisionContext, detective_decide, parse_decision
from detective.prompts import EMPTY_MEMORY, MEMORY_LABELS
from infra import LLMClient, MockBackend

CASE = load_case(ROOT / "cases" / "pilot.json")
PUBLIC = public_view(CASE)
WINDOW = {"e1": "Detective: Where were you at 20:30?\nBlair: I was in the Library at 20:30.",
          "e2": "Detective: Did you see Blair?\nDrew: The recording shows Blair in the Gallery at 20:30."}
WINDOW_TEXT = "\n".join(f"[{eid}] {text}" for eid, text in WINDOW.items())
PAYLOADS = {"none": MEMORY_LABELS["none"] + "\n" + WINDOW_TEXT,
            "raw": MEMORY_LABELS["raw"].format(k=8) + "\n" + WINDOW_TEXT,
            "ledger": MEMORY_LABELS["ledger"].format(k=8) + "\n- Blair said: I was in the Library at 20:30. (e1)"}


class Recorder:
    """Minimal stand-in for EventLog: records emit() calls."""

    def __init__(self):
        self.events = []

    def emit(self, kind, **payload):
        self.events.append(dict(payload, kind=kind))


def ctx(**overrides):
    base = dict(visible_exchanges=WINDOW, turns_left=10)
    base.update(overrides)
    return DecisionContext.from_public(PUBLIC, **base)


def client(script=None):
    backend = MockBackend(script)
    log = Recorder()
    return LLMClient(backend, model_id="mock", decoding={}, log=log), backend, log


def decide(context, payload=EMPTY_MEMORY, script=None, **kwargs):
    llm, backend, log = client(script)
    result = detective_decide({"payload": payload}, context, llm, "g1", **kwargs)
    return result, backend, log


class LeakageTests(unittest.TestCase):
    def test_no_private_case_content_in_prompts(self):
        contexts = [ctx(), ctx(turns_left=0), ctx(previous_accusations=[("Blair", "Knife", "Study")])]
        texts = []
        for context in contexts:
            _, backend, _ = decide(context, repair_errors=["suspect: unknown"], repair_of="c000001")
            texts.extend(message["content"] for message in backend.requests[0]["messages"])
        prompt = "\n".join(texts)

        ids = set(CASE["facts"]) | set(CASE["claims"]) | set(CASE["leads"])
        self.assertTrue(ids)
        for item_id in ids:
            self.assertNotIn(item_id, prompt)
        # Alex's secret is only "The murder", so this check is weak for Alex (choice 2a).
        for name, spec in CASE["suspects"].items():
            self.assertNotIn(spec["motive"], prompt, name)
            self.assertNotIn(spec["secret"], prompt, name)
        for marker in ("matched_claim_ids", "intent_contract", "honesty_policy", '"kind"'):
            self.assertNotIn(marker, prompt)


class ConditionInvarianceTests(unittest.TestCase):
    def test_only_memory_payload_differs(self):
        systems, users = [], []
        for payload in PAYLOADS.values():
            _, backend, _ = decide(ctx(), payload=payload)
            system, user = backend.requests[0]["messages"]
            self.assertEqual((system["role"], user["role"]), ("system", "user"))
            self.assertIn(payload, user["content"])
            systems.append(system["content"].encode("utf-8"))
            users.append(user["content"].replace(payload, ""))
        self.assertEqual(len(set(systems)), 1)
        self.assertEqual(len(set(users)), 1)

    def test_one_target_rule_v2(self):
        self.assertEqual(PROMPT_VERSION, "detective-v3")
        sentence = ("Each question should ask about one thing only, such as one person's whereabouts at one time, "
                    "or one clue. Suspects answer only one part of a multi-part question.")
        for payload in PAYLOADS.values():
            _, backend, _ = decide(ctx(), payload=payload)
            self.assertIn(sentence, backend.requests[0]["messages"][0]["content"])


class RoundTripTests(unittest.TestCase):
    def test_interrogate_reply_parses(self):
        reply = {"action": "interrogate", "suspect": "Blair", "question": "Who saw you in the Library?",
                 "cites": ["e1"]}
        context = ctx()
        result, _, log = decide(context, payload=PAYLOADS["raw"], script=[reply])
        self.assertEqual(result["prompt_version"], PROMPT_VERSION)
        self.assertEqual(result["call_id"], log.events[0]["call_id"])
        parsed = parse_decision(result["text"], context)
        self.assertTrue(parsed["ok"], parsed["errors"])
        self.assertEqual((log.events[0]["purpose"], log.events[0]["phase"]), ("detective_decision", "through_first_accusation"))
        self.assertEqual(log.events[0]["role"], "detective")
        self.assertEqual(log.events[0]["max_output_tokens"], 800)

    def test_first_accusation_with_report_parses(self):
        reply = {"action": "accuse", "culprit": "Blair", "weapon": "Knife", "location": "Library",
                 "contradiction_report": [{"left": {"exchange_id": "e1", "quote": "I was in the Library at 20:30"},
                                           "right": {"exchange_id": "e2", "quote": "Blair in the Gallery at 20:30"}}]}
        context = ctx(turns_left=4)
        result, _, log = decide(context, payload=PAYLOADS["raw"], script=[reply])
        parsed = parse_decision(result["text"], context)
        self.assertTrue(parsed["ok"], parsed["errors"])
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual((log.events[0]["purpose"], log.events[0]["phase"]), ("detective_decision", "through_first_accusation"))

    def test_retry_phase(self):
        context = ctx(previous_accusations=[("Blair", "Knife", "Study")])
        _, backend, log = decide(context)
        self.assertEqual(log.events[0]["phase"], "accusation_retries")
        self.assertIn("Accused Blair with Knife in Study: incorrect", backend.requests[0]["messages"][1]["content"])


class RepairTests(unittest.TestCase):
    def test_repair_errors_and_tag(self):
        llm, backend, log = client(["not json", json.dumps({"action": "interrogate", "suspect": "Drew",
                                                            "question": "Where were you at 21:00?"})])
        context = ctx()
        first = detective_decide({"payload": EMPTY_MEMORY}, context, llm, "g1")
        errors = parse_decision(first["text"], context)["errors"]
        detective_decide({"payload": EMPTY_MEMORY}, context, llm, "g1", repair_errors=errors, repair_of=first["call_id"])

        repair_text = "Your previous reply was invalid: " + "; ".join(errors) + "."
        self.assertNotIn(repair_text, backend.requests[0]["messages"][1]["content"])
        self.assertIn(repair_text, backend.requests[1]["messages"][1]["content"])
        self.assertEqual(log.events[0]["tags"], {})
        self.assertEqual(log.events[1]["tags"], {"repair_of": first["call_id"]})


class BudgetTests(unittest.TestCase):
    def test_no_turns_left_forces_accusation(self):
        _, backend, log = decide(ctx(turns_left=0))
        self.assertIn("must accuse now", backend.requests[0]["messages"][1]["content"])
        self.assertEqual(log.events[0]["purpose"], "detective_decision")


class ContextTests(unittest.TestCase):
    def test_missing_public_raises(self):
        context = DecisionContext(suspects=PUBLIC["suspects"], weapons=PUBLIC["weapons"],
                                  locations=PUBLIC["locations"], turns_left=10)
        llm, backend, _ = client()
        with self.assertRaises(ValueError):
            detective_decide({"payload": EMPTY_MEMORY}, context, llm, "g1")
        self.assertEqual(backend.requests, [])


if __name__ == "__main__":
    unittest.main()

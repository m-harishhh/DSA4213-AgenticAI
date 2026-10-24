"""Tests for the detective's checkpoint calls (§3.4): accept/reject and conversation reply. Run from repo root:
    python -m unittest tests.test_detective_checkpoint -v
Offline (MockBackend). The full pilot case is loaded only to check that private fields never leak.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework import load_case, public_view
from detective import (PROMPT_VERSION, DecisionContext, SCHEMA_CONFIG, detective_conversation_reply,
                       detective_invitation_response, parse_decision)
from detective.prompts import APPROACH, CHECKPOINT_PROTOCOL, CHECKPOINT_SYSTEM_PROMPT, EXAMPLE, MEMORY_LABELS
from infra import LLMCallError, LLMClient, MockBackend, TransportError

CASE = load_case(ROOT / "cases" / "pilot.json")
PUBLIC = public_view(CASE)
WINDOW = {"e1": "Detective: Where were you at 20:30?\nBlair: I was in the Library at 20:30."}
WINDOW_TEXT = "\n".join(f"[{eid}]\n{text}" for eid, text in WINDOW.items())
PAYLOADS = {"none": MEMORY_LABELS["none"] + "\n" + WINDOW_TEXT,
            "raw": MEMORY_LABELS["raw"].format(k=8) + "\n" + WINDOW_TEXT,
            "ledger": MEMORY_LABELS["ledger"].format(k=8) + '\n[e1] Blair\n- claim | speaker: Blair | "I was in the Library"'}
MESSAGE = "I saw something odd near the Gallery and wanted you to hear it first."


class Recorder:
    """Minimal stand-in for EventLog: records emit() calls."""

    def __init__(self):
        self.events = []

    def emit(self, kind, **payload):
        self.events.append(dict(payload, kind=kind))


def ctx(**overrides):
    base = dict(visible_exchanges=WINDOW, turns_left=15)
    base.update(overrides)
    return DecisionContext.from_public(PUBLIC, **base)


def client(script):
    backend = MockBackend(script)
    log = Recorder()
    return LLMClient(backend, model_id="mock", decoding={}, log=log, sleep=lambda seconds: None), backend, log


def prompt_text(request):
    return "\n".join(message["content"] for message in request["messages"])


class AcceptTests(unittest.TestCase):
    def test_accept_true_and_false(self):
        for accept in (True, False):
            llm, _, log = client([{"accept": accept}])
            result = detective_invitation_response("Drew", {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
            self.assertEqual(result, {"accept": accept, "ok": True, "errors": [], "call_id": log.events[0]["call_id"],
                                      "prompt_version": PROMPT_VERSION})

    def test_accept_prompt_is_identity_only(self):
        llm, backend, _ = client([{"accept": True}])
        detective_invitation_response("Drew", {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        system, user = backend.requests[0]["messages"]
        self.assertIn("Drew invites you to a private conversation.", user["content"])
        self.assertIn(CHECKPOINT_PROTOCOL, user["content"])
        self.assertIn(PAYLOADS["raw"], user["content"])
        prompt = prompt_text(backend.requests[0])
        self.assertNotIn(MESSAGE, prompt)
        self.assertNotIn(" says: ", prompt)
        for other in ("Alex", "Casey", "Ellis"):
            self.assertNotIn(f"{other} invites you", prompt)
        for item_id in set(CASE["claims"]) | set(CASE["facts"]) | set(CASE["leads"]):
            self.assertNotIn(item_id, prompt)
        for spec in CASE["suspects"].values():
            self.assertNotIn(spec["motive"], prompt)
            self.assertNotIn(spec["secret"], prompt)
        # Checkpoint status has no contradiction-report or must-accuse lines.
        self.assertNotIn("contradiction_report", user["content"])
        self.assertNotIn("must accuse now", user["content"])

    def test_no_state_between_calls(self):
        llm, backend, _ = client([{"accept": False}, {"accept": True}])
        detective_invitation_response("Drew", {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        detective_invitation_response("Casey", {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        second = prompt_text(backend.requests[1])
        self.assertIn("Casey invites you", second)
        self.assertNotIn("Drew invites you", second)
        self.assertNotIn('"accept"', second.replace('{"accept": true} or {"accept": false}', ""))
        self.assertEqual(len(backend.requests[1]["messages"]), 2)
        self.assertEqual(backend.requests[0]["messages"][0], backend.requests[1]["messages"][0])


class ReplyTests(unittest.TestCase):
    def test_reply_happy_path(self):
        llm, backend, log = client([{"message": "Thank you. Who else was near the Gallery?"}])
        result = detective_conversation_reply("Drew", MESSAGE, {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        self.assertEqual(result, {"message": "Thank you. Who else was near the Gallery?", "ok": True, "errors": [],
                                  "call_id": log.events[0]["call_id"], "prompt_version": PROMPT_VERSION})
        user = backend.requests[0]["messages"][1]["content"]
        self.assertIn(f'Drew says: "{MESSAGE}"', user)
        self.assertIn(f"at most {SCHEMA_CONFIG['max_reply_chars']} characters", user)

    def test_over_long_reply_is_repaired_once(self):
        too_long = {"message": "x" * (SCHEMA_CONFIG["max_reply_chars"] + 1)}
        llm, backend, log = client([too_long, {"message": "Noted, thank you."}])
        result = detective_conversation_reply("Drew", MESSAGE, {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "Noted, thank you.")
        self.assertEqual(len(backend.requests), 2)
        self.assertIn("Your previous reply was invalid: message:", backend.requests[1]["messages"][1]["content"])
        self.assertEqual(log.events[1]["tags"], {"repair_of": log.events[0]["call_id"]})
        self.assertEqual(result["call_id"], log.events[1]["call_id"])

    def test_two_invalid_replies_fail(self):
        too_long = {"message": "x" * (SCHEMA_CONFIG["max_reply_chars"] + 1)}
        llm, backend, log = client([too_long, "not json"])
        result = detective_conversation_reply("Drew", MESSAGE, {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["message"])
        self.assertTrue(result["errors"])
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(result["call_id"], log.events[1]["call_id"])

    def test_invalid_accept_twice_fails(self):
        llm, backend, _ = client([{"accept": "perhaps"}, {"accept": True, "why": "x"}])
        result = detective_invitation_response("Drew", {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        self.assertEqual((result["ok"], result["accept"]), (False, None))
        self.assertEqual(len(backend.requests), 2)


class LoggingTests(unittest.TestCase):
    def test_logged_role_purpose_phase_and_caps(self):
        llm, _, log = client([{"accept": True}, {"message": "Thank you."}])
        detective_invitation_response("Drew", {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        detective_conversation_reply("Drew", MESSAGE, {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        accept, reply = log.events
        self.assertEqual((accept["role"], accept["purpose"], accept["phase"]),
                         ("detective", "acceptance", "through_first_accusation"))
        self.assertEqual((reply["role"], reply["purpose"], reply["phase"]),
                         ("detective", "conversation_reply", "through_first_accusation"))
        self.assertEqual(accept["max_output_tokens"], SCHEMA_CONFIG["max_acceptance_tokens"])
        self.assertEqual(reply["max_output_tokens"], SCHEMA_CONFIG["max_reply_tokens"])
        self.assertIsNone(accept["response_schema"])

    def test_system_prompt_identical_across_conditions(self):
        systems = set()
        for payload in PAYLOADS.values():
            llm, backend, _ = client([{"accept": True}, {"message": "Thank you."}])
            detective_invitation_response("Drew", {"payload": payload}, ctx(), llm, "g1")
            detective_conversation_reply("Drew", MESSAGE, {"payload": payload}, ctx(), llm, "g1")
            systems.update(request["messages"][0]["content"] for request in backend.requests)
        self.assertEqual(systems, {CHECKPOINT_SYSTEM_PROMPT})

    def test_checkpoint_prompt_has_approach_but_no_example(self):
        self.assertIn(APPROACH, CHECKPOINT_SYSTEM_PROMPT)
        self.assertNotIn(EXAMPLE, CHECKPOINT_SYSTEM_PROMPT)
        self.assertNotIn("Mara", CHECKPOINT_SYSTEM_PROMPT)
        self.assertNotIn("Follow up on leads people mention, and compare accounts against each other.",
                         CHECKPOINT_SYSTEM_PROMPT)

    def test_llm_call_error_propagates(self):
        llm, _, _ = client([TransportError("down")] * 4)
        with self.assertRaises(LLMCallError):
            detective_invitation_response("Drew", {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        llm, _, _ = client([TransportError("down")] * 4)
        with self.assertRaises(LLMCallError):
            detective_conversation_reply("Drew", MESSAGE, {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")

    def test_missing_public_raises(self):
        context = DecisionContext(suspects=PUBLIC["suspects"], weapons=PUBLIC["weapons"],
                                  locations=PUBLIC["locations"], turns_left=15)
        llm, backend, _ = client([])
        with self.assertRaises(ValueError):
            detective_invitation_response("Drew", {"payload": PAYLOADS["raw"]}, context, llm, "g1")
        self.assertEqual(backend.requests, [])


def fenced(obj):
    return "```json\n" + json.dumps(obj) + "\n```"


FENCE_WARNING = "output: code fences stripped"


class CodeFenceTests(unittest.TestCase):
    """pilot_04c: gemini-3.5-flash-lite wraps valid JSON in ```json fences; that is formatting, not a failure."""

    def test_fenced_accept_needs_no_repair(self):
        llm, backend, _ = client([fenced({"accept": True})])
        result = detective_invitation_response("Drew", {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        self.assertEqual((result["accept"], result["ok"]), (True, True))
        self.assertEqual(len(backend.requests), 1)

    def test_fenced_reply_needs_no_repair(self):
        llm, backend, _ = client([fenced({"message": "Thank you. Who else was there?"})])
        result = detective_conversation_reply("Drew", MESSAGE, {"payload": PAYLOADS["raw"]}, ctx(), llm, "g1")
        self.assertEqual((result["message"], result["ok"]), ("Thank you. Who else was there?", True))
        self.assertEqual(len(backend.requests), 1)

    def test_fenced_decision_is_accepted_with_warning(self):
        action = {"action": "interrogate", "suspect": "Blair", "question": "Where were you at 21:00?"}
        result = parse_decision(fenced(action), ctx())
        self.assertTrue(result["ok"], result["errors"])
        self.assertIn(FENCE_WARNING, result["warnings"])

    def test_unfenced_has_no_warning(self):
        action = {"action": "interrogate", "suspect": "Blair", "question": "Where were you at 21:00?"}
        for raw in (json.dumps(action), json.dumps(action) + "\n", json.dumps(action).encode("utf-8")):
            result = parse_decision(raw, ctx())
            self.assertTrue(result["ok"], result["errors"])
            self.assertNotIn(FENCE_WARNING, result["warnings"])

    def test_fenced_invalid_action_still_fails(self):
        result = parse_decision(fenced({"action": "interrogate", "suspect": "Zed", "question": "Hi?"}), ctx())
        self.assertFalse(result["ok"])
        self.assertTrue(any("suspect" in error for error in result["errors"]))
        self.assertIn(FENCE_WARNING, result["warnings"])

    def test_fenced_broken_json_keeps_warning(self):
        result = parse_decision("```json\n{\"action\": \n```", ctx())
        self.assertFalse(result["ok"])
        self.assertIn(FENCE_WARNING, result["warnings"])

    def test_prose_around_json_is_not_extracted(self):
        result = parse_decision('Here you go: {"accept": true}', ctx())
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()

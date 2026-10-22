"""End-to-end tests for controller/game.py and controller/suspect.py with mock parts. Run from repo root:
    python -m unittest tests.test_game -v
Plays whole games through run_game with the real detective_decide, suspect pipeline and LLM wrapper
(MockBackend, no API calls); memory, interpreter and extraction are faked. Every log must pass
infra.validate_events. Uses the PILOT case only.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework import load_case, initial_state, allowed_content
from infra import LLMClient, MockBackend, TransportError, read_events, validate_events
from controller.game import run_game, default_parts
from controller.suspect import match_delivery

CASE_PATH = str(ROOT / "cases" / "pilot.json")
CASE = load_case(CASE_PATH)


def fake_memory(condition, exchanges, ledger, k):
    window = exchanges[-1:] if condition == "none" else exchanges[-k:]
    if condition == "ledger":
        payload = json.dumps([ledger.get(e["exchange_id"]) for e in window])
    else:
        payload = "\n".join(e["text"] for e in window) or "(No exchanges yet.)"
    return {"window_exchange_ids": [e["exchange_id"] for e in window], "payload": payload,
            "visible_exchanges": {e["exchange_id"]: e["text"] for e in window}}


def fake_interpret(question, suspect, cites, public, delivered, llm, game_id):
    return {"intent": {"kind": "whereabouts", "time": "21:00"}, "cited_claim_ids": [], "call_id": None}


def fake_extract(exchange, llm, game_id):
    return {"entries": [{"speaker": exchange["speaker"], "statement": exchange["text"], "source_span": exchange["text"]}],
            "status": "ok", "call_id": None, "failure_entry": None}


def failing_extract(exchange, llm, game_id):
    return {"entries": [], "status": "failed", "call_id": None,
            "failure_entry": {"type": "extraction_failure", "exchange_id": exchange["exchange_id"],
                              "reason": "schema failure after one repair"}}


def parts(**overrides):
    """Real detective and suspect pipeline; fake memory, interpreter and extraction."""
    return {**default_parts(), "memory": fake_memory, "interpret": fake_interpret, "extract": fake_extract,
            **overrides}


def question(suspect="Alex"):
    return {"action": "interrogate", "suspect": suspect, "question": "Where were you at 21:00?"}


def accuse(culprit, first=False):
    action = {"action": "accuse", "culprit": culprit, "weapon": "Knife", "location": "Library"}
    if first:
        action["contradiction_report"] = []
    return action


def script(n_questions, guesses):
    """Detective decisions interleaved with one suspect answer per question, as the LLM sees them."""
    out = []
    for _ in range(n_questions):
        out += [question(), "I was in the Study at 21:00."]
    return out + [accuse(g, first=(i == 0)) for i, g in enumerate(guesses)]


class GameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.seed = 0

    def play(self, responses, condition="raw", game_parts=None):
        self.seed += 1  # a fresh game_id per game: EventLog appends to an existing file
        llm = LLMClient(MockBackend(responses), "mock-model", {"temperature": 0})
        result = run_game(CASE_PATH, {"run_id": "test"}, self.seed, condition, llm, self.tmp.name,
                          game_parts or parts())
        events = read_events(result["log_path"])
        validation = validate_events(events)
        self.assertTrue(validation["passed"], validation["errors"][:3])
        return result, events

    def end(self, events):
        return next(e for e in events if e["kind"] == "game_end")


class Outcomes(GameTest):
    def test_every_outcome_in_every_condition(self):
        cases = {"solved_first": (2, ["Alex"]), "solved_retry": (1, ["Blair", "Alex"]),
                 "unsolved": (1, ["Blair", "Casey", "Drew"])}
        for condition in ("none", "raw", "ledger"):
            for outcome, (n, guesses) in cases.items():
                with self.subTest(condition=condition, outcome=outcome):
                    result, events = self.play(script(n, guesses), condition)
                    self.assertEqual(result["outcome"], outcome)
                    self.assertEqual(self.end(events)["attempts_used"], len(guesses))

    def test_first_accusation_and_components(self):
        result, events = self.play(script(1, ["Blair", "Alex"]))
        self.assertFalse(result["first_accusations_correct"])
        self.assertEqual(self.end(events)["component_correct"],
                         {"culprit": False, "weapon": True, "location": True})

    def test_feedback_is_whole_triple_only(self):
        _, events = self.play(script(0, ["Blair", "Alex"]))
        feedback = [e["feedback"] for e in events if e["kind"] == "accusation"]
        self.assertEqual(feedback, ["incorrect", "correct"])

    def test_contradiction_report_logged_once(self):
        _, events = self.play(script(0, ["Blair", "Casey", "Alex"]))
        self.assertEqual(sum(e["kind"] == "contradiction_report" for e in events), 1)


class Budgets(GameTest):
    def test_turn_budget_forces_an_accusation(self):
        result, events = self.play(script(20, ["Alex"]))
        self.assertEqual(self.end(events)["interrogations_used"], 20)
        self.assertEqual(result["outcome"], "solved_first")

    def test_question_after_budget_is_a_failed_run(self):
        result, events = self.play(script(20, []) + [question(), question()])
        self.assertEqual(result["outcome"], "failed_run")
        self.assertEqual(self.end(events)["interrogations_used"], 20)

    def test_no_interrogation_after_first_accusation(self):
        result, _ = self.play(script(0, ["Blair"]) + [question(), question()])
        self.assertEqual(result["outcome"], "failed_run")

    def test_repeated_triple_is_rejected(self):
        result, _ = self.play(script(0, ["Blair", "Blair"]) + [accuse("Blair")])
        self.assertEqual(result["outcome"], "failed_run")


class Failures(GameTest):
    def test_invalid_output_twice_is_a_failed_run(self):
        result, events = self.play(["not json", "still not json"])
        self.assertEqual(result["outcome"], "failed_run")
        self.assertIsNone(self.end(events)["component_correct"])
        self.assertTrue(any(e["kind"] == "failure" for e in events))

    def test_repair_is_tagged_with_the_call_it_repairs(self):
        result, events = self.play(["not json"] + script(0, ["Alex"]))
        self.assertEqual(result["outcome"], "solved_first")
        tags = [e["tags"] for e in events if e["kind"] == "llm_call" and e["role"] == "detective"]
        self.assertIn("repair_of", tags[1])

    def test_llm_call_failure_is_logged_and_a_failed_run(self):
        # Every attempt fails in transport, so the wrapper's bounded retries run out.
        errors = [TransportError("503") for _ in range(10)]
        llm = LLMClient(MockBackend(errors), "mock-model", {"temperature": 0}, sleep=lambda seconds: None)
        result = run_game(CASE_PATH, {"run_id": "test"}, 99, "raw", llm, self.tmp.name, parts())
        events = read_events(result["log_path"])
        self.assertTrue(validate_events(events)["passed"])
        self.assertEqual(result["outcome"], "failed_run")
        failure = next(e for e in events if e["kind"] == "failure")
        self.assertEqual((failure["stage"], failure["reason"]), ("llm_call", "transport_failure"))

    def test_software_crash_is_logged_and_reraised(self):
        def broken_respond(*args, **kwargs):
            raise ZeroDivisionError("bug in the suspect pipeline")
        llm = LLMClient(MockBackend([question()]), "mock-model", {"temperature": 0})
        with self.assertRaises(ZeroDivisionError):
            run_game(CASE_PATH, {"run_id": "test"}, 98, "raw", llm, self.tmp.name, parts(respond=broken_respond))
        events = read_events(Path(self.tmp.name) / "test_raw_98.jsonl")
        self.assertEqual((events[-1]["kind"], events[-1]["stage"]), ("failure", "software_crash"))
        self.assertFalse(any(e["kind"] == "game_end" for e in events))  # a crash is not a game result
        self.assertTrue(llm.log._handle.closed)

    def test_failed_extraction_is_explicit_in_ledger(self):
        _, events = self.play(script(1, ["Alex"]), "ledger", parts(extract=failing_extract))
        extraction = next(e for e in events if e["kind"] == "extraction")
        self.assertEqual(extraction["status"], "failed")
        self.assertEqual(extraction["failure_entry"]["type"], "extraction_failure")


class SuspectPipeline(GameTest):
    def test_suspect_calls_carry_stable_agent_ids(self):
        _, events = self.play(script(1, ["Alex"]))
        calls = [e for e in events if e["kind"] == "llm_call" and e["role"] == "suspect"]
        self.assertEqual({c["agent_id"] for c in calls}, {"suspect_1"})  # Alex is first in public order

    def test_delivered_cover_story_updates_state(self):
        _, events = self.play(script(1, ["Alex"]))
        interrogation = next(e for e in events if e["kind"] == "interrogation")
        self.assertEqual(interrogation["controller_matched"]["claim_ids"], ["claim_cover_Alex"])
        self.assertIsNone(interrogation["selected_branch_id"])  # the suspect model chose, not the controller

    def test_suspect_prompt_hides_internal_ids(self):
        _, events = self.play(script(1, ["Alex"]))
        call = next(e for e in events if e["kind"] == "llm_call" and e["role"] == "suspect")
        prompt = " ".join(m["content"] for m in call["messages"])
        for internal in ("claim_", "fact_", "approved_cover", "self_protective"):
            self.assertNotIn(internal, prompt)


class MatchDelivery(unittest.TestCase):
    def match(self, suspect, intent, text):
        branches = allowed_content(CASE, suspect, intent, initial_state(CASE, suspect))
        return match_delivery(CASE, suspect, text, branches)

    def test_claims(self):
        intent = {"kind": "whereabouts", "time": "21:00"}
        self.assertEqual(self.match("Alex", intent, "I was in the Study at 21:00.")["claim_ids"], ["claim_cover_Alex"])
        self.assertEqual(self.match("Alex", intent, "No, I was in the Library at 21:00.")["claim_ids"],
                         ["claim_self_Alex_21:00"])
        self.assertEqual(self.match("Alex", intent, "I'd rather not say.")["claim_ids"], [])
        self.assertEqual(self.match("Alex", intent, "I was in the Study at 20:30.")["claim_ids"], [])

    def test_negation_is_not_delivery(self):
        intent = {"kind": "whereabouts", "time": "21:00"}
        self.assertEqual(self.match("Alex", intent, "I was NOT in the Library at 21:00.")["claim_ids"], [])
        self.assertEqual(self.match("Alex", intent, "I was never in the Study at 21:00.")["claim_ids"], [])
        self.assertEqual(self.match("Alex", intent, "I wasn't in the Study at 21:00, I was in the Library at 21:00.")
                         ["claim_ids"], ["claim_self_Alex_21:00"])

    def test_leads(self):
        overview = {"kind": "overview"}
        self.assertEqual(self.match("Blair", overview, "Ask me about the attacker's badge.")["lead_ids"], ["badge_trace"])
        self.assertEqual(self.match("Blair", overview, "I don't know anything about the badge.")["lead_ids"], [])
        self.assertEqual(self.match("Drew", overview, "At 21:00 I was busy.")["lead_ids"], [])


if __name__ == "__main__":
    unittest.main()

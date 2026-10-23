"""End-to-end leakage and matched-horizon tests for the detective (qa/blackbox_requirements.md). Run from repo root:
    python -m unittest tests.test_detective_end_to_end -v
Plays one game per condition (none, raw, ledger) through controller.game.run_game on the PILOT case with the
same seed and default_parts(): the real detective, memory, extraction, interpreter and suspect pipeline.
Only the LLM is scripted (MockBackend callable); each request's role is identified from its system message.

Not testable end to end yet (checkpoints are not in the game loop): CKPT-7 (a detective conversation enters
memory as one exchange and can evict the oldest) and CKPT-8 (suspect-only conversations never enter memory).
Covered for now by tests/test_detective_memory.py (WindowTests: detective_conversation is eligible and counts
toward k; suspect_conversation and invitation are excluded) and tests/test_detective_checkpoint.py.
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework import load_case
from controller.game import default_parts, run_game
from detective.prompts import SYSTEM_PROMPT
from infra import LLMClient, MockBackend, read_events, validate_events

CASE_PATH = str(ROOT / "cases" / "pilot.json")
CASE = load_case(CASE_PATH)
SEED = 7
CONDITIONS = ("none", "raw", "ledger")

QUESTIONS = [("Alex", "Where were you at 21:00?"),
             ("Drew", "What does your recording show at 21:00?"),
             ("Blair", "Where were you at 20:30?"),
             ("Casey", "Did you see anyone near the Library?")]
ANSWERS = {"Alex": "I was in the Study at 21:00.",
           "Drew": "The recording shows Alex in the Library at 21:00. Ask Alex about the floor.",
           "Blair": "I think I was in the Gallery at 20:30.",
           "Casey": "I'd rather not say."}
# Quotes that appear in no exchange, so any later appearance can only come from feeding the report back (CONT-2).
MARKER_LEFT, MARKER_RIGHT = "MARKER-LEFT-QUOTE-7Q", "MARKER-RIGHT-QUOTE-9Z"
FIRST_ACCUSATION = {"action": "accuse", "culprit": "Blair", "weapon": "Knife", "location": "Library",
                    "contradiction_report": [{"left": {"exchange_id": "e1", "quote": MARKER_LEFT},
                                              "right": {"exchange_id": "e2", "quote": MARKER_RIGHT}}]}
RETRY = {"action": "accuse", "culprit": "Alex", "weapon": "Knife", "location": "Library"}
INTERPRETATION = {"kind": "whereabouts", "time": "21:00", "subject": None, "topic": None, "challenged": None,
                  "evidence": []}


def entry(type_, speaker, span, target=None, topic=None):
    return {"type": type_, "speaker": speaker, "statement": span, "entities": [], "time": None, "target": target,
            "topic": topic, "retracts": False, "source_span": span}


def extraction_reply(user):
    """One Detective entry and one suspect entry per exchange, plus a referral when the answer gives one."""
    question_line, answer_line = user.split("\n")[1:3]
    speaker, answer = answer_line.split(": ", 1)
    entries = [entry("claim", "Detective", question_line.removeprefix("Detective: ")), entry("claim", speaker, answer)]
    if "Ask Alex about the floor." in answer:
        entries.append(entry("unresolved_lead", speaker, "Ask Alex about the floor.", "Alex", "the floor"))
    return {"entries": entries}


def scripted_llm():
    decisions = [{"action": "interrogate", "suspect": s, "question": q} for s, q in QUESTIONS]
    decisions += [FIRST_ACCUSATION, RETRY]

    def respond(request):
        system, user = request["messages"][0]["content"], request["messages"][-1]["content"]
        if system.startswith("You are a celebrated private detective"):
            return decisions.pop(0)
        if system.startswith("You classify one question"):
            return INTERPRETATION
        if system.startswith("You take notes"):
            return extraction_reply(user)
        name = re.match(r"You are (\w+), a staff member", system).group(1)
        return ANSWERS[name]

    return LLMClient(MockBackend(respond), "mock-model", {"temperature": 0})


def private_strings():
    """Private case content that must never reach a detective or extractor prompt."""
    strings = set(CASE["claims"]) | set(CASE["facts"]) | set(CASE["leads"])
    strings |= {claim["key"] for claim in CASE["claims"].values()}
    strings |= {"matched_claim_ids", "claim_keys", "lead_labels", "self_protective", json.dumps(CASE["solution"])}
    for spec in CASE["suspects"].values():
        strings |= {spec["motive"], spec["secret"]}
    return strings


class DetectiveEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.results, cls.events = {}, {}
        for condition in CONDITIONS:
            result = run_game(CASE_PATH, {"run_id": "e2e"}, SEED, condition, scripted_llm(), cls.tmp.name,
                              default_parts())
            cls.results[condition] = result
            cls.events[condition] = read_events(result["log_path"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def kind(self, condition, kind):
        return [e for e in self.events[condition] if e["kind"] == kind]

    def calls(self, condition, role):
        return [e for e in self.kind(condition, "llm_call") if e["role"] == role]

    def test_logs_valid_and_games_complete(self):
        for condition in CONDITIONS:
            with self.subTest(condition=condition):
                validation = validate_events(self.events[condition])
                self.assertTrue(validation["passed"], validation["errors"][:3])
                self.assertEqual(self.results[condition]["outcome"], "solved_retry")
                self.assertEqual(len(self.kind(condition, "interrogation")), len(QUESTIONS))
        self.assertEqual(len(self.kind("ledger", "extraction")), len(QUESTIONS))

    def test_sys3_none_window_is_latest_exchange(self):
        # SYS-3: empty before the first exchange, then exactly the latest exchange.
        latest = None
        for event in self.events["none"]:
            if event["kind"] == "interrogation":
                latest = event["exchange_id"]
            elif event["kind"] == "memory_view":
                self.assertEqual(event["window_exchange_ids"], [latest] if latest else [])

    def test_sys5_raw_and_ledger_windows_match(self):
        # SYS-5 (matched horizon) and SYS-4 (most recent min(k, t) exchanges, in order).
        raw = [e["window_exchange_ids"] for e in self.kind("raw", "memory_view")]
        ledger = [e["window_exchange_ids"] for e in self.kind("ledger", "memory_view")]
        self.assertEqual(raw, ledger)
        self.assertEqual(raw[0], [])
        self.assertEqual(raw[-1], [f"e{i}" for i in range(1, len(QUESTIONS) + 1)])

    def test_leakage_no_private_case_data(self):
        # ENV-2, ENV-3, SYS-10 and §4.2: nothing private reaches the detective or the extractor.
        forbidden = private_strings()
        for condition in CONDITIONS:
            for call in self.calls(condition, "detective") + self.calls(condition, "extractor"):
                prompt = "\n".join(message["content"] for message in call["messages"])
                for text in forbidden:
                    self.assertNotIn(text, prompt, f"{condition} {call['role']} {call['call_id']}")

    def test_sys7_extractor_sees_one_exchange(self):
        # SYS-7: each extraction sees exactly one exchange's delivered text and no other exchange id.
        texts = {e["exchange_id"]: f"Detective: {e['question']}\n{e['suspect']}: {e['response']}"
                 for e in self.kind("ledger", "interrogation")}
        calls = self.calls("ledger", "extractor")
        self.assertEqual(len(calls), len(texts))
        for call, extraction in zip(calls, self.kind("ledger", "extraction")):
            user = call["messages"][-1]["content"]
            self.assertEqual([eid for eid, text in texts.items() if text in user], [extraction["exchange_id"]])
            self.assertEqual(set(re.findall(r"\be\d+\b", user)), {extraction["exchange_id"]})

    def test_sys6_ledger_has_no_verdicts_or_links(self):
        # SYS-6: no truth labels or supporting/conflicting links in the ledger view.
        for view in self.kind("ledger", "memory_view"):
            for word in ("true", "false", "contradicts", "contradiction", "supports", "guilty", "verdict"):
                self.assertIsNone(re.search(rf"\b{word}\b", view["memory_payload"], re.IGNORECASE), word)
        last = self.kind("ledger", "memory_view")[-1]["memory_payload"]
        self.assertIn("speaker: Detective (your question)", last)
        self.assertIn("ask: Alex | topic: the floor", last)

    def test_ctrl2_detective_system_prompt_identical(self):
        # CTRL-2: the detective's system message is byte-identical across conditions.
        systems = {call["messages"][0]["content"] for condition in CONDITIONS
                   for call in self.calls(condition, "detective")}
        self.assertEqual(systems, {SYSTEM_PROMPT})

    def test_cont2_report_not_fed_back(self):
        # CONT-2: the contradiction report never reaches later detective prompts or memory.
        for condition in CONDITIONS:
            with self.subTest(condition=condition):
                report = self.kind(condition, "contradiction_report")
                self.assertEqual(len(report), 1)
                first_accusation = self.kind(condition, "accusation")[0]["seq"]
                later = [c for c in self.calls(condition, "detective") if c["seq"] > first_accusation]
                self.assertTrue(later)
                for call in self.calls(condition, "detective"):
                    prompt = "\n".join(message["content"] for message in call["messages"])
                    self.assertNotIn(MARKER_LEFT, prompt)
                    self.assertNotIn(MARKER_RIGHT, prompt)
                for call in later:
                    user = call["messages"][-1]["content"]
                    self.assertNotIn("contradiction_report", user)
                    self.assertNotIn(json.dumps(report[0]["reported_pairs_raw"]), user)
                for view in self.kind(condition, "memory_view"):
                    self.assertNotIn(MARKER_LEFT, view["memory_payload"])


if __name__ == "__main__":
    unittest.main()

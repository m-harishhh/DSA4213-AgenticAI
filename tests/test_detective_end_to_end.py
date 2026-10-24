"""End-to-end leakage and matched-horizon tests for the detective (qa/blackbox_requirements.md). Run from repo root:
    python -m unittest tests.test_detective_end_to_end -v
Plays one game per condition (none, raw, ledger) through controller.game.run_game on the PILOT case with the
same seed and default_parts(): the real detective, memory, extraction, interpreter and suspect pipeline.
Only the LLM is scripted (MockBackend callable); each request's role is identified from its system message.

CKPT-7 (a detective conversation enters memory as one exchange and can evict the oldest) and CKPT-8
(suspect-only conversations never reach the detective) are covered end to end by CheckpointEndToEnd: the real
detective checkpoint calls and memory, with fake suspect-side checkpoint parts (invite, accept, message, reply).
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

# Checkpoint games: 11 questions, so checkpoints run after interrogations 5 and 10 (not 15).
CHECKPOINT_QUESTIONS = (QUESTIONS * 3)[:11]
DETECTIVE_MARKER = "the lantern flickered at 20:45"
PRIVATE_MARKER = "the violet umbrella"
DETECTIVE_REPLY = "Thank you, noted."
# (suspect, nth time asked to invite) -> recipient. A suspect who completes a conversation is not asked again
# at the same checkpoint, so Blair's 2nd ask is checkpoint 2.
INVITES = {("Blair", 1): "Detective", ("Casey", 1): "Drew", ("Blair", 2): "Detective"}
MESSAGES = {("Blair", "Detective"): f"I wanted you to know that {DETECTIVE_MARKER} near the Gallery.",
            ("Casey", "Drew"): f"Keep this between us: I left {PRIVATE_MARKER} in the Study."}


def entry(type_, speaker, span, target=None, topic=None):
    return {"type": type_, "speaker": speaker, "statement": span, "entities": [], "time": None, "target": target,
            "topic": topic, "retracts": False, "source_span": span}


def extraction_reply(user):
    """One entry per "Speaker: text" line of the exchange (so a Detective entry and a suspect entry),
    plus a referral when the answer gives one."""
    entries = []
    for line in user.split("\n")[1:]:
        speaker, text = line.split(": ", 1)
        entries.append(entry("claim", speaker, text))
        if "Ask Alex about the floor." in text:
            entries.append(entry("unresolved_lead", speaker, "Ask Alex about the floor.", "Alex", "the floor"))
    return {"entries": entries}


def scripted_llm(questions=QUESTIONS):
    decisions = [{"action": "interrogate", "suspect": s, "question": q} for s, q in questions]
    decisions += [FIRST_ACCUSATION, RETRY]

    def respond(request):
        system, user = request["messages"][0]["content"], request["messages"][-1]["content"]
        if system.startswith("You are a celebrated private detective"):
            if '{"accept": true}' in user:        # checkpoint invitation: accept every one
                return {"accept": True}
            if '{"message":' in user:             # checkpoint conversation: one bounded reply
                return {"message": DETECTIVE_REPLY}
            return decisions.pop(0)
        if system.startswith("You classify one question"):
            return INTERPRETATION
        if system.startswith("You take notes"):
            return extraction_reply(user)
        name = re.match(r"You are (\w+), a staff member", system).group(1)
        return ANSWERS[name]

    return LLMClient(MockBackend(respond), "mock-model", {"temperature": 0})


def checkpoint_parts():
    """Fake suspect-side checkpoint parts (tests/test_game.py pattern); the detective side stays real.
    options=[] so no message is matched to a catalogued claim."""
    asked = {}

    def invite(case, suspect, state, history, available, llm, game_id):
        asked[suspect] = asked.get(suspect, 0) + 1
        choice = INVITES.get((suspect, asked[suspect]))
        return {"invite": choice if choice in available else None, "call_id": None}

    def accept(case, suspect, inviter, state, history, llm, game_id):
        return {"accept": True, "call_id": None}

    def message(case, suspect, recipient, state, history, llm, game_id):
        return {"text": MESSAGES[(suspect, recipient)], "options": [], "call_id": None}

    def reply(case, suspect, inviter, text, state, history, llm, game_id):
        return {"text": "Understood.", "options": [], "call_id": None}

    return {**default_parts(), "invite": invite, "accept": accept, "message": message, "reply": reply}


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


class CheckpointEndToEnd(unittest.TestCase):
    """Checkpoint 1: Blair invites the detective, Casey invites Drew (private). Checkpoint 2: Blair invites the
    detective again with a full window. Exchanges: e1-e5, e6 (conversation), e7-e11, e12 (conversation), e13."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.results, cls.events = {}, {}
        for condition in CONDITIONS:
            result = run_game(CASE_PATH, {"run_id": "e2e_ckpt"}, SEED, condition,
                              scripted_llm(CHECKPOINT_QUESTIONS), cls.tmp.name, checkpoint_parts())
            cls.results[condition] = result
            cls.events[condition] = read_events(result["log_path"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def kind(self, condition, kind):
        return [e for e in self.events[condition] if e["kind"] == kind]

    def conversations(self, condition, visibility):
        return [e for e in self.kind(condition, "conversation") if e["visibility"] == visibility]

    def views_after(self, condition, seq, decision_types=("question", "accusation")):
        return [e for e in self.kind(condition, "memory_view") if e["seq"] > seq and e["decision_type"] in decision_types]

    def test_checkpoint_games_complete(self):
        for condition in CONDITIONS:
            with self.subTest(condition=condition):
                validation = validate_events(self.events[condition])
                self.assertTrue(validation["passed"], validation["errors"][:3])
                self.assertEqual(self.results[condition]["outcome"], "solved_retry")
                end = self.kind(condition, "game_end")[0]
                self.assertEqual(end["checkpoints_reached"], 2)
                self.assertEqual(end["conversations"], {"detective": 2, "private": 1})
                self.assertEqual([c["exchange_id"] for c in self.conversations(condition, "detective")], ["e6", "e12"])

    def test_ckpt7_conversation_enters_memory(self):
        # CKPT-7: one exchange in memory straight away; CKPT-9: no interrogation turn used.
        for condition in CONDITIONS:
            with self.subTest(condition=condition):
                conversation = self.conversations(condition, "detective")[0]
                window = self.views_after(condition, conversation["seq"])[0]["window_exchange_ids"]
                self.assertIn("e6", window)
                if condition == "none":
                    self.assertEqual(window, ["e6"])
                after = [e for e in self.kind(condition, "interrogation") if e["seq"] > conversation["seq"]][0]
                self.assertEqual((after["interrogation_index"], after["exchange_index"]), (6, 7))
                end = self.kind(condition, "game_end")[0]
                self.assertEqual((end["interrogations_used"], end["exchanges_total"]), (len(CHECKPOINT_QUESTIONS), 13))

    def test_ckpt7_raw_and_ledger_show_the_conversation(self):
        # CKPT-7: the conversation is shown like any exchange: verbatim (raw) or as notes (ledger).
        seq = self.conversations("raw", "detective")[0]["seq"]
        raw = self.views_after("raw", seq)[0]["memory_payload"]
        self.assertRegex(raw, rf"\[e6\]\nBlair: [^\n]*{re.escape(DETECTIVE_MARKER)}[^\n]*\nDetective: ")
        seq = self.conversations("ledger", "detective")[0]["seq"]
        ledger = self.views_after("ledger", seq)[0]["memory_payload"]
        block = ledger.split("[e6] Blair\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("speaker: Detective (your reply)", block)
        self.assertIn(DETECTIVE_MARKER, block)

    def test_ckpt7_conversation_evicts_oldest_when_full(self):
        # CKPT-7: with the window already at k = 8, the conversation pushes the oldest exchange out.
        for condition in ("raw", "ledger"):
            with self.subTest(condition=condition):
                conversation = self.conversations(condition, "detective")[1]
                before = [e for e in self.kind(condition, "memory_view") if e["seq"] < conversation["seq"]][-1]
                self.assertEqual(before["decision_type"], "conversation_reply")
                self.assertEqual(len(before["window_exchange_ids"]), 8)
                after = self.views_after(condition, conversation["seq"])[0]["window_exchange_ids"]
                self.assertEqual(after, before["window_exchange_ids"][1:] + ["e12"])
                self.assertNotIn(before["window_exchange_ids"][0], after)

    def test_matched_horizon_with_conversations(self):
        # SYS-5 with CKPT-7: raw and ledger see the same windows, checkpoint calls and conversations included.
        raw = [(e["decision_type"], e["window_exchange_ids"]) for e in self.kind("raw", "memory_view")]
        ledger = [(e["decision_type"], e["window_exchange_ids"]) for e in self.kind("ledger", "memory_view")]
        self.assertEqual(raw, ledger)
        self.assertTrue(any("e12" in window for _, window in raw))

    def test_ckpt8_private_chat_never_reaches_detective(self):
        # CKPT-8 / ENV-4: a suspect-only conversation stays out of every detective and extractor prompt.
        for condition in CONDITIONS:
            with self.subTest(condition=condition):
                private = self.conversations(condition, "private")
                self.assertEqual([(c["inviter"], c["recipient"], c["exchange_id"]) for c in private],
                                 [("Casey", "Drew", None)])
                self.assertIn(PRIVATE_MARKER, private[0]["message"])          # the marker really was said
                for call in self.kind(condition, "llm_call"):
                    if call["role"] in ("detective", "extractor"):
                        prompt = "\n".join(message["content"] for message in call["messages"])
                        self.assertNotIn(PRIVATE_MARKER, prompt, call["call_id"])
                visible = ({e["exchange_id"] for e in self.kind(condition, "interrogation")}
                           | {c["exchange_id"] for c in self.conversations(condition, "detective")})
                for view in self.kind(condition, "memory_view"):
                    self.assertNotIn(PRIVATE_MARKER, view["memory_payload"])
                    self.assertLessEqual(set(view["window_exchange_ids"]), visible)

    def test_ckpt8_sanity_detective_marker_visible(self):
        # CKPT-8 sanity: the test can see conversations, so the private check above is meaningful.
        for condition in CONDITIONS:
            with self.subTest(condition=condition):
                self.assertTrue(any(DETECTIVE_MARKER in view["memory_payload"]
                                    for view in self.kind(condition, "memory_view")))


if __name__ == "__main__":
    unittest.main()

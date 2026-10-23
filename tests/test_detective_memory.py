"""Tests for detective/memory.py (build_memory, extract_ledger). Run from repo root:
    python -m unittest tests.test_detective_memory -v
Offline (MockBackend). Fake exchanges use the controller's exchange dict
(exchange_id, kind, matched_claim_ids, speaker, text). The pilot case is loaded only to check leakage.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework import load_case
from detective import SCHEMA_CONFIG, build_memory, extract_ledger
from detective.prompts import (EMPTY_MEMORY, EXTRACTION_PROMPT, EXTRACTOR_VERSION, MEMORY_LABELS,
                               render_ledger_block)
from infra import LLMCallError, LLMClient, MockBackend, TransportError

CASE = load_case(ROOT / "cases" / "pilot.json")
CLAIM_IDS = list(CASE["claims"])


def exchange(index, speaker, question, answer, kind="interrogation"):
    return {"exchange_id": f"e{index}", "kind": kind, "matched_claim_ids": CLAIM_IDS[index - 1:index],
            "speaker": speaker, "text": f"Detective: {question}\n{speaker}: {answer}"}


GAME = [exchange(1, "Blair", "Where were you at 20:30?", "I was in the Library at 20:30."),
        exchange(2, "Drew", "Did you see Blair?", "The recording shows Blair in the Gallery at 20:30. Ask Alex about the floor."),
        exchange(3, "Blair", "Drew saw you in the Gallery.", "I take it back: I was not in the Library at 20:30.")]
NOT_ELIGIBLE = [dict(exchange(9, "Casey", "-", "Private chat."), kind="suspect_conversation", exchange_id="x1"),
                dict(exchange(9, "Ellis", "-", "Invite."), kind="invitation", exchange_id="x2")]


def entry(type_, speaker, statement, source_span, entities=(), time=None, target=None, topic=None, retracts=False):
    return {"type": type_, "speaker": speaker, "statement": statement, "entities": list(entities), "time": time,
            "target": target, "topic": topic, "retracts": retracts, "source_span": source_span}


LEDGER = {
    "e1": [dict(entry("claim", "Blair", "Blair was in the Library at 20:30.", "I was in the Library at 20:30.",
                      ["Blair", "Library"], "20:30"), exchange_id="e1")],
    "e2": [dict(entry("observation", "Drew", "The recording shows Blair in the Gallery at 20:30.",
                      "The recording shows Blair in the Gallery at 20:30.", ["Blair", "Gallery"], "20:30"),
                exchange_id="e2"),
           dict(entry("unresolved_lead", "Drew", "Ask Alex about the floor.", "Ask Alex about the floor.",
                      ["Alex"], target="Alex", topic="the floor"), exchange_id="e2")],
    "e3": [dict(entry("claim", "Blair", "Blair was not in the Library at 20:30.",
                      "I was not in the Library at 20:30.", ["Blair", "Library"], "20:30", retracts=True),
                exchange_id="e3")],
}


class Recorder:
    """Minimal stand-in for EventLog: records emit() calls."""

    def __init__(self):
        self.events = []

    def emit(self, kind, **payload):
        self.events.append(dict(payload, kind=kind))


def client(script):
    backend = MockBackend(script)
    log = Recorder()
    return LLMClient(backend, model_id="mock", decoding={}, log=log, sleep=lambda seconds: None), backend, log


class WindowTests(unittest.TestCase):
    def test_none_is_last_eligible_exchange(self):
        memory = build_memory("none", GAME + NOT_ELIGIBLE, LEDGER, 8)
        self.assertEqual(memory["window_exchange_ids"], ["e3"])
        self.assertTrue(memory["payload"].startswith(MEMORY_LABELS["none"] + "\n"))

    def test_raw_and_ledger_last_k_with_eviction(self):
        for condition in ("raw", "ledger"):
            self.assertEqual(build_memory(condition, GAME, LEDGER, 8)["window_exchange_ids"], ["e1", "e2", "e3"])
            self.assertEqual(build_memory(condition, GAME, LEDGER, 2)["window_exchange_ids"], ["e2", "e3"])

    def test_ineligible_kinds_excluded(self):
        mixed = [GAME[0], NOT_ELIGIBLE[0], GAME[1], NOT_ELIGIBLE[1],
                 dict(GAME[2], kind="detective_conversation")]
        for condition in ("none", "raw", "ledger"):
            memory = build_memory(condition, mixed, LEDGER, 8)
            self.assertNotIn("x1", memory["window_exchange_ids"])
            self.assertNotIn("x2", memory["window_exchange_ids"])
            self.assertNotIn("Private chat.", memory["payload"])
            self.assertNotIn("Invite.", memory["payload"])
        self.assertEqual(build_memory("raw", mixed, LEDGER, 8)["window_exchange_ids"], ["e1", "e2", "e3"])

    def test_matched_horizon(self):
        for k in (1, 2, 3, 8):
            self.assertEqual(build_memory("raw", GAME, LEDGER, k)["window_exchange_ids"],
                             build_memory("ledger", GAME, LEDGER, k)["window_exchange_ids"])

    def test_visible_exchanges_are_id_and_text_only(self):
        for condition in ("none", "raw", "ledger"):
            memory = build_memory(condition, GAME, LEDGER, 2)
            expected = {ex["exchange_id"]: ex["text"] for ex in GAME[-len(memory["window_exchange_ids"]):]}
            self.assertEqual(memory["visible_exchanges"], expected)

    def test_invalid_condition_or_k(self):
        for bad in [("full", 8), ("raw", 0), ("raw", -1), ("raw", 2.0), ("raw", "8"), ("raw", True)]:
            with self.assertRaises(ValueError):
                build_memory(bad[0], GAME, LEDGER, bad[1])


class PayloadTests(unittest.TestCase):
    def test_no_claim_ids_in_payload(self):
        for condition in ("none", "raw", "ledger"):
            payload = build_memory(condition, GAME, LEDGER, 8)["payload"]
            self.assertNotIn("matched_claim_ids", payload)
            for claim_id in CLAIM_IDS:
                self.assertNotIn(claim_id, payload)

    def test_raw_payload_format(self):
        payload = build_memory("raw", GAME[:2], LEDGER, 8)["payload"]
        self.assertEqual(payload, MEMORY_LABELS["raw"].format(k=8) + "\n[e1]\n" + GAME[0]["text"]
                         + "\n\n[e2]\n" + GAME[1]["text"])

    def test_ledger_shows_only_window_entries(self):
        payload = build_memory("ledger", GAME, LEDGER, 2)["payload"]
        self.assertNotIn("[e1]", payload)
        self.assertNotIn('"I was in the Library at 20:30."', payload)
        self.assertIn("[e2] Drew", payload)
        self.assertIn('- unresolved lead | speaker: Drew | ask: Alex | topic: the floor | "Ask Alex about the floor."',
                      payload)
        self.assertIn('- (retraction) claim | speaker: Blair | retracts: yes | "I was not in the Library at 20:30."',
                      payload)

    def test_ledger_unknowns_missing_and_failed(self):
        ledger = {"e1": [dict(entry("claim", "Blair", "Something.", "I was"), exchange_id="e1")],
                  "e3": [{"type": "extraction_failure", "exchange_id": "e3", "reason": "schema failure after one repair"}]}
        payload = build_memory("ledger", GAME, ledger, 8)["payload"]
        self.assertIn('- claim | speaker: Blair | entities: unknown | time: unknown | "I was"', payload)
        self.assertIn("[e2] Drew\n(no notes)", payload)
        self.assertIn("[e3] Blair\n(extraction failed for this exchange)", payload)

    def test_empty_memory(self):
        for condition in ("none", "raw", "ledger"):
            memory = build_memory(condition, NOT_ELIGIBLE, {}, 8)
            self.assertEqual(memory["payload"], MEMORY_LABELS[condition].format(k=8) + "\n" + EMPTY_MEMORY)
            self.assertEqual((memory["window_exchange_ids"], memory["visible_exchanges"]), ([], {}))


class ExtractionTests(unittest.TestCase):
    def test_happy_path_spans(self):
        reply = {"entries": [entry("observation", "Drew", "Blair was in the Gallery at 20:30.",
                                   "The recording shows Blair in the Gallery at 20:30.", ["Blair", "Gallery"], "20:30"),
                             entry("unresolved_lead", "Drew", "Ask Alex about the floor.", "Ask Alex about the floor.",
                                   ["Alex"], target="Alex", topic="the floor")]}
        llm, _, log = client([reply])
        result = extract_ledger(GAME[1], llm, "g1")
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["failure_entry"])
        self.assertEqual(result["call_id"], log.events[0]["call_id"])
        for item in result["entries"]:
            self.assertEqual(item["exchange_id"], "e2")
            self.assertTrue(item["span_verified"])
            self.assertEqual(GAME[1]["text"][item["span"]["start"]:item["span"]["end"]], item["source_span"])
        lead = result["entries"][1]
        self.assertEqual((lead["speaker"], lead["target"], lead["topic"]), ("Drew", "Alex", "the floor"))

    def test_bad_span_kept_unverified(self):
        llm, _, _ = client([{"entries": [entry("claim", "Blair", "In the Library.", "I was in the library all night")]}])
        result = extract_ledger(GAME[0], llm, "g1")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual((result["entries"][0]["span"], result["entries"][0]["span_verified"]), (None, False))

    def test_retracts_kept(self):
        llm, _, _ = client([{"entries": [entry("claim", "Blair", "Not in the Library.", "I was not in the Library at 20:30.",
                                               ["Library"], "20:30", retracts=True)]}])
        self.assertTrue(extract_ledger(GAME[2], llm, "g1")["entries"][0]["retracts"])

    def test_exchange_id_from_exchange_not_model(self):
        llm, _, _ = client([{"entries": [entry("claim", "Blair", "x", "I was")]}])
        self.assertEqual(extract_ledger(GAME[0], llm, "g1")["entries"][0]["exchange_id"], "e1")

    def test_prompt_has_exchange_but_no_case_data(self):
        llm, backend, _ = client([{"entries": []}])
        extract_ledger(GAME[1], llm, "g1")
        prompt = "\n".join(message["content"] for message in backend.requests[0]["messages"])
        self.assertIn(GAME[1]["text"], prompt)
        self.assertNotIn(GAME[0]["text"], prompt)
        self.assertNotIn("matched_claim_ids", prompt)
        for item_id in set(CASE["claims"]) | set(CASE["facts"]) | set(CASE["leads"]):
            self.assertNotIn(item_id, prompt)
        for spec in CASE["suspects"].values():
            self.assertNotIn(spec["motive"], prompt)
            self.assertNotIn(spec["secret"], prompt)

    def test_repaired_status(self):
        llm, _, _ = client(["not json", {"entries": []}])
        self.assertEqual(extract_ledger(GAME[0], llm, "g1")["status"], "repaired")

    def test_two_invalid_replies_fail_with_entry(self):
        llm, _, log = client(["not json", {"entries": [{"type": "guess"}]}])
        result = extract_ledger(GAME[0], llm, "g1")
        self.assertEqual((result["status"], result["entries"]), ("failed", []))
        self.assertEqual(result["call_id"], log.events[0]["call_id"])
        self.assertEqual(result["failure_entry"], {"type": "extraction_failure", "exchange_id": "e1",
                                                   "reason": "schema failure after one repair"})

    def test_transport_failure_raises(self):
        llm, _, _ = client([TransportError("down")] * 4)
        with self.assertRaises(LLMCallError):
            extract_ledger(GAME[0], llm, "g1")

    def test_logged_call_fields(self):
        llm, _, log = client([{"entries": []}])
        extract_ledger(GAME[0], llm, "g1")
        event = log.events[0]
        self.assertEqual((event["role"], event["agent_id"], event["purpose"], event["phase"]),
                         ("extractor", "extractor", "ledger_extraction", "through_first_accusation"))
        self.assertEqual(event["max_output_tokens"], SCHEMA_CONFIG["max_extraction_tokens"])
        self.assertEqual(event["game_id"], "g1")

    def test_referral_round_trip(self):
        lead = entry("unresolved_lead", "Drew", "Ask Alex about the floor.", "Ask Alex about the floor.",
                     ["Alex"], target="Alex", topic="the floor")
        llm, _, _ = client([{"entries": [lead]}])
        result = extract_ledger(GAME[1], llm, "g1")
        kept = result["entries"][0]
        self.assertEqual((kept["speaker"], kept["target"], kept["topic"]), ("Drew", "Alex", "the floor"))
        payload = build_memory("ledger", GAME[:2], {"e2": result["entries"]}, 8)["payload"]
        self.assertIn("ask: Alex | topic: the floor", payload)

    def test_extraction_prompt_v2(self):
        self.assertEqual(EXTRACTOR_VERSION, "extractor-v2")
        self.assertIn('Lines starting with "Detective:" are the detective\'s own words.', EXTRACTION_PROMPT)
        self.assertIn('with speaker exactly "Detective"', EXTRACTION_PROMPT)
        self.assertIn("The detective's questions are never unresolved leads.", EXTRACTION_PROMPT)
        self.assertIn("Extract every explicit claim, observation, refusal and unresolved lead", EXTRACTION_PROMPT)


class DetectiveLabelTests(unittest.TestCase):
    ENTRIES = [entry("claim", " detective ", "Drew saw Blair in the Gallery.", "Drew saw you in the Gallery.",
                     ["Drew", "Blair", "Gallery"]),
               LEDGER["e3"][0]]

    def test_question_label_in_interrogation(self):
        block = render_ledger_block(GAME[2], self.ENTRIES)
        self.assertIn("- claim | speaker: Detective (your question) | entities: Drew, Blair, Gallery", block)
        self.assertIn('- (retraction) claim | speaker: Blair | retracts: yes | "I was not in the Library at 20:30."',
                      block)

    def test_reply_label_in_detective_conversation(self):
        block = render_ledger_block(dict(GAME[2], kind="detective_conversation"), self.ENTRIES)
        self.assertIn("speaker: Detective (your reply)", block)
        self.assertNotIn("(your question)", block)

    def test_suspect_entries_unchanged(self):
        for kind in ("interrogation", "detective_conversation"):
            block = render_ledger_block(dict(GAME[1], kind=kind), LEDGER["e2"])
            self.assertNotIn("(your", block)
            self.assertIn('- unresolved lead | speaker: Drew | ask: Alex | topic: the floor | "Ask Alex about the floor."',
                          block)


if __name__ == "__main__":
    unittest.main()

"""Offline tests for Aniroodh's infra handoff; python -m unittest discover -s tests -v. No network."""

import copy
import importlib.util
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from casework import load_case
from casework.diagnostics import eligible_exchanges, evidence_diagnostics, contradiction_diagnostics
from infra import (LLMClient, MockBackend, GeminiBackend, OpenRouterBackend, TransportError, LLMCallError,
                   EventLog, read_events, suspect_agent_ids,
                   validate_events, trace_prefix_before_first_accusation, to_annotation_exchanges,
                   summarise_cost, REQUIRED_KEYS)
from infra.llm import REPAIR_INSTRUCTION, check_shape, parse_structured

ROOT = Path(__file__).resolve().parents[1]
JOSH_KEYS = ["exchange_id", "kind", "speaker", "delivered_response", "claim_ids", "annotations"]
DECODING = {"temperature": 0.0, "max_output_tokens": 64}
ACCEPT = {"type": "object", "required": ["accept"], "properties": {"accept": {"type": "boolean"}},
          "additionalProperties": False}


def load_example_module():
    spec = importlib.util.spec_from_file_location("infra_example", ROOT / "examples" / "infra_example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Recorder:
    """Minimal stand-in for EventLog: records emit() calls."""

    def __init__(self):
        self.events = []

    def emit(self, kind, **payload):
        self.events.append({"kind": kind, **payload})
        return self.events[-1]


def make_client(script, **kwargs):
    log = Recorder()
    ticks = itertools.count()
    sleeps = []
    client = LLMClient(MockBackend(script), "mock-model", DECODING, log=log, clock=lambda: float(next(ticks)),
                       sleep=sleeps.append, **kwargs)
    return client, log, sleeps


def call(client, **overrides):
    arguments = {"role": "suspect", "agent_id": "suspect_2", "purpose": "interrogation_response",
                 "phase": "through_first_accusation",
                 "messages": [{"role": "user", "content": "Where were you?"}], "game_id": "g1"}
    arguments.update(overrides)
    return client.call(**arguments)


class WrapperTests(unittest.TestCase):
    def test_call_logs_one_record_with_usage_and_cost(self):
        pricing = {"input_per_million": 1.0, "output_per_million": 2.0, "basis": "test"}
        client, log, _ = make_client(["I was in the Library.", "second answer"], pricing=pricing)
        record = call(client)
        self.assertEqual(record["text"], "I was in the Library.")
        self.assertEqual((record["call_id"], record["status"], record["parsed"]), ("c000001", "ok", None))
        self.assertEqual([event["kind"] for event in log.events], ["llm_call"])
        self.assertEqual(log.events[0]["call_id"], "c000001")
        usage = record["usage"]
        self.assertEqual(usage["total_tokens"], usage["input_tokens"] + usage["output_tokens"])
        self.assertAlmostEqual(record["cost_usd"], (usage["input_tokens"] * 1.0 + usage["output_tokens"] * 2.0) / 1e6)
        self.assertEqual([a["kind"] for a in record["attempts"]], ["initial"])
        self.assertEqual(record["latency_ms"], 1000)
        self.assertEqual(call(client)["call_id"], "c000002")

    def test_agent_id_defaults_for_singleton_roles_and_is_logged(self):
        client, log, _ = make_client(["q", "a", "e"])
        detective = call(client, role="detective", purpose="question", agent_id=None)
        self.assertEqual(detective["agent_id"], "detective")
        self.assertEqual(call(client, agent_id="suspect_3")["agent_id"], "suspect_3")
        extractor = call(client, role="extractor", purpose="ledger_extraction", agent_id=None)
        self.assertEqual(extractor["agent_id"], "extractor")
        self.assertEqual([event["agent_id"] for event in log.events], ["detective", "suspect_3", "extractor"])
        names = ["Alex", "Blair", "Casey", "Drew", "Ellis"]
        self.assertEqual(suspect_agent_ids(names), {"Alex": "suspect_1", "Blair": "suspect_2", "Casey": "suspect_3",
                                                    "Drew": "suspect_4", "Ellis": "suspect_5"})
        with self.assertRaises(ValueError):
            suspect_agent_ids(names[:4])

    def test_game_loop_vocabulary_is_accepted(self):
        # Harish's decide() logs one detective_decision call and a memory_view of type "decision";
        # Aravindh tags the controller-side repair with repair_of.
        client, log, _ = make_client(["{\"action\": \"interrogate\"}", "{\"action\": \"accuse\"}"])
        first = call(client, role="detective", purpose="detective_decision", agent_id=None)
        call(client, role="detective", purpose="detective_decision", agent_id=None,
             tags={"repair_of": first["call_id"]})
        self.assertEqual([e["purpose"] for e in log.events], ["detective_decision"] * 2)
        events = [{"seq": 1, "ts": "t", "game_id": "g1", "kind": "game_start", "schema_version": "1.0", "run_id": "r",
                   "case_id": "c", "case_sha256": "x", "condition": "raw", "k": 5, "turn_budget": 20,
                   "accusation_budget": 3, "seed": 1, "model_id": "m", "decoding": {}, "prompt_versions": {},
                   "pricing": None, "manifest_sha256": None, "code_version": None},
                  {"seq": 2, "ts": "t", "game_id": "g1", "kind": "llm_call", **log.events[0]},
                  {"seq": 3, "ts": "t", "game_id": "g1", "kind": "memory_view", "decision_index": 1,
                   "decision_type": "decision", "window_exchange_ids": [], "memory_payload": [],
                   "call_id": first["call_id"]}]
        self.assertEqual(validate_events(events), {"passed": True, "errors": []})

    def test_no_pricing_means_null_cost_not_zero(self):
        client, _, _ = make_client(["x"])
        self.assertIsNone(call(client)["cost_usd"])

    def test_vocabulary_and_message_shape_are_enforced(self):
        client, log, _ = make_client(["x"])
        for overrides in ({"role": "judge"}, {"purpose": "chat"}, {"phase": "warmup"}, {"purpose": "contradiction_report"},
                          {"agent_id": None}, {"agent_id": ""}, {"agent_id": "Blair"}, {"agent_id": "suspect_9"},
                          {"role": "detective", "purpose": "question", "agent_id": "suspect_1"},
                          {"messages": [{"role": "tool", "content": "x"}]}, {"messages": [{"role": "user"}]}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                call(client, **overrides)
        self.assertEqual(log.events, [])

    def test_transport_retries_are_bounded_then_logged_and_raised(self):
        client, log, sleeps = make_client([TransportError("503")] * 4, max_transport_retries=3)
        with self.assertRaises(LLMCallError) as context:
            call(client)
        record = context.exception.record
        self.assertEqual(record["status"], "transport_failure")
        self.assertEqual([a["kind"] for a in record["attempts"]], ["initial"] + ["transport_retry"] * 3)
        self.assertTrue(all(a["status"] == "transport_error" for a in record["attempts"]))
        self.assertEqual(sleeps, [1, 2, 4])
        self.assertEqual(log.events[0]["status"], "transport_failure")
        self.assertEqual(record["usage"]["total_tokens"], 0)

    def test_transport_retry_then_success_keeps_both_attempts(self):
        client, _, sleeps = make_client([TransportError("429"), "fine"])
        record = call(client)
        self.assertEqual(record["status"], "ok")
        self.assertEqual([a["status"] for a in record["attempts"]], ["transport_error", "ok"])
        self.assertEqual(sleeps, [1])
        self.assertEqual(record["attempts"][0]["usage"]["total_tokens"], 0)
        self.assertGreater(record["usage"]["total_tokens"], 0)

    def test_structured_output_is_parsed_even_in_code_fences(self):
        client, _, _ = make_client(['```json\n{"accept": true}\n```'])
        record = call(client, response_schema=ACCEPT)
        self.assertEqual(record["parsed"], {"accept": True})
        self.assertFalse(record["repair_used"])

    def test_format_repair_happens_once_then_fails(self):
        client, log, _ = make_client(["not json", "still not json"])
        with self.assertRaises(LLMCallError) as context:
            call(client, response_schema=ACCEPT)
        record = context.exception.record
        self.assertEqual(record["status"], "format_failure")
        self.assertEqual([a["kind"] for a in record["attempts"]], ["initial", "format_repair"])
        self.assertEqual([a["status"] for a in record["attempts"]], ["format_error", "format_error"])
        self.assertTrue(record["repair_used"])
        repair_request = client.backend.requests[1]["messages"]
        self.assertEqual([m["role"] for m in repair_request], ["user", "assistant", "user"])
        self.assertEqual(repair_request[1]["content"], "not json")
        self.assertTrue(repair_request[2]["content"].startswith(REPAIR_INSTRUCTION))
        self.assertEqual(len(log.events), 1)

    def test_format_repair_recovers_and_sums_usage(self):
        client, _, _ = make_client(["oops", {"accept": False}])
        record = call(client, response_schema=ACCEPT)
        self.assertEqual(record["parsed"], {"accept": False})
        self.assertTrue(record["repair_used"])
        self.assertEqual(record["usage"]["total_tokens"],
                         sum(a["usage"]["total_tokens"] for a in record["attempts"]))
        self.assertEqual(record["latency_ms"], 2000)

    def test_shape_check_and_parsing(self):
        triple = {"type": "object", "required": ["culprit", "weapon", "location"],
                  "properties": {"culprit": {"enum": ["Alex", "Blair"]}, "weapon": {"type": "string"},
                                 "location": {"type": "string"}}, "additionalProperties": False}
        self.assertEqual(check_shape({"culprit": "Alex", "weapon": "Knife", "location": "Library"}, triple), [])
        errors = check_shape({"culprit": "Zed", "weapon": "Knife", "extra": 1}, triple)
        self.assertEqual(len(errors), 3)
        self.assertEqual(check_shape(True, {"type": "integer"}), ["$: expected type integer"])
        self.assertEqual(check_shape([1, "a"], {"type": "array", "items": {"type": "integer"}}),
                         ["$[1]: expected type integer"])
        parsed, errors = parse_structured('Sure! {"accept": true} hope that helps', ACCEPT)
        self.assertEqual((parsed, errors), ({"accept": True}, []))
        parsed, errors = parse_structured("no braces here", ACCEPT)
        self.assertIsNone(parsed)
        self.assertTrue(errors[0].startswith("not JSON"))

    def test_gemini_backend_needs_a_key_before_any_network(self):
        backend = GeminiBackend(api_key_env="NO_SUCH_KEY_VAR_FOR_TESTS")
        with self.assertRaises(RuntimeError):
            backend.generate(model_id="m", messages=[{"role": "user", "content": "hi"}], decoding={})

    def test_openrouter_backend_needs_a_key_before_any_network(self):
        backend = OpenRouterBackend(api_key_env="NO_SUCH_KEY_VAR_FOR_TESTS")
        with self.assertRaises(RuntimeError):
            backend.generate(model_id="m", messages=[{"role": "user", "content": "hi"}], decoding={})

    def test_openrouter_backend_build_request_shape(self):
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        body = OpenRouterBackend.build_request("google/gemini-3.1-flash-lite", messages,
                                               {"temperature": 0.7, "top_p": 0.95}, max_output_tokens=100)
        self.assertEqual(body["model"], "google/gemini-3.1-flash-lite")
        self.assertEqual(body["messages"], messages)  # OpenAI role names pass through unchanged
        self.assertEqual(body["temperature"], 0.7)
        self.assertEqual(body["top_p"], 0.95)
        self.assertEqual(body["max_tokens"], 100)
        self.assertNotIn("response_format", body)

    def test_openrouter_backend_build_request_includes_json_schema(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        body = OpenRouterBackend.build_request("m", [{"role": "user", "content": "hi"}], {},
                                               response_schema=schema)
        self.assertEqual(body["response_format"],
                         {"type": "json_schema", "json_schema": {"name": "response", "strict": True, "schema": schema}})

    def test_openrouter_backend_parse_response(self):
        payload = {"choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": "google/gemini-3.1-flash-lite"}
        parsed = OpenRouterBackend.parse_response(payload)
        self.assertEqual(parsed, {"text": '{"a": 1}', "finish_reason": "stop",
                                 "usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 0},
                                 "model_version": "google/gemini-3.1-flash-lite"})

    def test_openrouter_backend_is_transport_error_for_retryable_status(self):
        class FakeResponse:
            status_code = 429

        class FakeHTTPStatusError(Exception):
            response = FakeResponse()

        self.assertTrue(OpenRouterBackend.is_transport_error(FakeHTTPStatusError()))


class EventLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = load_case(ROOT / "cases" / "pilot.json")
        cls.example = load_example_module()
        cls.temporary = tempfile.TemporaryDirectory(prefix="infra_test_", dir=ROOT)
        cls.events = cls.example.run_mock_game(Path(cls.temporary.name) / "game.jsonl", cls.case)
        cls.fixture = json.loads((ROOT / "examples" / "annotation_example.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_envelope_required_keys_and_round_trip(self):
        path = Path(self.temporary.name) / "envelope.jsonl"
        with EventLog(path, "g1", clock=lambda: "T0") as log:
            with self.assertRaises(ValueError):
                log.emit("not_a_kind", x=1)
            with self.assertRaises(ValueError):
                log.emit("failure", stage="transport")
            with self.assertRaises(ValueError):
                log.emit("failure", stage="transport", reason="r", call_id=None, detail=None, seq=9)
            with self.assertRaises(ValueError):
                log.emit("failure", stage="transport", reason="r", call_id=None, detail=None, game_id="other")
            event = log.failure(stage="transport", reason="retries exhausted")
        self.assertEqual((event["seq"], event["ts"], event["game_id"], event["kind"]), (1, "T0", "g1", "failure"))
        self.assertEqual(read_events(path), log.events)

    def test_example_is_valid_and_validator_catches_structural_errors(self):
        self.assertEqual(validate_events(self.events), {"passed": True, "errors": []})
        index = {event["kind"]: event["seq"] - 1 for event in self.events}

        def mutated(change):
            events = copy.deepcopy(self.events)
            change(events)
            return validate_events(events)

        def set_exchange(events, old, new):
            for event in events:
                if event["kind"] == "interrogation" and event["exchange_id"] == old:
                    event["exchange_id"] = new

        cases = {
            "duplicate or non-contiguous exchange": lambda e: set_exchange(e, "e2", "e1"),
            "unknown kind": lambda e: e[index["interrogation"]].update(kind="chat"),
            "missing key": lambda e: e[index["interrogation"]].pop("response"),
            "testimony after accusation": lambda e: e.insert(index["accusation"] + 1,
                                                             copy.deepcopy(e[index["interrogation"]])),
            "game_end not last": lambda e: e.append(copy.deepcopy(e[index["failure"]] if "failure" in index
                                                                  else e[index["checkpoint_end"]])),
            "private conversation with exchange_id": lambda e: [ev.update(exchange_id="e9") for ev in e
                                                                  if ev["kind"] == "conversation"
                                                                  and ev["visibility"] == "private"],
            "dangling call id": lambda e: e[index["interrogation"]].update(response_call_id="c999999"),
            "bad seq": lambda e: e[3].update(seq=99),
            "game_start not first": lambda e: e.insert(0, copy.deepcopy(e[index["checkpoint_end"]])),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                report = mutated(change)
                self.assertFalse(report["passed"], name)
                self.assertTrue(report["errors"], name)

    def test_contradiction_report_cites_the_first_accusation_call(self):
        report = next(e for e in self.events if e["kind"] == "contradiction_report")
        first = next(e for e in self.events if e["kind"] == "accusation")
        self.assertEqual(report["call_id"], first["call_id"])
        self.assertLess(report["seq"], first["seq"])
        call_record = next(e for e in self.events if e["kind"] == "llm_call" and e["call_id"] == first["call_id"])
        self.assertEqual(call_record["purpose"], "accusation")
        self.assertIsNone(call_record["parsed"])  # decisions.md D1: detective calls carry no response_schema
        self.assertEqual(report["reported_pairs_raw"], json.loads(call_record["text"])["contradiction_report"])
        self.assertNotIn("contradiction_report", {e["purpose"] for e in self.events if e["kind"] == "llm_call"})

    def test_prefix_stops_before_first_accusation(self):
        prefix = trace_prefix_before_first_accusation(self.events)
        self.assertNotIn("accusation", [event["kind"] for event in prefix])
        # After the prefix: accusation 1, then (llm_call, memory_view, accusation) for attempt 2, then game_end.
        self.assertEqual(len(self.events) - len(prefix), 5)
        self.assertEqual(self.events[len(prefix)]["kind"], "accusation")

    def test_export_has_exactly_joshs_keys_in_play_order(self):
        export = to_annotation_exchanges(self.events)
        self.assertEqual(list(export), ["exchanges", "reported_pairs"])
        for entry in export["exchanges"]:
            self.assertEqual(list(entry), JOSH_KEYS)
            self.assertEqual((entry["claim_ids"], entry["annotations"]), ([], []))
        self.assertEqual([e["exchange_id"] for e in export["exchanges"]],
                         ["e1", "e2", "inv1", "e3", "inv2", "p1_msg", "p1_reply"])
        self.assertEqual([e["kind"] for e in export["exchanges"]],
                         ["interrogation", "interrogation", "invitation", "detective_conversation", "invitation",
                          "suspect_conversation", "suspect_conversation"])
        self.assertEqual(export["reported_pairs"], [])
        self.assertEqual([e["exchange_id"] for e in eligible_exchanges(export["exchanges"], "raw", 2)], ["e2", "e3"])
        self.assertEqual([e["exchange_id"] for e in eligible_exchanges(export["exchanges"], "none", 5)], ["e3"])

    def test_overlay_reproduces_joshs_fixture_exactly(self):
        export = to_annotation_exchanges(self.events, self.example.OVERLAY)
        visible = [e for e in export["exchanges"] if e["exchange_id"] in {"e1", "e2", "e3"}]
        self.assertEqual(visible, self.fixture["exchanges"])
        self.assertEqual(export["reported_pairs"], self.fixture["reported_pairs"])

    def test_overlay_errors_are_loud(self):
        overlay = copy.deepcopy(self.example.OVERLAY)
        overlay["exchanges"]["e1"]["annotations"][0]["source_span"]["end"] = 5
        with self.assertRaises(ValueError):
            to_annotation_exchanges(self.events, overlay)
        overlay = copy.deepcopy(self.example.OVERLAY)
        overlay["exchanges"]["e42"] = {"claim_ids": [], "annotations": []}
        with self.assertRaises(ValueError):
            to_annotation_exchanges(self.events, overlay)
        overlay = copy.deepcopy(self.example.OVERLAY)
        overlay["reported_pairs"][0]["left"]["exchange_id"] = "e42"
        with self.assertRaises(ValueError):
            to_annotation_exchanges(self.events, overlay)
        with self.assertRaises(ValueError):
            to_annotation_exchanges(self.events, claim_ids_from="guess")

    def test_controller_bookkeeping_mode_is_separate_from_annotations(self):
        export = to_annotation_exchanges(self.events, claim_ids_from="controller_bookkeeping")
        by_id = {e["exchange_id"]: e for e in export["exchanges"]}
        self.assertEqual(by_id["e1"]["claim_ids"], ["claim_cover_Blair"])
        self.assertEqual(by_id["e3"]["claim_ids"], ["claim_badge_trace"])
        self.assertEqual(by_id["p1_msg"]["claim_ids"], [])
        self.assertTrue(all(e["annotations"] == [] for e in export["exchanges"]))

    def test_round_trip_through_joshs_adapters(self):
        export = to_annotation_exchanges(self.events, self.example.OVERLAY)
        conflicts = contradiction_diagnostics(self.case, export["exchanges"], export["reported_pairs"], "ledger", 3)
        self.assertEqual((conflicts["precision"], conflicts["recall"]), (1.0, 1.0))
        coverage = evidence_diagnostics(self.case, export["exchanges"], "ledger", 3)
        self.assertEqual((coverage["coverage_numerator"], coverage["coverage_denominator"]), (2, 10))
        self.assertEqual(coverage["approved_false_claims_disclosed"], ["claim_cover_Blair"])
        self.assertEqual(coverage["source_window_exchange_ids"], ["e1", "e2", "e3"])
        none = contradiction_diagnostics(self.case, export["exchanges"], export["reported_pairs"], "none", 3)
        self.assertEqual(none["observable_gold_pair_count"], 0)

    def test_checked_in_example_regenerates_identically(self):
        regenerated = Path(self.temporary.name) / "regenerated.jsonl"
        self.example.run_mock_game(regenerated, self.case)
        self.assertEqual(regenerated.read_text(encoding="utf-8"),
                         (ROOT / "examples" / "event_log_example.jsonl").read_text(encoding="utf-8"))
        overlay = json.loads((ROOT / "examples" / "annotation_overlay_example.json").read_text(encoding="utf-8"))
        self.assertEqual(overlay, self.example.OVERLAY)
        self.assertEqual(read_events(ROOT / "examples" / "event_log_example.jsonl"), self.events)

    def test_schema_mirrors_required_keys(self):
        schema = json.loads((ROOT / "schemas" / "event_log.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["properties"]["kind"]["enum"]), set(REQUIRED_KEYS))
        seen = {}
        for block in schema["allOf"]:
            kind = block["if"]["properties"]["kind"]["const"]
            seen[kind] = set(block["then"]["required"])
        self.assertEqual(seen, {kind: set(keys) for kind, keys in REQUIRED_KEYS.items()})
        for event in self.events:
            self.assertIn(event["kind"], schema["properties"]["kind"]["enum"])
            self.assertTrue(set(schema["required"]) <= set(event))

    def test_cost_summary_is_consistent(self):
        summary = summarise_cost(self.events)
        calls = [event for event in self.events if event["kind"] == "llm_call"]
        self.assertEqual(summary["calls"], len(calls))
        self.assertEqual(summary["total_tokens"], sum(c["usage"]["total_tokens"] for c in calls))
        self.assertEqual(sum(b["total_tokens"] for b in summary["by_phase"].values()), summary["total_tokens"])
        self.assertEqual(sum(b["total_tokens"] for b in summary["by_attempt_kind"].values()), summary["total_tokens"])
        self.assertAlmostEqual(summary["cost_usd"], sum(c["cost_usd"] for c in calls))
        self.assertEqual(set(summary["by_phase"]), {"through_first_accusation", "accusation_retries"})
        self.assertEqual(set(summary["by_agent"]),
                         {"detective", "extractor", "suspect_2", "suspect_3", "suspect_4", "suspect_5"})
        self.assertEqual(sum(b["calls"] for b in summary["by_agent"].values()), summary["calls"])
        self.assertEqual(summary["calls_without_pricing"], 0)
        self.assertEqual(summary["controller_repair_calls"], {"calls": 0, "total_tokens": 0})
        client, log, _ = make_client(["bad", "good"])
        first = call(client, role="detective", purpose="question", agent_id=None)
        call(client, role="detective", purpose="question", agent_id=None, tags={"repair_of": first["call_id"]})
        repairs = summarise_cost([{"kind": "llm_call", **event} for event in log.events])["controller_repair_calls"]
        self.assertEqual(repairs["calls"], 1)
        self.assertGreater(repairs["total_tokens"], 0)
        self.assertEqual(self.events[-1]["cost"], summary)

    def test_example_script_runs_from_the_command_line(self):
        result = subprocess.run([sys.executable, "examples/infra_example.py"], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"precision": 1.0', result.stdout)
        self.assertIn("no API calls were made", result.stdout)


if __name__ == "__main__":
    unittest.main()

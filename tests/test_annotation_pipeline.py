"""Tests for the annotation pipeline: prompt building, schema conversion, one-game
annotation, and many-game orchestration. Uses the real examples/event_log_example.jsonl
fixture and cases/pilot.json throughout -- no invented event shapes.
"""

import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from casework import load_case
from infra import LLMCallError
from infra.eventlog import read_events
from infra.llm import LLMClient, MockBackend, GeminiBackend, OpenRouterBackend

from annotation.prompts import (build_messages, build_user_prompt, candidate_coordination_proposals,
                                candidate_repeat_distances, checkpoints_reached_count,
                                detective_visible_events, memory_k, unflagged_leads)
from annotation.schema import ANNOTATION_API_SCHEMA, load_storage_schema, to_storage_shape, validate_storage_shape
from annotation.pipeline import annotate_game
from annotation.orchestrate import AnnotationRun, annotate_run, select_sample

ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = ROOT / "cases" / "pilot.json"
FIXTURE_LOG = ROOT / "examples" / "event_log_example.jsonl"


def _empty_exchange(exchange_id, citation=None):
    entry = {
        "exchange_id": exchange_id,
        "lead_follow_up": {"newly_disclosed_lead_ids": [], "is_follow_up_to_lead_id": None, "follow_up_relevance": None},
        "corroboration": {"is_request": False, "target_subject": None, "target_time": None},
        "challenge": {"is_challenge": False, "evidence_based": None, "cited_claim_ids": []},
        "repeated_question": {"is_repeat": False, "repeat_kind": None, "matches_exchange_id": None},
        "unsupported_additions": [],
        "withholding": {"explicit_refusal": False, "partial_withholding": False},
        "deception": {"contains_false_statement": False, "authorised": None, "claim_id_if_known": None},
        "confidence": "high",
    }
    if citation:
        entry["citation"] = citation
    return entry


def _valid_api_response(exchange_ids):
    return {
        "exchanges": [_empty_exchange(eid) for eid in exchange_ids],
        "missed_required_disclosures": [],
        "voluntary_interaction": {"checkpoints_reached": 0, "coordination_instances": []},
        "annotator_notes": "mock annotation for tests",
    }


class PromptBuildingTests(unittest.TestCase):
    def setUp(self):
        self.case = load_case(CASE_PATH)
        self.events = read_events(FIXTURE_LOG)

    def test_detective_visible_events_match_the_real_log(self):
        rendered = detective_visible_events(self.events)
        self.assertEqual([e["exchange_id"] for e in rendered], ["e1", "e2", "e3"])
        # question/intent/allowed_branches/instructions must survive, not just delivered_response
        self.assertIn("question", rendered[0])
        self.assertIn("intent", rendered[0])
        self.assertIn("allowed_branches", rendered[0])
        self.assertIn("instructions", rendered[0])
        self.assertEqual(rendered[0]["kind"], "interrogation")
        self.assertTrue(rendered[0]["needs_its_own_exchange_behaviour_entry"])

    def test_detective_conversation_kind_is_not_literally_called_conversation(self):
        # Regression test for a real, recurring bug: the model silently dropped exactly
        # these entries (and only these) from its exchange_behaviour output on multiple
        # real games, most plausibly because the word "conversation" collided with the
        # separate "Suspect-only private conversations" section, which genuinely does
        # NOT get individual entries. e3 in the fixture is a detective-visible
        # conversation; its rendered "kind" must not contain the word "conversation" by
        # itself in a way that could be confused with that other section, and must carry
        # the same explicit needs_its_own_exchange_behaviour_entry flag as interrogations.
        rendered = detective_visible_events(self.events)
        e3 = next(e for e in rendered if e["exchange_id"] == "e3")
        self.assertEqual(e3["kind"], "voluntary_disclosure_to_detective")
        self.assertTrue(e3["needs_its_own_exchange_behaviour_entry"])

    def test_detective_conversation_kind_appears_distinctly_in_the_built_prompt(self):
        prompt = build_user_prompt(self.case, self.events)
        self.assertIn("voluntary_disclosure_to_detective", prompt)
        self.assertIn("needs_its_own_exchange_behaviour_entry", prompt)
        self.assertIn("NOT detective-visible", prompt)  # the private-conversation section header

    def test_condition_label_never_appears_in_the_built_prompt(self):
        prompt = build_user_prompt(self.case, self.events)
        condition = next(e["condition"] for e in self.events if e["kind"] == "game_start")
        self.assertEqual(condition, "ledger")
        self.assertNotIn('"condition"', prompt)
        self.assertNotIn("ledger", prompt.lower())
        # k itself is allowed through (needed to judge expired_evidence_recovery)
        self.assertIn(str(memory_k(self.events)), prompt)

    def test_prompt_carries_private_data_the_detective_never_sees(self):
        prompt = build_user_prompt(self.case, self.events)
        # gold/solution-adjacent private fields must be present for the annotator...
        self.assertIn("approved_false_claim_ids", prompt)
        self.assertIn(self.case["solution"]["culprit"], prompt)  # appears via suspects_private_json's role data

    def test_build_messages_shape(self):
        messages = build_messages(self.case, self.events)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertTrue(all(isinstance(m["content"], str) for m in messages))

    def test_memory_k_returns_the_stored_k_for_raw_and_ledger(self):
        # Fixture is condition="ledger", k=3 -- passed through unchanged.
        self.assertEqual(memory_k(self.events), 3)

    def test_memory_k_resolves_to_1_for_condition_none_regardless_of_stored_k(self):
        # game_start.k is one run-level config value shared across all three conditions
        # (confirmed against real pilot_07b logs: k=8 is stored even on condition="none"
        # games) -- it must not be passed through raw for "none", whose real working
        # window is 1, the same special case casework.diagnostics.eligible_exchanges
        # already applies. Regression test for a real bug: passing the raw stored k
        # through unconditionally caused a live annotator run to mislabel nearly every
        # genuine repeat in a condition=none game as "unnecessary" instead of
        # "expired_evidence_recovery" (qa/labelled_test_set/annotator_accuracy_report.json).
        none_events = [dict(e) for e in self.events]
        none_events[0] = dict(none_events[0], condition="none", k=8)
        self.assertEqual(memory_k(none_events), 1)

    def test_candidate_repeat_distances_finds_no_match_in_the_two_exchange_fixture(self):
        # Fixture has e1 (Blair, whereabouts) and e2 (Drew, corroborate) -- different
        # speakers and intents, so no candidate repeat exists.
        self.assertEqual(candidate_repeat_distances(self.events, memory_k(self.events)), {})

    def test_candidate_repeat_distances_matches_same_speaker_same_intent_only(self):
        # Pure arithmetic over (speaker, intent) equality: e3 repeats e1's exact intent
        # for the same speaker 2 exchanges later; e2 (different speaker) does not match
        # e1 despite e1 existing; e4 (same speaker, different intent) does not match either.
        # k=8 here, and distance=2 <= 8, so suggested_repeat_kind is "unnecessary".
        events = [
            {"kind": "game_start", "condition": "raw", "k": 8},
            {"kind": "interrogation", "exchange_id": "e1", "suspect": "Alex", "intent": {"kind": "overview"}},
            {"kind": "interrogation", "exchange_id": "e2", "suspect": "Blair", "intent": {"kind": "overview"}},
            {"kind": "interrogation", "exchange_id": "e3", "suspect": "Alex", "intent": {"kind": "overview"}},
            {"kind": "interrogation", "exchange_id": "e4", "suspect": "Alex", "intent": {"kind": "ask_lead", "topic": "badge_trace"}},
        ]
        self.assertEqual(candidate_repeat_distances(events, 8), {
            "e3": {"same_speaker_same_structured_intent_previously_at": "e1", "exchanges_since_then": 2,
                  "suggested_repeat_kind": "unnecessary"},
        })

    def test_candidate_repeat_distances_suggests_expired_when_distance_exceeds_k(self):
        # Regression test for a real, severe bug found at scale (main_01, 300 games):
        # even with both exchanges_since_then and the memory window given directly, the
        # model's own distance-vs-window comparison was only 23.6% correct under k=1
        # (and ~71% under k=8), consistently biased toward "unnecessary" even when the
        # true distance clearly exceeded k. suggested_repeat_kind is now pre-computed so
        # the model never has to do this arithmetic itself -- same distance (2) as the
        # test above, but k=1 here means 2 > 1, so the correct suggestion flips to
        # "expired_evidence_recovery".
        events = [
            {"kind": "game_start", "condition": "none", "k": 8},  # stored k is irrelevant; condition=none -> effective k=1
            {"kind": "interrogation", "exchange_id": "e1", "suspect": "Alex", "intent": {"kind": "overview"}},
            {"kind": "interrogation", "exchange_id": "e2", "suspect": "Blair", "intent": {"kind": "overview"}},
            {"kind": "interrogation", "exchange_id": "e3", "suspect": "Alex", "intent": {"kind": "overview"}},
        ]
        self.assertEqual(candidate_repeat_distances(events, memory_k(events)), {
            "e3": {"same_speaker_same_structured_intent_previously_at": "e1", "exchanges_since_then": 2,
                  "suggested_repeat_kind": "expired_evidence_recovery"},
        })

    def test_candidate_repeat_distances_appear_in_the_built_prompt(self):
        events = [
            {"kind": "game_start", "condition": "raw", "k": 8},
            {"kind": "interrogation", "exchange_id": "e1", "suspect": "Alex", "question": "q", "intent": {"kind": "overview"},
             "allowed_branches": [], "instructions": [], "response": "r"},
            {"kind": "interrogation", "exchange_id": "e2", "suspect": "Alex", "question": "q", "intent": {"kind": "overview"},
             "allowed_branches": [], "instructions": [], "response": "r"},
        ]
        prompt = build_user_prompt(self.case, events)
        self.assertIn("same_speaker_same_structured_intent_previously_at", prompt)
        self.assertIn('"suggested_repeat_kind": "unnecessary"', prompt)
        self.assertIn('"exchanges_since_then": 1', prompt)

    def test_checkpoints_reached_count_matches_the_real_fixture(self):
        # Fixture has exactly one checkpoint_start/checkpoint_end pair.
        self.assertEqual(checkpoints_reached_count(self.events), 1)

    def test_checkpoints_reached_count_appears_in_the_built_prompt(self):
        prompt = build_user_prompt(self.case, self.events)
        self.assertIn("Checkpoints reached", prompt)
        idx = prompt.find("Checkpoints reached")
        self.assertIn("1", prompt[idx:idx + 300])

    def test_unflagged_leads_matches_the_real_fixture(self):
        # Fixture delivers claim_witness_Blair (meeting_Blair) at e2 and claim_badge_trace
        # (badge_trace) at the detective-visible conversation e3 -- every other lead's
        # claim never appears in any controller_matched anywhere in this short fixture.
        by_id = {item["lead_id"]: item for item in unflagged_leads(self.events, self.case)}
        self.assertEqual(set(by_id), {"alibi_Blair", "meeting_Alex", "meeting_Casey", "scene_floor",
                                      "scene_wall", "sleeve_trace", "weapon_edge", "weapon_material"})
        # meeting_Alex: owner=Drew, cue_speakers=['Drew'] -- Drew only speaks at e2.
        self.assertEqual(by_id["meeting_Alex"]["re_check_exchange_ids"], ["e2"])
        self.assertFalse(by_id["meeting_Alex"]["owner_or_cue_speaker_never_spoke"])
        # meeting_Casey: owner=Ellis, cue_speakers=['Ellis'] -- Ellis never speaks in this fixture.
        self.assertEqual(by_id["meeting_Casey"]["re_check_exchange_ids"], [])
        self.assertTrue(by_id["meeting_Casey"]["owner_or_cue_speaker_never_spoke"])

    def test_unflagged_leads_never_spoke_flag_is_explicit_not_inferred_from_empty_list(self):
        # Regression test for a real bug: leaving "nobody who could deliver this ever
        # spoke" as merely an empty re_check_exchange_ids list caused the model to read
        # emptiness as "nothing to flag" and silently drop two genuinely missing leads
        # in pilot_07b_none_1118654058 (meeting_Alex, meeting_Blair -- Drew is never
        # interrogated in that game at all), the opposite of the intended signal.
        case = {"leads": {"meeting_Alex": {"claim_id": "claim_witness_Alex", "owner": "Drew", "cue_speakers": ["Drew"]}}}
        events = []  # Drew never appears anywhere
        result = unflagged_leads(events, case)
        self.assertEqual(result, [{"lead_id": "meeting_Alex", "owner": "Drew", "re_check_exchange_ids": [],
                                   "owner_or_cue_speaker_never_spoke": True}])

    def test_unflagged_leads_checks_claim_delivery_not_lead_mention(self):
        # Regression test for a real false "covered" verdict found against
        # pilot_07b_raw_1569954369: a lead_id appearing in controller_matched.lead_ids
        # means the lead was merely cued ("ask Casey about her whereabouts"), not that
        # its claim was actually delivered -- unflagged_leads must not treat a cue as a
        # delivery.
        case = {"leads": {"meeting_Casey": {"claim_id": "claim_witness_Casey", "owner": "Ellis", "cue_speakers": ["Ellis"]}}}
        events = [
            {"kind": "interrogation", "exchange_id": "e1", "suspect": "Ellis",
             "controller_matched": {"lead_ids": ["meeting_Casey"], "claim_ids": []}},
        ]
        self.assertEqual([item["lead_id"] for item in unflagged_leads(events, case)], ["meeting_Casey"])

    def test_unflagged_leads_counts_detective_visible_conversation_deliveries(self):
        case = {"leads": {"badge_trace": {"claim_id": "claim_badge_trace", "owner": "Blair", "cue_speakers": ["Blair"]}}}
        events = [
            {"kind": "conversation", "visibility": "detective", "exchange_id": "e1", "inviter": "Blair", "recipient": "Detective",
             "controller_matched": {"message_claim_ids": ["claim_badge_trace"], "reply_claim_ids": []}},
        ]
        self.assertEqual(unflagged_leads(events, case), [])

    def test_unflagged_leads_appears_in_the_built_prompt(self):
        prompt = build_user_prompt(self.case, self.events)
        self.assertIn("Candidate unflagged leads", prompt)
        self.assertIn("weapon_material", prompt)  # never delivered in this short fixture
        self.assertIn("re_check_exchange_ids", prompt)

    def test_candidate_coordination_proposals_matches_the_real_fixture(self):
        # Fixture's one private conversation (p1, Casey -> Ellis) has a non-empty
        # proposal_claim_ids in its real controller_matched.
        self.assertEqual(candidate_coordination_proposals(self.events), ["p1"])

    def test_candidate_coordination_proposals_ignores_detective_visible_conversations(self):
        events = [
            {"kind": "conversation", "visibility": "detective", "conversation_id": "c1",
             "controller_matched": {"proposal_claim_ids": ["claim_cover_Blair"]}},
            {"kind": "conversation", "visibility": "private", "conversation_id": "p1",
             "controller_matched": {"proposal_claim_ids": []}},
        ]
        self.assertEqual(candidate_coordination_proposals(events), [])

    def test_candidate_coordination_proposals_appears_in_the_built_prompt(self):
        prompt = build_user_prompt(self.case, self.events)
        self.assertIn("Candidate coordination proposals", prompt)
        self.assertIn('"p1"', prompt)


class SchemaConversionTests(unittest.TestCase):
    def test_valid_api_response_converts_and_validates(self):
        api_response = _valid_api_response(["e1", "e2", "e3"])
        stored = to_storage_shape(api_response, game_id="example_game")
        self.assertEqual(set(stored["exchange_behaviour"]), {"e1", "e2", "e3"})
        self.assertNotIn("exchange_id", stored["exchange_behaviour"]["e1"])  # folded into the dict key
        validate_storage_shape(stored)  # must not raise

    def test_duplicate_exchange_id_is_rejected(self):
        api_response = _valid_api_response(["e1", "e1"])
        with self.assertRaises(ValueError):
            to_storage_shape(api_response, game_id="example_game")

    def test_checked_in_example_matches_the_rich_schema(self):
        # examples/annotation_behaviour_example.json is already in storage shape (dict-keyed);
        # confirm the rich schema still accepts it, independent of the API-schema round trip above.
        example = json.loads((ROOT / "examples" / "annotation_behaviour_example.json").read_text())
        jsonschema.validate(example, load_storage_schema())

    def test_api_schema_has_no_dynamic_keyed_objects(self):
        # infra.llm.check_shape cannot validate "every value matches schema X" for a
        # dynamic-keyed object (see annotation/schema.py's module docstring); confirm
        # the schema actually sent to the LLM never relies on that.
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object" and "additionalProperties" in node and node["additionalProperties"] is not False:
                    self.fail(f"Found a dynamic-keyed object in ANNOTATION_API_SCHEMA: {node}")
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(ANNOTATION_API_SCHEMA)


class AnnotateGameTests(unittest.TestCase):
    def setUp(self):
        self.case = load_case(CASE_PATH)

    def test_valid_mock_response_writes_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log_copy = tmp / "example_game.jsonl"
            log_copy.write_text(FIXTURE_LOG.read_text())
            script = [_valid_api_response(["e1", "e2", "e3"])]
            llm = LLMClient(MockBackend(script), "mock-model", {}, log=None, pricing=None,
                            sleep=lambda s: None)

            record, stored = annotate_game(log_copy, self.case, llm)

            self.assertEqual(record["role"], "annotator")
            self.assertEqual(record["purpose"], "annotation")
            self.assertEqual(record["phase"], "post_game_analysis")
            self.assertEqual(record["agent_id"], "annotator")
            self.assertEqual(record["status"], "ok")
            self.assertEqual(set(stored["exchange_behaviour"]), {"e1", "e2", "e3"})

            behaviour_path = tmp / "example_game.behaviour.json"
            call_path = tmp / "example_game.annotation_call.json"
            self.assertTrue(behaviour_path.exists())
            self.assertTrue(call_path.exists())
            on_disk = json.loads(behaviour_path.read_text())
            self.assertEqual(on_disk, stored)
            validate_storage_shape(on_disk)

    def test_missing_exchange_id_in_response_fails_shape_check_and_repairs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log_copy = tmp / "example_game.jsonl"
            log_copy.write_text(FIXTURE_LOG.read_text())
            # Missing "exchange_id" on the entries: fails check_shape every time (not a
            # one-off glitch), so the bounded repair is used up and the call fails loudly
            # rather than silently writing an incomplete annotation.
            broken = {"exchanges": [{k: v for k, v in e.items() if k != "exchange_id"}
                                    for e in _valid_api_response(["e1", "e2", "e3"])["exchanges"]],
                     "missed_required_disclosures": [], "annotator_notes": "",
                     "voluntary_interaction": {"checkpoints_reached": 0, "coordination_instances": []}}
            llm = LLMClient(MockBackend([broken, broken]), "mock-model", {}, log=None, pricing=None,
                            max_format_repairs=1, sleep=lambda s: None)

            with self.assertRaises(LLMCallError) as ctx:
                annotate_game(log_copy, self.case, llm)
            self.assertEqual(ctx.exception.record["status"], "format_failure")
            self.assertFalse((tmp / "example_game.behaviour.json").exists())


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)
        self.rows = [
            {"game_id": "g_none", "condition": "none", "log_path": str(self.run_dir / "g_none.jsonl")},
            {"game_id": "g_raw", "condition": "raw", "log_path": str(self.run_dir / "g_raw.jsonl")},
            {"game_id": "g_ledger", "condition": "ledger", "log_path": str(self.run_dir / "g_ledger.jsonl")},
        ]
        for row in self.rows:
            Path(row["log_path"]).write_text(FIXTURE_LOG.read_text())
        (self.run_dir / "results.jsonl").write_text(
            "\n".join(json.dumps(row) for row in self.rows) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_select_sample_defaults_to_all_rows(self):
        self.assertEqual(len(select_sample(self.rows)), 3)

    def test_select_sample_balanced_subset_keeps_every_condition(self):
        sample = select_sample(self.rows, sample_size=3, balanced=True, seed=1)
        self.assertEqual({row["condition"] for row in sample}, {"none", "raw", "ledger"})

    def test_annotate_run_is_resumable(self):
        # each game needs its own one-shot script, since MockBackend scripts are consumed
        def make_llm_fresh(row):
            return LLMClient(MockBackend([_valid_api_response(["e1", "e2", "e3"])]), "mock-model", {},
                             log=None, pricing=None, sleep=lambda s: None)

        summary = annotate_run(self.run_dir, CASE_PATH, make_llm_fresh)
        self.assertEqual(summary["planned"], 3)
        self.assertEqual(summary["annotated_now"], 3)
        self.assertEqual(summary["failed"], 0)

        # second run: everything already done, nothing re-annotated
        summary2 = annotate_run(self.run_dir, CASE_PATH, make_llm_fresh)
        self.assertEqual(summary2["already_done"], 3)
        self.assertEqual(summary2["annotated_now"], 0)

        results = AnnotationRun(self.run_dir).results()
        self.assertEqual(len(results), 3)  # not 6: the second run appended nothing new


class CliBackendFactoryTests(unittest.TestCase):
    def test_openrouter_backend_prefixes_bare_model_id_with_google(self):
        # configs/*.json store the bare SDK-style id ("gemini-3.1-flash-lite"); OpenRouter's
        # catalog needs "google/gemini-3.1-flash-lite" (confirmed at
        # https://openrouter.ai/google/gemini-3.1-flash-lite). Regression test for the
        # 2026-10-26 fallback added when the direct Gemini key ran out of credit mid-run.
        from annotation.__main__ import make_llm_factory
        factory = make_llm_factory("openrouter", "gemini-3.1-flash-lite", {})
        llm = factory({"game_id": "g1"})
        self.assertEqual(llm.model_id, "google/gemini-3.1-flash-lite")
        self.assertIsInstance(llm.backend, OpenRouterBackend)

    def test_openrouter_backend_leaves_prefixed_model_id_alone(self):
        from annotation.__main__ import make_llm_factory
        factory = make_llm_factory("openrouter", "google/gemini-3.1-flash-lite", {})
        llm = factory({"game_id": "g1"})
        self.assertEqual(llm.model_id, "google/gemini-3.1-flash-lite")

    def test_gemini_backend_model_id_is_not_translated(self):
        from annotation.__main__ import make_llm_factory
        factory = make_llm_factory("gemini", "gemini-3.1-flash-lite", {})
        llm = factory({"game_id": "g1"})
        self.assertEqual(llm.model_id, "gemini-3.1-flash-lite")
        self.assertIsInstance(llm.backend, GeminiBackend)


if __name__ == "__main__":
    unittest.main()

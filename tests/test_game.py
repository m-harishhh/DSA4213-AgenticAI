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


def no_invite(case, suspect, state, history, available, llm, game_id):
    return {"invite": None, "call_id": None}


def parts(**overrides):
    """Real detective and suspect pipeline; fake memory, interpreter and extraction.
    Nobody invites at checkpoints unless a test overrides "invite" (checkpoints get their own tests)."""
    return {**default_parts(), "memory": fake_memory, "interpret": fake_interpret, "extract": fake_extract,
            "invite": no_invite, **overrides}


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

    def test_prompt_versions_come_from_the_code(self):
        from detective.prompts import PROMPT_VERSION
        _, events = self.play(script(0, ["Alex"]))
        logged = events[0]["prompt_versions"]
        self.assertEqual(logged["detective"], PROMPT_VERSION)
        self.assertEqual(set(logged), {"detective", "suspect", "interpreter", "extractor"})

    def test_fenced_json_is_accepted(self):
        # pilot_04c: gemini-3.5-flash-lite wraps valid actions in ```json fences; that is not a format failure
        fenced = "```json\n" + json.dumps(accuse("Alex", first=True)) + "\n```"
        result, events = self.play([fenced])
        self.assertEqual(result["outcome"], "solved_first")
        self.assertFalse(any(e["kind"] == "failure" for e in events))

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


def interpretation(kind, **fields):
    return {"kind": kind, "time": None, "subject": None, "topic": None, "challenged": None, "evidence": [], **fields}


def extraction(speaker, span):
    return {"entries": [{"type": "claim", "speaker": speaker, "statement": span, "entities": [], "time": None,
                         "target": None, "topic": None, "retracts": False, "source_span": span}]}


class DefaultParts(GameTest):
    """A complete mock game through default_parts(): every real module is connected
    (memory, detective, interpreter, suspect pipeline, matching, extraction); only the LLM is scripted."""

    def turn(self, condition, suspect, question_text, interp, answer):
        responses = [{"action": "interrogate", "suspect": suspect, "question": question_text}, interp, answer]
        if condition == "ledger":
            responses.append(extraction(suspect, answer))
        return responses

    def challenge_script(self, condition):
        return (self.turn(condition, "Alex", "Where were you at 21:00?",
                          interpretation("whereabouts", time="21:00"), "I was in the Study at 21:00.")
                + self.turn(condition, "Drew", "Did you see Alex at 21:00?",
                            interpretation("corroborate", subject="Alex", time="21:00"),
                            "The timestamped recording shows Alex in the Library at 21:00.")
                + self.turn(condition, "Alex", "Drew's recording puts you in the Library at 21:00. Why did you lie?",
                            interpretation("challenge", challenged="e1", evidence=["e2"]),
                            "Fine. I was in the Library at 21:00, but I did not kill anyone.")
                + [accuse("Alex", first=True)])

    def test_complete_game_with_a_challenge_and_retraction(self):
        for condition in ("none", "raw", "ledger"):
            with self.subTest(condition=condition):
                result, events = self.play(self.challenge_script(condition), condition, default_parts())
                self.assertEqual(result["outcome"], "solved_first")
                turns = [e for e in events if e["kind"] == "interrogation"]
                self.assertEqual([t["intent"]["kind"] for t in turns], ["whereabouts", "corroborate", "challenge"])
                self.assertEqual(turns[0]["controller_matched"]["claim_ids"], ["claim_cover_Alex"])
                self.assertEqual(turns[2]["intent"]["claim_id"], "claim_cover_Alex")
                self.assertEqual(turns[2]["controller_matched"]["retraction_ids"], ["claim_cover_Alex"])
                roles = {e["role"] for e in events if e["kind"] == "llm_call"}
                expected = {"detective", "interpreter", "suspect"} | ({"extractor"} if condition == "ledger" else set())
                self.assertEqual(roles, expected)

    def test_challenge_targets_the_claim_on_the_evidence_topic(self):
        # pilot_02 bug: Alex's overview delivered BOTH the true 20:30 account and the 21:00 cover story;
        # the challenge picked the 20:30 claim, so Alex could never retract the lie.
        script = (self.turn("raw", "Alex", "Tell me about your evening.", interpretation("overview"),
                            "I was in the Study at 20:30 and still in the Study at 21:00.")
                  + self.turn("raw", "Drew", "Did you see Alex at 21:00?",
                              interpretation("corroborate", subject="Alex", time="21:00"),
                              "The timestamped recording shows Alex in the Library at 21:00.")
                  + self.turn("raw", "Alex", "Drew's recording puts you in the Library at 21:00. Explain.",
                              interpretation("challenge", challenged="e1", evidence=["e2"]),
                              "I insist I was in the Study at 21:00.")
                  # the detective now points at the previous challenge answer, which matched nothing
                  + self.turn("raw", "Alex", "You keep saying the Study. Drew's recording disagrees.",
                              interpretation("challenge", challenged="e3", evidence=["e2"]),
                              "Fine, I was in the Library at 21:00.")
                  + [accuse("Alex", first=True)])
        _, events = self.play(script, "raw", default_parts())
        turns = [e for e in events if e["kind"] == "interrogation"]
        self.assertEqual(sorted(turns[0]["controller_matched"]["claim_ids"]),
                         ["claim_cover_Alex", "claim_self_Alex_20:30"])
        for challenge in turns[2:]:
            self.assertEqual(challenge["intent"]["claim_id"], "claim_cover_Alex")
        self.assertEqual(turns[3]["controller_matched"]["retraction_ids"], ["claim_cover_Alex"])

    def test_suspect_prompt_rules(self):
        _, events = self.play(self.turn("raw", "Alex", "Where were you at 21:00?",
                                        interpretation("whereabouts", time="21:00"), "In the Study at 21:00.")
                              + [accuse("Alex", first=True)], "raw", default_parts())
        system = next(e for e in events if e["kind"] == "llm_call" and e["role"] == "suspect")["messages"][0]["content"]
        self.assertIn("Do not invent additional facts", system)
        self.assertNotIn("At checkpoints", system)   # checkpoint rule only at checkpoints (pilot_02)

    def test_volunteered_evidence_unlocks_retraction(self):
        # pilot_03 end to end: Drew volunteers the sighting; the challenge citing it makes Alex retract.
        script = (self.turn("raw", "Alex", "Where were you at 21:00?", interpretation("whereabouts", time="21:00"),
                            "I was in the Study at 21:00.")
                  + self.turn("raw", "Drew", "Where were you at 21:00?", interpretation("whereabouts", time="21:00"),
                              "I was in the Library at 21:00. I happened to see Alex there.")
                  + self.turn("raw", "Alex", "Drew saw you in the Library at 21:00. Which is true?",
                              interpretation("challenge", challenged="e1", evidence=["e2"]),
                              "Alright, I was in the Library at 21:00.")
                  + [accuse("Alex", first=True)])
        _, events = self.play(script, "raw", default_parts())
        challenge = [e for e in events if e["kind"] == "interrogation"][2]
        self.assertEqual(challenge["allowed_branches"][0]["branch_id"], "retract_account_not_confess")
        self.assertEqual(challenge["controller_matched"]["retraction_ids"], ["claim_cover_Alex"])

    def test_motive_has_no_checkpoint_sentence_in_interrogations(self):
        # every suspect in both cases: the motive core stays, "You may approach the detective..." does not
        from casework import suspect_view
        from casework.disclosure import branch_prompt
        from controller.suspect import suspect_system_prompt
        for path in ("pilot.json", "evaluation.json"):
            case = load_case(str(ROOT / "cases" / path))
            for suspect in case["suspects"]:
                with self.subTest(case=path, suspect=suspect):
                    state = initial_state(case, suspect)
                    branches = allowed_content(case, suspect, {"kind": "overview"}, state)
                    prompt = suspect_system_prompt(suspect_view(case, suspect, state),
                                                   [branch_prompt(case, b) for b in branches])
                    self.assertNotIn("approach the detective", prompt)
                    core = case["suspects"][suspect]["motive"].split(". ")[0]
                    self.assertIn("Your motive: " + core, prompt)

    def test_memory_window_holds_the_latest_k_exchanges(self):
        script = []
        for _ in range(10):
            script += self.turn("raw", "Alex", "Where were you at 21:00?",
                                interpretation("whereabouts", time="21:00"), "I was in the Study at 21:00.")
        _, events = self.play(script + [accuse("Alex", first=True)], "raw", {**default_parts(), "invite": no_invite})
        windows = [e["window_exchange_ids"] for e in events if e["kind"] == "memory_view"]
        self.assertEqual(windows[0], [])                                   # empty before the first question
        self.assertEqual(windows[-1], [f"e{i}" for i in range(3, 11)])     # k = 8 at the accusation

    def test_challenge_without_evidence_is_an_ordinary_question(self):
        # pilot_03b: "What weapon were you carrying at 21:00?" came back as a challenge with no evidence,
        # which only allows maintain/decline. It is answered as an ordinary question instead.
        script = (self.turn("raw", "Alex", "What weapon were you carrying at 21:00?",
                            interpretation("challenge", time="21:00", challenged="e1"), "None at all.")
                  + self.turn("raw", "Drew", "What was the weapon material?",
                              interpretation("challenge"), "It was steel.")
                  + [accuse("Alex", first=True)])
        _, events = self.play(script, "raw", default_parts())
        intents = [e["intent"] for e in events if e["kind"] == "interrogation"]
        self.assertEqual(intents, [{"kind": "whereabouts", "time": "21:00"}, {"kind": "overview"}])

    def test_interpreter_ignores_topics_never_mentioned(self):
        script = self.turn("raw", "Blair", "What about the injury pattern?",
                           interpretation("ask_lead", topic="weapon_edge"), "I'd rather not say.")
        _, events = self.play(script + [accuse("Alex", first=True)], "raw", default_parts())
        turn = next(e for e in events if e["kind"] == "interrogation")
        self.assertEqual(turn["intent"], {"kind": "ask_lead", "topic": None})


def checkpoint_parts(invite_policy, suspect_accepts=lambda suspect, inviter: True, detective_accepts=True,
                     detective_ok=True):
    """Fake checkpoint agents. invite_policy(suspect, available) -> name, "Detective" or None.
    Messages state the speaker's first shareable claim, so sharing can be checked."""
    from casework.disclosure import communication_options

    def invite(case, suspect, state, history, available, llm, game_id):
        return {"invite": invite_policy(suspect, available), "call_id": None}

    def accept(case, suspect, inviter, state, history, llm, game_id):
        return {"accept": suspect_accepts(suspect, inviter), "call_id": None}

    def say(case, suspect, state):
        options = communication_options(case, suspect, state)
        shareable = [o for o in options if o["kind"] == "share_claim"]
        return {"text": shareable[0]["text"] if shareable else "Nothing to add.", "options": options, "call_id": None}

    def message(case, suspect, recipient, state, history, llm, game_id):
        return say(case, suspect, state)

    def reply(case, suspect, inviter, text, state, history, llm, game_id):
        return say(case, suspect, state)

    def detective_accept(inviter, memory, ctx, llm, game_id):
        return {"accept": detective_accepts if detective_ok else None, "ok": detective_ok,
                "errors": [] if detective_ok else ["bad"], "call_id": None}

    def detective_reply(inviter, text, memory, ctx, llm, game_id):
        return {"message": "Thank you, noted.", "ok": True, "errors": [], "call_id": None}

    return parts(invite=invite, accept=accept, message=message, reply=reply,
                 detective_accept=detective_accept, detective_reply=detective_reply)


def first_available(suspect, available):
    return available[0]


def prefer_detective(suspect, available):
    return "Detective" if "Detective" in available else available[0]


class Checkpoints(GameTest):
    def kinds(self, events, kind):
        return [e for e in events if e["kind"] == kind]

    def test_fire_after_interrogations_5_10_15(self):
        _, events = self.play(script(16, ["Alex"]), game_parts=checkpoint_parts(first_available))
        starts = self.kinds(events, "checkpoint_start")
        self.assertEqual([s["after_interrogation"] for s in starts], [5, 10, 15])
        self.assertEqual(self.end(events)["checkpoints_reached"], 3)

    def test_no_checkpoint_before_the_fifth_question(self):
        _, events = self.play(script(4, ["Alex"]), game_parts=checkpoint_parts(first_available))
        self.assertEqual(self.kinds(events, "checkpoint_start"), [])
        self.assertEqual(self.end(events)["checkpoints_reached"], 0)

    def test_slot_limits(self):
        # Everyone invites and everyone accepts: at most 3 conversations, at most 1 with the detective,
        # and nobody in two conversations at the same checkpoint (CKPT-2)
        _, events = self.play(script(5, ["Alex"]), game_parts=checkpoint_parts(prefer_detective))
        conversations = self.kinds(events, "conversation")
        self.assertLessEqual(len(conversations), 3)
        self.assertEqual(sum(c["visibility"] == "detective" for c in conversations), 1)
        people = [p for c in conversations for p in (c["inviter"], c["recipient"])]
        self.assertEqual(len(people), len(set(people)))

    def test_rejection_keeps_both_slots(self):
        # Every suspect rejects every suspect invitation; the detective rejects too (CKPT-3)
        _, events = self.play(script(5, ["Alex"]), game_parts=checkpoint_parts(
            first_available, suspect_accepts=lambda s, i: False, detective_accepts=False))
        invitations = self.kinds(events, "invitation")
        self.assertEqual({i["decision"] for i in invitations}, {"rejected"})
        self.assertEqual({i["pass_no"] for i in invitations}, {1, 2})      # still free, so invited again
        self.assertEqual(self.kinds(events, "conversation"), [])
        self.assertEqual(self.end(events)["invitations"], {"sent": len(invitations), "accepted": 0,
                                                           "rejected": len(invitations)})

    def test_detective_conversation_enters_memory_without_a_turn(self):
        detective_only = lambda s, available: "Detective" if "Detective" in available else None
        _, events = self.play(script(6, ["Alex"]), game_parts=checkpoint_parts(detective_only))
        conversation = self.kinds(events, "conversation")[0]
        self.assertEqual((conversation["visibility"], conversation["exchange_id"]), ("detective", "e6"))
        self.assertEqual(self.kinds(events, "interrogation")[-1]["exchange_id"], "e7")   # numbering continues
        self.assertEqual(self.end(events)["interrogations_used"], 6)                     # no turn used (CKPT-9)
        self.assertEqual(self.end(events)["exchanges_total"], 7)
        windows = [e["window_exchange_ids"] for e in self.kinds(events, "memory_view")]
        self.assertTrue(any("e6" in w for w in windows))                                  # CKPT-7

    def test_private_conversation_never_reaches_the_detective(self):
        suspects_only = lambda s, available: next((a for a in available if a != "Detective"), None)
        _, events = self.play(script(6, ["Alex"]), game_parts=checkpoint_parts(suspects_only))
        conversations = self.kinds(events, "conversation")
        self.assertTrue(conversations)
        self.assertEqual({c["visibility"] for c in conversations}, {"private"})
        self.assertTrue(all(c["exchange_id"] is None for c in conversations))            # CKPT-8
        windows = [x for e in self.kinds(events, "memory_view") for x in e["window_exchange_ids"]]
        self.assertEqual(sorted(set(windows)), sorted({f"e{i}" for i in range(1, 7)}))  # interrogations only
        shared = conversations[0]["controller_matched"]["message_claim_ids"]
        self.assertTrue(shared)                                                           # what was said is passed on

    def test_proposal_order_is_seeded(self):
        orders = []
        for _ in range(2):
            self.seed = 41                                          # play() adds 1: same seed both times
            _, events = self.play(script(5, ["Alex"]), game_parts=checkpoint_parts(first_available))
            orders.append(self.kinds(events, "checkpoint_start")[0]["proposal_order"])
            Path(self.tmp.name, "test_raw_42.jsonl").unlink()
        self.assertEqual(orders[0], orders[1])                     # CKPT-11

    def test_detective_checkpoint_failure_is_a_failed_run(self):
        detective_only = lambda s, available: "Detective" if "Detective" in available else None
        result, events = self.play(script(5, ["Alex"]), game_parts=checkpoint_parts(detective_only, detective_ok=False))
        self.assertEqual(result["outcome"], "failed_run")
        self.assertEqual(self.kinds(events, "failure")[0]["stage"], "checkpoint_acceptance")

    def test_complete_game_with_real_checkpoint_prompts(self):
        # Every real module connected (detective, interpreter, suspects incl. checkpoint prompts, extraction);
        # a scripted LLM recognises each prompt and answers it.
        decisions = iter([{"action": "interrogate", "suspect": name, "question": "Tell me about your evening."}
                          for name in ["Alex", "Blair", "Casey", "Drew", "Ellis", "Alex"]]
                         + [accuse("Alex", first=True)])
        invited = set()

        def llm(request):
            system = request["messages"][0]["content"]
            last = request["messages"][-1]["content"]
            if system.startswith("You are a celebrated private detective"):
                if '{"accept": true}' in last:
                    return {"accept": True}
                if '{"message":' in last:
                    return {"message": "Thank you, noted."}
                return next(decisions)
            if system.startswith("You classify one question"):
                return interpretation("overview")
            if system.startswith("You take notes"):
                return {"entries": []}
            if "invite ONE person" in system:
                name = system.split("You are ")[1].split(",")[0]
                choice = "Detective" if not invited else None   # only the first suspect asked invites
                invited.add(name)
                return {"invite": choice}
            if "invites you to a short private conversation" in system:
                return {"accept": True}
            return "I was in the Study at 21:00."                   # answers and messages

        for condition in ("raw", "ledger"):
            with self.subTest(condition=condition):
                decisions = iter([{"action": "interrogate", "suspect": n, "question": "Tell me about your evening."}
                                  for n in ["Alex", "Blair", "Casey", "Drew", "Ellis", "Alex"]]
                                 + [accuse("Alex", first=True)])
                invited.clear()
                result, events = self.play(llm, condition, default_parts())
                conversation = self.kinds(events, "conversation")[0]
                self.assertEqual(conversation["visibility"], "detective")
                self.assertEqual(conversation["reply"], "Thank you, noted.")
                self.assertEqual(self.end(events)["conversations"], {"detective": 1, "private": 0})
                purposes = {e["purpose"] for e in self.kinds(events, "llm_call")}
                self.assertTrue({"invitation", "acceptance", "conversation_message", "conversation_reply"} <= purposes)


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
        # asked about 21:00, but volunteering the true 20:30 whereabouts still counts (§3.3)
        self.assertEqual(self.match("Alex", intent, "I was in the Study at 20:30.")["claim_ids"],
                         ["claim_self_Alex_20:30"])
        self.assertEqual(self.match("Alex", intent, "I was in the Gallery at 20:30.")["claim_ids"], [])

    def test_negation_is_not_delivery(self):
        intent = {"kind": "whereabouts", "time": "21:00"}
        self.assertEqual(self.match("Alex", intent, "I was NOT in the Library at 21:00.")["claim_ids"], [])
        self.assertEqual(self.match("Alex", intent, "I was never in the Study at 21:00.")["claim_ids"], [])
        self.assertEqual(self.match("Alex", intent, "I wasn't in the Study at 21:00, I was in the Library at 21:00.")
                         ["claim_ids"], ["claim_self_Alex_21:00"])

    def test_volunteered_truthful_claims_count(self):
        # pilot_03: asked about his own whereabouts, Drew volunteered seeing Alex in the Library.
        # That is the counter-evidence to Alex's cover story, so it must count as delivered.
        whereabouts = {"kind": "whereabouts", "time": "21:00"}
        said = self.match("Drew", whereabouts, "I was in the Library at 21:00. I happened to see Alex there.")
        self.assertEqual(said["claim_ids"], ["claim_self_Drew_21:00", "claim_witness_Alex"])
        self.assertEqual(self.match("Drew", whereabouts, "I was in the Library at 21:00.")["claim_ids"],
                         ["claim_self_Drew_21:00"])                       # no name, no witness claim
        self.assertEqual(self.match("Alex", whereabouts, "The walls were green.")["claim_ids"], [])  # not Alex's clue

    def test_room_and_time_must_share_a_sentence(self):
        # pilot_03b answers, verbatim
        whereabouts = {"kind": "whereabouts", "time": "21:00"}
        cases = [
            ("Alex", "I was in the Study at 21:00. I had been working there throughout the evening, "
                     "though I stopped by the Library briefly earlier on.", ["claim_cover_Alex"]),
            ("Alex", "Actually, I need to correct my earlier statement; I was in the Library at 21:00.",
             ["claim_self_Alex_21:00"]),
            ("Drew", "I was in the Library at 21:00. Alex was also there at that time.",
             ["claim_self_Drew_21:00", "claim_witness_Alex"]),      # "there" borrows the room from the sentence before
            ("Drew", "I was in the Library at 21:00. Alex was in the Study.", ["claim_self_Drew_21:00"]),
        ]
        for suspect, text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.match(suspect, whereabouts, text)["claim_ids"], expected)

    def test_negated_sighting_is_not_a_witness_claim(self):
        # Aravindh's review of #21
        whereabouts = {"kind": "whereabouts", "time": "21:00"}
        self.assertEqual(self.match("Drew", whereabouts, "At 21:00 I was in the Library; Alex wasn't there.")["claim_ids"],
                         ["claim_self_Drew_21:00"])
        self.assertEqual(self.match("Drew", whereabouts, "No, Alex was in the Library at 21:00.")["claim_ids"],
                         ["claim_witness_Alex"])

    def test_whole_words_only(self):
        self.assertEqual(self.match("Blair", {"kind": "overview"}, "I walked around the gallery.")["claim_ids"], [])

    def test_leads(self):
        overview = {"kind": "overview"}
        self.assertEqual(self.match("Blair", overview, "Ask me about the attacker's badge.")["lead_ids"], ["badge_trace"])
        self.assertEqual(self.match("Blair", overview, "I don't know anything about the badge.")["lead_ids"], [])
        self.assertEqual(self.match("Drew", overview, "At 21:00 I was busy.")["lead_ids"], [])


if __name__ == "__main__":
    unittest.main()

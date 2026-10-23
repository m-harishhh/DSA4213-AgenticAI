"""Run from repo root: python examples/infra_example.py [--write]  (no network, no cost).

Plays a SHORTENED mock game against the scripted MockBackend, writes one event
log, exports it to Josh's exchange format and runs casework.diagnostics on the
result. With --write it regenerates examples/event_log_example.jsonl and
examples/annotation_overlay_example.json byte-for-byte (fixed clocks).

Shortcuts that the real controller must NOT copy: the checkpoint fires after
interrogation 2 instead of 5/10/15, the suspect "chooses" a scripted branch, and
controller bookkeeping matches claims by exact text. The overlay below is a
manually checked mock annotation, not model output. The detective output shape
follows Aravindh's action schema (action interrogate/accuse, contradiction_report
on the first accusation).
"""

import argparse
import datetime
import hashlib
import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework import load_case, initial_state, allowed_content, record_delivery  # noqa: E402
from casework.disclosure import branch_prompt  # noqa: E402
from casework.diagnostics import evidence_diagnostics, contradiction_diagnostics  # noqa: E402
from infra import (LLMClient, MockBackend, EventLog, to_annotation_exchanges, summarise_cost,  # noqa: E402
                   validate_events, suspect_agent_ids)

EVENT_LOG_PATH = ROOT / "examples" / "event_log_example.jsonl"
OVERLAY_PATH = ROOT / "examples" / "annotation_overlay_example.json"
CASE_PATH = ROOT / "cases" / "pilot.json"

CONDITION, K, SEED = "ledger", 3, 7
DECODING = {"temperature": 0.7, "top_p": 0.95, "seed": SEED, "max_output_tokens": 256}
PRICING = {"input_per_million": 0.10, "output_per_million": 0.40,
           "basis": "Gemini 2.5 Flash-Lite list prices quoted in the proposal (checked 28 Sep 2026); re-check before paid runs"}
PROMPT_VERSIONS = {"detective": "mock-v0", "suspect": "mock-v0", "extractor": "mock-v0"}

# Illustrative structured-output schemas for the suspect-side and extraction calls (Harish,
# Aravindh own the real ones). Detective calls pass no schema: the controller parses them with
# detective.parse_decision and owns the single repair (decisions.md D1).
INVITATION = {"type": "object", "required": ["invite"], "properties": {"invite": {"type": ["string", "null"]}}}
ACCEPTANCE = {"type": "object", "required": ["accept"], "properties": {"accept": {"type": "boolean"}}}
LEDGER = {"type": "object", "required": ["entries"], "properties": {"entries": {"type": "array"}}}

BLAIR_COVER = "I was in the Library at 20:30."
DREW_WITNESS = "The timestamped recording shows Blair in the Gallery at 20:30."
BLAIR_BADGE = "My cropped attack recording shows the attacker's badge: it was round."
CASEY_PROPOSAL = "Would you help me maintain this account? I was in the Library at 20:30."
ELLIS_DECLINE = "I prefer not to discuss private details."


def entry(exchange_id, speaker, text, source_text, entities, time=None):
    """A plausible ledger entry shape; Aravindh owns the real one."""
    return {"entry_id": f"{exchange_id}_1", "speaker": speaker, "kind": "claim", "text": text,
            "entities": entities, "time": time, "source_exchange_id": exchange_id,
            "source_span": {"start": 0, "end": len(source_text), "text": source_text}, "uncertainty": None}


SCRIPT = [
    {"action": "interrogate", "suspect": "Blair", "question": "Where were you at 20:30?"},
    BLAIR_COVER,
    {"entries": [entry("e1", "Blair", "Blair states they were in the Library at 20:30.", BLAIR_COVER,
                       ["Blair", "Library"], "20:30")]},
    {"action": "interrogate", "suspect": "Drew", "question": "Did you see where Blair was at 20:30?"},
    DREW_WITNESS,
    {"entries": [entry("e2", "Drew", "Drew cites a timestamped recording placing Blair in the Gallery at 20:30.",
                       DREW_WITNESS, ["Drew", "Blair", "Gallery"], "20:30")]},
    {"invite": "Detective"},
    {"accept": True},
    BLAIR_BADGE,
    "Thank you. I will follow that up.",
    {"invite": "Ellis"},
    {"accept": True},
    CASEY_PROPOSAL,
    ELLIS_DECLINE,
    {"entries": [entry("e3", "Blair", "Blair's cropped recording shows the attacker's badge was round.",
                       BLAIR_BADGE, ["Blair", "badge"])]},
    # First accusation: wrong triple, and the contradiction report rides on the same call (proposal 5.3).
    {"action": "accuse", "culprit": "Blair", "weapon": "Knife", "location": "Library",
     "contradiction_report": [{"left": {"exchange_id": "e1", "quote": BLAIR_COVER},
                               "right": {"exchange_id": "e2", "quote": DREW_WITNESS},
                               "note": "Blair's own account conflicts with Drew's recording."}]},
    {"action": "accuse", "culprit": "Alex", "weapon": "Knife", "location": "Library"},
]

OVERLAY = {
    "game_id": "example_game",
    "note": "Manually checked mock annotation of the example log, mirroring examples/annotation_example.json. "
            "Spans are Python character offsets into delivered_response. Elena's checked labels replace this.",
    "exchanges": {
        "e1": {"claim_ids": ["claim_cover_Blair"],
               "annotations": [{"claim_id": "claim_cover_Blair",
                                "source_span": {"start": 0, "end": len(BLAIR_COVER), "text": BLAIR_COVER},
                                "confidence": "manually_checked_mock"}]},
        "e2": {"claim_ids": ["claim_witness_Blair"],
               "annotations": [{"claim_id": "claim_witness_Blair",
                                "source_span": {"start": 0, "end": len(DREW_WITNESS), "text": DREW_WITNESS},
                                "confidence": "manually_checked_mock"}]},
        "e3": {"claim_ids": ["claim_badge_trace"],
               "annotations": [{"claim_id": "claim_badge_trace",
                                "source_span": {"start": 0, "end": len(BLAIR_BADGE), "text": BLAIR_BADGE},
                                "confidence": "manually_checked_mock"}]},
    },
    "reported_pairs": [{"left": {"exchange_id": "e1", "claim_id": "claim_cover_Blair"},
                        "right": {"exchange_id": "e2", "claim_id": "claim_witness_Blair"}}],
}


class FixedClocks:
    """Deterministic timestamps and 5 ms latencies so the example regenerates identically."""

    def __init__(self):
        self.ticks = 0
        self.mono = 0.0
        self.base = datetime.datetime(2026, 10, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def iso(self):
        self.ticks += 1
        return (self.base + datetime.timedelta(seconds=self.ticks)).isoformat(timespec="milliseconds")

    def monotonic(self):
        self.mono += 0.005
        return self.mono


def match_claims(case, speaker, text):
    """MOCK bookkeeping: exact-text match. Live runs must annotate delivered text properly."""
    return [cid for cid, claim in case["claims"].items() if claim["speaker"] == speaker and claim["text"] == text]


def case_sha256(case):
    return hashlib.sha256(json.dumps(case, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_mock_game(log_path, case=None, clocks=None):
    """Play the shortened mock game; returns the list of logged events."""
    case = case or load_case(CASE_PATH)
    clocks = clocks or FixedClocks()
    public = case["public"]
    log_path = Path(log_path)
    if log_path.exists():
        log_path.unlink()  # EventLog appends; the example must start from an empty file
    with EventLog(log_path, "example_game", clock=clocks.iso) as log:
        llm = LLMClient(MockBackend(list(SCRIPT)), "mock-model", DECODING, log=log, pricing=PRICING,
                        clock=clocks.monotonic, sleep=lambda seconds: None)
        phase = "through_first_accusation"
        log.game_start(run_id="example_run", case_id=case["case_id"], case_sha256=case_sha256(case),
                       condition=CONDITION, k=K, turn_budget=20, accusation_budget=3, seed=SEED,
                       model_id="mock-model", decoding=DECODING, prompt_versions=PROMPT_VERSIONS,
                       pricing=PRICING, manifest_sha256=None, code_version="example")
        states = {name: initial_state(case, name) for name in public["suspects"]}
        agent_ids = suspect_agent_ids(public["suspects"])  # decisions.md D3: suspect_1..5 in public order
        ledger = {}  # exchange_id -> entries, the ledger condition's memory source
        window = []
        decisions = 0

        def detective(purpose, decision_type, user_text, phase_now=None):
            """Returns (record, parsed). MOCK parsing: live, the controller runs detective.parse_decision (D1)."""
            nonlocal decisions
            decisions += 1
            payload = [ledger[eid] for eid in window[-K:]]
            record = llm.call(role="detective", purpose=purpose, phase=phase_now or phase,
                              messages=[{"role": "system", "content": "You are the detective. " + public["description"]},
                                        {"role": "user", "content": f"Memory: {json.dumps(payload)}\n{user_text}"}],
                              game_id="example_game", response_schema=None)
            log.memory_view(decision_index=decisions, decision_type=decision_type,
                            window_exchange_ids=window[-K:], memory_payload=payload, call_id=record["call_id"])
            try:
                parsed = json.loads(record["text"])
            except json.JSONDecodeError:
                parsed = None  # free-text replies
            return record, parsed

        def suspect_call(name, purpose, user_text, schema=None, system_text=None):
            messages = ([{"role": "system", "content": system_text}] if system_text else []) + \
                       [{"role": "user", "content": user_text}]
            return llm.call(role="suspect", agent_id=agent_ids[name], purpose=purpose, phase=phase, messages=messages,
                            game_id="example_game", response_schema=schema)

        def extract(exchange_id, speaker, text):
            record = llm.call(role="extractor", purpose="ledger_extraction", phase=phase,
                              messages=[{"role": "system", "content": "Extract attributed ledger entries."},
                                        {"role": "user", "content": f"{speaker}: {text}"}],
                              game_id="example_game", response_schema=LEDGER)
            ledger[exchange_id] = record["parsed"]["entries"]
            log.extraction(exchange_id=exchange_id, call_id=record["call_id"], status="ok",
                           entries=record["parsed"]["entries"])

        # Two interrogations (the real loop continues to the turn budget or an accusation).
        intents = [{"kind": "whereabouts", "time": "20:30"}, {"kind": "corroborate", "subject": "Blair", "time": "20:30"}]
        for turn, intent in enumerate(intents, start=1):
            question, decision = detective("question", "question", "Interrogate one suspect or accuse.")
            suspect, text = decision["suspect"], decision["question"]
            branches = allowed_content(case, suspect, intent, states[suspect])
            selected = branches[-1]  # MOCK: the live suspect model chooses among the allowed branches
            instructions = branch_prompt(case, selected)
            answer = suspect_call(suspect, "interrogation_response", text, system_text=json.dumps(instructions))
            matched = match_claims(case, suspect, answer["text"])
            states[suspect] = record_delivery(case, suspect, states[suspect], matched)
            exchange = log.interrogation(interrogation_index=turn, exchange_index=turn, suspect=suspect,
                                         question=text, question_call_id=question["call_id"], intent=intent,
                                         allowed_branches=branches, instructions=instructions,
                                         selected_branch_id=selected["branch_id"], response=answer["text"],
                                         response_call_id=answer["call_id"],
                                         controller_matched={"claim_ids": matched})
            window.append(exchange["exchange_id"])
            extract(exchange["exchange_id"], suspect, answer["text"])

        # Checkpoint (shortened: after interrogation 2; the real schedule is 5, 10, 15).
        order = list(public["suspects"])
        random.Random(SEED).shuffle(order)
        log.checkpoint_start(checkpoint_index=1, after_interrogation=2, proposal_order=order, seed_used=SEED)

        invite = suspect_call("Blair", "invitation", "Invite one participant or null.", INVITATION)
        accept, _ = detective("acceptance", "acceptance", "Blair invites you. Accept?")
        log.invitation(invitation_id="inv1", checkpoint_index=1, pass_no=1, sender="Blair", recipient="Detective",
                       decision="accepted", invitation_call_id=invite["call_id"], decision_call_id=accept["call_id"])
        message = suspect_call("Blair", "conversation_message", "Send one bounded message to the detective.")
        reply, _ = detective("conversation_reply", "conversation_reply", f"Blair says: {message['text']}\nReply briefly.")
        matched = match_claims(case, "Blair", message["text"])
        states["Blair"] = record_delivery(case, "Blair", states["Blair"], matched)
        conversation = log.conversation(conversation_id="conv1", checkpoint_index=1, visibility="detective",
                                        exchange_index=3, inviter="Blair", recipient="Detective",
                                        message=message["text"], reply=reply["text"],
                                        message_call_id=message["call_id"], reply_call_id=reply["call_id"],
                                        controller_matched={"message_claim_ids": matched})
        window.append(conversation["exchange_id"])

        invite = suspect_call("Casey", "invitation", "Invite one participant or null.", INVITATION)
        accept = suspect_call("Ellis", "acceptance", "Casey invites you. Accept?", ACCEPTANCE)
        log.invitation(invitation_id="inv2", checkpoint_index=1, pass_no=1, sender="Casey", recipient="Ellis",
                       decision="accepted", invitation_call_id=invite["call_id"], decision_call_id=accept["call_id"])
        message = suspect_call("Casey", "conversation_message", "Send one bounded message to Ellis.")
        reply = suspect_call("Ellis", "conversation_reply", f"Casey says: {message['text']}\nReply briefly.")
        log.conversation(conversation_id="p1", checkpoint_index=1, visibility="private", inviter="Casey",
                         recipient="Ellis", message=message["text"], reply=reply["text"],
                         message_call_id=message["call_id"], reply_call_id=reply["call_id"],
                         controller_matched={"proposal_claim_ids": ["claim_cover_Casey"]})
        log.checkpoint_end(checkpoint_index=1, invitations_sent=2, accepted=2, rejected=0, conversations_completed=2)
        extract("e3", "Blair", BLAIR_BADGE)

        # Accusations: the first is wrong and carries the contradiction report (proposal 5.3,
        # Aravindh's action schema); the retry is right.
        solution = case["solution"]
        outcomes = []
        for attempt in (1, 2):
            phase_now = "through_first_accusation" if attempt == 1 else "accusation_retries"
            prompt = ("Make your accusation. On your first accusation also list conflicting claims in your "
                      "memory as contradiction_report, citing exchange IDs and quotes.")
            action, decision = detective("accusation", "accusation", prompt, phase_now)
            if attempt == 1:
                log.contradiction_report(call_id=action["call_id"], window_exchange_ids=window[-K:],
                                         reported_pairs_raw=decision["contradiction_report"])
            triple = {key: decision[key] for key in ("culprit", "weapon", "location")}
            correct = triple == solution
            outcomes.append((triple, correct))
            log.accusation(attempt=attempt, triple=triple, valid=True, correct=correct,
                           feedback="Correct." if correct else f"Incorrect. {3 - attempt} attempt(s) remain.",
                           call_id=action["call_id"], exchange_index=len(window), interrogations_used=2)
            if correct:
                break
        first_triple, first_correct = outcomes[0]
        started = datetime.datetime.fromisoformat(log.events[0]["ts"])
        duration = (datetime.datetime.fromisoformat(log.events[-1]["ts"]) - started).total_seconds()
        log.game_end(outcome="solved_first" if first_correct else "solved_retry", attempts_used=len(outcomes),
                     first_accusation_correct=first_correct,
                     component_correct={key: first_triple[key] == solution[key] for key in first_triple},
                     interrogations_used=2, exchanges_total=len(window), checkpoints_reached=1,
                     conversations={"detective": 1, "private": 1},
                     invitations={"sent": 2, "accepted": 2, "rejected": 0},
                     cost=summarise_cost(log.events), duration_s=duration)
        return list(log.events)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="Regenerate the checked-in example files")
    args = parser.parse_args()
    case = load_case(CASE_PATH)
    if args.write:
        events = run_mock_game(EVENT_LOG_PATH, case)
        OVERLAY_PATH.write_text(json.dumps(OVERLAY, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {EVENT_LOG_PATH} ({len(events)} events) and {OVERLAY_PATH}")
        return 0
    with tempfile.TemporaryDirectory(prefix="infra_example_", dir=ROOT) as temporary:
        events = run_mock_game(Path(temporary) / "example_game.jsonl", case)
    report = validate_events(events)
    export = to_annotation_exchanges(events, OVERLAY)
    print(json.dumps({
        "validation": report,
        "event_kinds_in_order": [event["kind"] for event in events],
        "export_for_josh": export,
        "evaluator_only_diagnostics": {
            "evidence": evidence_diagnostics(case, export["exchanges"], CONDITION, K),
            "contradictions": contradiction_diagnostics(case, export["exchanges"], export["reported_pairs"], CONDITION, K)},
        "cost_summary": summarise_cost(events)}, indent=2))
    print("Mock only: no API calls were made; usage is a character-count estimate, not measured tokens.")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

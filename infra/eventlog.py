"""Per-game append-only event log (JSONL), its validator, cost summary, and the
export to Josh's exchange format (examples/annotation_example.json).

One file per game: runs/<run_id>/<game_id>.jsonl. Every line is one event with
the envelope keys seq, ts, game_id, kind followed by flat kind-specific keys.
Standard library only; plain dicts at module boundaries.
"""

import copy
import datetime
import json
from pathlib import Path

from .llm import ROLES, PURPOSES, PHASES, ATTEMPT_KINDS, ATTEMPT_STATUSES, CALL_STATUSES

SCHEMA_VERSION = "1.0"
ENVELOPE_KEYS = ("seq", "ts", "game_id", "kind")
CONDITIONS = ("none", "raw", "ledger")
DECISION_TYPES = ("question", "accusation", "acceptance", "conversation_reply", "contradiction_report")
INVITATION_DECISIONS = ("accepted", "rejected", "no_slot")
VISIBILITIES = ("private", "detective")
EXTRACTION_STATUSES = ("ok", "repaired", "failed")
OUTCOMES = ("solved_first", "solved_retry", "unsolved", "failed_run")
EXCHANGE_KINDS = ("interrogation", "detective_conversation", "suspect_conversation", "invitation")

# Required keys per event kind. schemas/event_log.schema.json mirrors this table; a test keeps them equal.
REQUIRED_KEYS = {
    "game_start": ("schema_version", "run_id", "case_id", "case_sha256", "condition", "k", "turn_budget",
                   "accusation_budget", "seed", "model_id", "decoding", "prompt_versions", "pricing",
                   "manifest_sha256", "code_version"),
    "llm_call": ("call_id", "role", "purpose", "phase", "model_id", "decoding", "response_schema",
                 "max_output_tokens", "messages", "text", "parsed", "usage", "cost_usd", "finish_reason",
                 "latency_ms", "attempts", "repair_used", "status", "tags"),
    "memory_view": ("decision_index", "decision_type", "window_exchange_ids", "memory_payload", "call_id"),
    "interrogation": ("interrogation_index", "exchange_index", "exchange_id", "suspect", "question",
                      "question_call_id", "intent", "intent_call_id", "allowed_branches", "instructions",
                      "selected_branch_id", "response", "response_call_id", "controller_matched"),
    "checkpoint_start": ("checkpoint_index", "after_interrogation", "proposal_order", "seed_used"),
    "invitation": ("invitation_id", "checkpoint_index", "pass_no", "sender", "recipient", "decision",
                   "invitation_call_id", "decision_call_id"),
    "conversation": ("conversation_id", "checkpoint_index", "visibility", "exchange_id", "exchange_index",
                     "inviter", "recipient", "message", "reply", "message_call_id", "reply_call_id",
                     "controller_matched"),
    "checkpoint_end": ("checkpoint_index", "invitations_sent", "accepted", "rejected", "conversations_completed"),
    "extraction": ("exchange_id", "call_id", "status", "entries", "failure_entry"),
    "contradiction_report": ("call_id", "window_exchange_ids", "reported_pairs_raw"),
    "accusation": ("attempt", "triple", "valid", "correct", "feedback", "call_id", "exchange_index",
                   "interrogations_used", "testimony_frozen"),
    "failure": ("stage", "reason", "call_id", "detail"),
    "game_end": ("outcome", "attempts_used", "first_accusation_correct", "component_correct",
                 "interrogations_used", "exchanges_total", "checkpoints_reached", "conversations",
                 "invitations", "cost", "duration_s"),
}
KINDS = tuple(REQUIRED_KEYS)
CALL_ID_KEYS = ("call_id", "question_call_id", "intent_call_id", "response_call_id", "message_call_id",
                "reply_call_id", "invitation_call_id", "decision_call_id")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


class EventLog:
    """Append-only JSONL writer. Each emit() validates the kind and required keys, then flushes.

    Keep one EventLog per game. Use the typed helpers below from the controller;
    infra.llm.LLMClient writes `llm_call` events through emit() itself.
    """

    def __init__(self, path, game_id, clock=None):
        self.path = Path(path)
        self.game_id = game_id
        self.clock = clock or utc_now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self.seq = 0
        self.events = []

    def emit(self, kind, **payload):
        if kind not in REQUIRED_KEYS:
            raise ValueError(f"Unknown event kind {kind!r}; known kinds: {KINDS}")
        for key in ("seq", "ts", "kind"):
            if key in payload:
                raise ValueError(f"{key!r} is an envelope key and cannot be supplied")
        if "game_id" in payload:
            if payload["game_id"] not in (None, self.game_id):
                raise ValueError("Payload game_id does not match this log's game_id")
            payload.pop("game_id")
        missing = [key for key in REQUIRED_KEYS[kind] if key not in payload]
        if missing:
            raise ValueError(f"{kind} event is missing required keys {missing}")
        self.seq += 1
        event = {"seq": self.seq, "ts": self.clock(), "game_id": self.game_id, "kind": kind, **payload}
        line = json.dumps(event, ensure_ascii=False)
        self._handle.write(line + "\n")
        self._handle.flush()
        self.events.append(json.loads(line))
        return self.events[-1]

    def close(self):
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # Typed helpers: one per controller-emitted kind. Nullable keys default to None.
    def game_start(self, *, run_id, case_id, case_sha256, condition, k, turn_budget, accusation_budget,
                   seed, model_id, decoding, prompt_versions, pricing=None, manifest_sha256=None,
                   code_version=None):
        return self.emit("game_start", schema_version=SCHEMA_VERSION, run_id=run_id, case_id=case_id,
                         case_sha256=case_sha256, condition=condition, k=k, turn_budget=turn_budget,
                         accusation_budget=accusation_budget, seed=seed, model_id=model_id,
                         decoding=decoding, prompt_versions=prompt_versions, pricing=pricing,
                         manifest_sha256=manifest_sha256, code_version=code_version)

    def memory_view(self, *, decision_index, decision_type, window_exchange_ids, memory_payload, call_id):
        return self.emit("memory_view", decision_index=decision_index, decision_type=decision_type,
                         window_exchange_ids=list(window_exchange_ids), memory_payload=memory_payload,
                         call_id=call_id)

    def interrogation(self, *, interrogation_index, exchange_index, suspect, question, question_call_id,
                      intent, allowed_branches, instructions, response, response_call_id,
                      intent_call_id=None, selected_branch_id=None, controller_matched=None):
        matched = {"claim_ids": [], "lead_ids": [], "retraction_ids": []}
        matched.update(controller_matched or {})
        return self.emit("interrogation", interrogation_index=interrogation_index,
                         exchange_index=exchange_index, exchange_id=f"e{exchange_index}", suspect=suspect,
                         question=question, question_call_id=question_call_id, intent=intent,
                         intent_call_id=intent_call_id, allowed_branches=allowed_branches,
                         instructions=instructions, selected_branch_id=selected_branch_id,
                         response=response, response_call_id=response_call_id, controller_matched=matched)

    def checkpoint_start(self, *, checkpoint_index, after_interrogation, proposal_order, seed_used):
        return self.emit("checkpoint_start", checkpoint_index=checkpoint_index,
                         after_interrogation=after_interrogation, proposal_order=list(proposal_order),
                         seed_used=seed_used)

    def invitation(self, *, invitation_id, checkpoint_index, pass_no, sender, recipient, decision,
                   invitation_call_id=None, decision_call_id=None):
        return self.emit("invitation", invitation_id=invitation_id, checkpoint_index=checkpoint_index,
                         pass_no=pass_no, sender=sender, recipient=recipient, decision=decision,
                         invitation_call_id=invitation_call_id, decision_call_id=decision_call_id)

    def conversation(self, *, conversation_id, checkpoint_index, visibility, inviter, recipient, message,
                     reply, message_call_id, reply_call_id, exchange_index=None, controller_matched=None):
        matched = {"message_claim_ids": [], "reply_claim_ids": [], "proposal_claim_ids": []}
        matched.update(controller_matched or {})
        if visibility == "detective" and exchange_index is None:
            raise ValueError("A conversation with the detective enters memory and needs exchange_index")
        exchange_id = f"e{exchange_index}" if visibility == "detective" else None
        return self.emit("conversation", conversation_id=conversation_id, checkpoint_index=checkpoint_index,
                         visibility=visibility, exchange_id=exchange_id, exchange_index=exchange_index,
                         inviter=inviter, recipient=recipient, message=message, reply=reply,
                         message_call_id=message_call_id, reply_call_id=reply_call_id,
                         controller_matched=matched)

    def checkpoint_end(self, *, checkpoint_index, invitations_sent, accepted, rejected, conversations_completed):
        return self.emit("checkpoint_end", checkpoint_index=checkpoint_index, invitations_sent=invitations_sent,
                         accepted=accepted, rejected=rejected, conversations_completed=conversations_completed)

    def extraction(self, *, exchange_id, call_id, status, entries, failure_entry=None):
        return self.emit("extraction", exchange_id=exchange_id, call_id=call_id, status=status,
                         entries=entries, failure_entry=failure_entry)

    def contradiction_report(self, *, call_id, window_exchange_ids, reported_pairs_raw):
        return self.emit("contradiction_report", call_id=call_id, window_exchange_ids=list(window_exchange_ids),
                         reported_pairs_raw=reported_pairs_raw)

    def accusation(self, *, attempt, triple, valid, correct, feedback, call_id, exchange_index,
                   interrogations_used):
        return self.emit("accusation", attempt=attempt, triple=triple, valid=valid, correct=correct,
                         feedback=feedback, call_id=call_id, exchange_index=exchange_index,
                         interrogations_used=interrogations_used, testimony_frozen=True)

    def failure(self, *, stage, reason, call_id=None, detail=None):
        return self.emit("failure", stage=stage, reason=reason, call_id=call_id, detail=detail)

    def game_end(self, *, outcome, attempts_used, first_accusation_correct, component_correct,
                 interrogations_used, exchanges_total, checkpoints_reached, conversations, invitations,
                 cost, duration_s):
        return self.emit("game_end", outcome=outcome, attempts_used=attempts_used,
                         first_accusation_correct=first_accusation_correct, component_correct=component_correct,
                         interrogations_used=interrogations_used, exchanges_total=exchanges_total,
                         checkpoints_reached=checkpoints_reached, conversations=conversations,
                         invitations=invitations, cost=cost, duration_s=duration_s)


def read_events(path):
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def validate_events(events):
    """Structural and ordering checks for one game's events. Returns {"passed", "errors"}."""
    errors = []
    if not events:
        return {"passed": False, "errors": ["Empty event list"]}
    for index, event in enumerate(events):
        missing = [key for key in ENVELOPE_KEYS if key not in event]
        if missing:
            errors.append(f"Event {index}: missing envelope keys {missing}")
            continue
        if event["seq"] != index + 1:
            errors.append(f"Event {index}: seq {event['seq']} is not contiguous")
        if event["game_id"] != events[0]["game_id"]:
            errors.append(f"Event {event['seq']}: game_id differs from the first event")
        kind = event["kind"]
        if kind not in REQUIRED_KEYS:
            errors.append(f"Event {event['seq']}: unknown kind {kind!r}")
            continue
        missing = [key for key in REQUIRED_KEYS[kind] if key not in event]
        if missing:
            errors.append(f"Event {event['seq']} ({kind}): missing keys {missing}")
    if errors:
        return {"passed": False, "errors": errors}

    kinds = [event["kind"] for event in events]
    if kinds[0] != "game_start":
        errors.append("First event must be game_start")
    if kinds.count("game_start") != 1:
        errors.append("Exactly one game_start is required")
    if "game_end" in kinds and (kinds.count("game_end") != 1 or kinds[-1] != "game_end"):
        errors.append("game_end must be the single last event")
    start = events[0]
    if start["kind"] == "game_start" and start["condition"] not in CONDITIONS:
        errors.append(f"condition must be one of {CONDITIONS}")

    call_ids = [event["call_id"] for event in events if event["kind"] == "llm_call"]
    if len(call_ids) != len(set(call_ids)):
        errors.append("Duplicate llm_call call_ids")
    known_calls = set(call_ids)
    visible_ids, invitation_ids, conversation_ids = [], [], []
    expected_exchange, first_accusation_seq, last_attempt = 1, None, 0
    for event in events:
        kind = event["kind"]
        for key in CALL_ID_KEYS:
            if key in event and event[key] is not None and event[key] not in known_calls:
                errors.append(f"Event {event['seq']} ({kind}): {key} {event[key]!r} has no llm_call")
        if kind == "llm_call":
            if event["role"] not in ROLES or event["purpose"] not in PURPOSES or event["phase"] not in PHASES:
                errors.append(f"Event {event['seq']}: role/purpose/phase outside the vocabulary")
            if event["status"] not in CALL_STATUSES:
                errors.append(f"Event {event['seq']}: status {event['status']!r} unknown")
            for attempt in event["attempts"]:
                if attempt.get("kind") not in ATTEMPT_KINDS or attempt.get("status") not in ATTEMPT_STATUSES:
                    errors.append(f"Event {event['seq']}: attempt kind/status outside the vocabulary")
        elif kind == "memory_view" and event["decision_type"] not in DECISION_TYPES:
            errors.append(f"Event {event['seq']}: decision_type {event['decision_type']!r} unknown")
        elif kind in ("interrogation", "conversation"):
            if first_accusation_seq is not None:
                errors.append(f"Event {event['seq']} ({kind}): testimony is frozen after the first accusation")
            is_visible = kind == "interrogation" or event["visibility"] == "detective"
            if kind == "conversation":
                conversation_ids.append(event["conversation_id"])
                if event["visibility"] not in VISIBILITIES:
                    errors.append(f"Event {event['seq']}: visibility {event['visibility']!r} unknown")
                if event["visibility"] == "private" and event["exchange_id"] is not None:
                    errors.append(f"Event {event['seq']}: private conversations carry no exchange_id")
            if is_visible:
                visible_ids.append(event["exchange_id"])
                if event["exchange_index"] != expected_exchange or event["exchange_id"] != f"e{expected_exchange}":
                    errors.append(f"Event {event['seq']} ({kind}): expected exchange e{expected_exchange}, "
                                  f"got {event['exchange_id']!r}/{event['exchange_index']!r}")
                expected_exchange += 1
        elif kind == "invitation":
            invitation_ids.append(event["invitation_id"])
            if event["decision"] not in INVITATION_DECISIONS:
                errors.append(f"Event {event['seq']}: decision {event['decision']!r} unknown")
        elif kind == "extraction" and event["status"] not in EXTRACTION_STATUSES:
            errors.append(f"Event {event['seq']}: extraction status {event['status']!r} unknown")
        elif kind == "accusation":
            if first_accusation_seq is None:
                first_accusation_seq = event["seq"]
            if event["attempt"] != last_attempt + 1:
                errors.append(f"Event {event['seq']}: accusation attempts must be 1, 2, 3 in order")
            last_attempt = event["attempt"]
            if not isinstance(event["triple"], dict) or set(event["triple"]) != {"culprit", "weapon", "location"}:
                errors.append(f"Event {event['seq']}: triple must have culprit, weapon and location")
        elif kind == "game_end" and event["outcome"] not in OUTCOMES:
            errors.append(f"Event {event['seq']}: outcome {event['outcome']!r} unknown")
    for label, ids in (("exchange", visible_ids), ("invitation", invitation_ids), ("conversation", conversation_ids)):
        if len(ids) != len(set(ids)):
            errors.append(f"Duplicate {label} IDs")
    return {"passed": not errors, "errors": errors}


def trace_prefix_before_first_accusation(events):
    """Josh's adapters score the prefix ending immediately before the first accusation."""
    for index, event in enumerate(events):
        if event["kind"] == "accusation":
            return list(events[:index])
    return list(events)


def to_annotation_exchanges(events, overlay=None, until_first_accusation=True, claim_ids_from="annotations"):
    """Export one game to Josh's format: {"exchanges": [...], "reported_pairs": [...]}.

    Keys are exactly those of examples/annotation_example.json. claim_ids and
    annotations come from the annotation overlay (Elena's checked labels); without
    an overlay they are empty. claim_ids_from="controller_bookkeeping" fills
    claim_ids from the controller's online matches instead: a smoke test for the
    metrics pipeline only, never a source of reported results.
    """
    if claim_ids_from not in ("annotations", "controller_bookkeeping"):
        raise ValueError("claim_ids_from must be 'annotations' or 'controller_bookkeeping'")
    source = trace_prefix_before_first_accusation(events) if until_first_accusation else list(events)
    labels = (overlay or {}).get("exchanges", {})

    def build(exchange_id, kind, speaker, delivered_response, bookkeeping_ids):
        entry = {"exchange_id": exchange_id, "kind": kind, "speaker": speaker,
                 "delivered_response": delivered_response, "claim_ids": [], "annotations": []}
        if exchange_id in labels:
            entry["claim_ids"] = list(labels[exchange_id].get("claim_ids", []))
            entry["annotations"] = copy.deepcopy(labels[exchange_id].get("annotations", []))
            for annotation in entry["annotations"]:
                span = annotation["source_span"]
                if delivered_response[span["start"]:span["end"]] != span["text"]:
                    raise ValueError(f"Annotation span does not match delivered text in {exchange_id}")
        elif claim_ids_from == "controller_bookkeeping":
            entry["claim_ids"] = list(bookkeeping_ids)
        return entry

    exchanges = []
    for event in source:
        kind = event["kind"]
        if kind == "interrogation":
            exchanges.append(build(event["exchange_id"], "interrogation", event["suspect"], event["response"],
                                   event["controller_matched"].get("claim_ids", [])))
        elif kind == "invitation":
            exchanges.append(build(event["invitation_id"], "invitation", event["sender"], "", []))
        elif kind == "conversation":
            matched = event["controller_matched"]
            if event["visibility"] == "detective":
                exchanges.append(build(event["exchange_id"], "detective_conversation", event["inviter"],
                                       event["message"], matched.get("message_claim_ids", [])))
            else:
                exchanges.append(build(event["conversation_id"] + "_msg", "suspect_conversation", event["inviter"],
                                       event["message"], matched.get("message_claim_ids", [])))
                exchanges.append(build(event["conversation_id"] + "_reply", "suspect_conversation",
                                       event["recipient"], event["reply"], matched.get("reply_claim_ids", [])))
    exported_ids = {entry["exchange_id"] for entry in exchanges}
    unknown = set(labels) - exported_ids
    if unknown:
        raise ValueError(f"Overlay labels exchanges that are not in the exported trace: {sorted(unknown)}")
    reported_pairs = copy.deepcopy((overlay or {}).get("reported_pairs", []))
    for pair in reported_pairs:
        for side in ("left", "right"):
            if pair[side]["exchange_id"] not in exported_ids:
                raise ValueError(f"reported pair cites unknown exchange {pair[side]['exchange_id']!r}")
    return {"exchanges": exchanges, "reported_pairs": reported_pairs}


def _bucket():
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0,
            "cost_usd": 0.0}


def summarise_cost(events):
    """Token and USD totals by role, purpose, phase and attempt kind, from llm_call events."""
    calls = [event for event in events if event["kind"] == "llm_call"]
    summary = {"calls": len(calls), "by_role": {}, "by_purpose": {}, "by_phase": {}, "by_attempt_kind": {},
               "total_tokens": 0, "cost_usd": None, "calls_without_pricing": 0}
    priced_total = 0.0
    any_priced = False
    for call in calls:
        usage = call["usage"]
        for group, key in (("by_role", call["role"]), ("by_purpose", call["purpose"]), ("by_phase", call["phase"])):
            bucket = summary[group].setdefault(key, _bucket())
            bucket["calls"] += 1
            for name in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
                bucket[name] += usage[name]
            bucket["cost_usd"] += call["cost_usd"] or 0.0
        summary["total_tokens"] += usage["total_tokens"]
        if call["cost_usd"] is None:
            summary["calls_without_pricing"] += 1
        else:
            any_priced = True
            priced_total += call["cost_usd"]
        for attempt in call["attempts"]:
            bucket = summary["by_attempt_kind"].setdefault(attempt["kind"], {"attempts": 0, "total_tokens": 0})
            bucket["attempts"] += 1
            bucket["total_tokens"] += attempt["usage"]["total_tokens"]
    summary["cost_usd"] = priced_total if any_priced else None
    return summary

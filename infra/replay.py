"""Replayable traces: a readable transcript per game, the annotation export for Elena,
and a backend that re-serves recorded model responses with no API calls.

Everything here reads the per-game event log and never changes it.
"""

import json
from pathlib import Path

from .eventlog import read_events, to_annotation_exchanges, summarise_cost
from .llm import TransportError


class ReplayMismatch(Exception):
    """The replayed call's prompt differs from the recorded one (code or prompts changed)."""


class ReplayBackend:
    """Serve the recorded llm_call outcomes of one game, in call order.

    Each recorded call is replayed as its final attempt: a recorded success returns
    the recorded text and that attempt's usage; a recorded failure raises TransportError
    as many times as the original attempts, so the wrapper's retry policy reproduces
    the failure. With strict=True the prompt must equal the recorded one.
    """

    def __init__(self, events, strict=True):
        self.records = [event for event in events if event["kind"] == "llm_call"]
        self.strict = strict
        self.index = 0
        self._failures_left = None

    @property
    def exhausted(self):
        return self.index >= len(self.records)

    def generate(self, *, model_id, messages, decoding, response_schema=None, max_output_tokens=None):
        if self.exhausted:
            raise ReplayMismatch(f"Replay has no record for call {self.index + 1}")
        record = self.records[self.index]
        if self.strict and [dict(m) for m in messages] != record["messages"]:
            raise ReplayMismatch(f"Prompt for {record['call_id']} differs from the recorded one")
        if record["status"] != "ok":
            if self._failures_left is None:
                self._failures_left = len(record["attempts"])
            self._failures_left -= 1
            if self._failures_left <= 0:
                self._failures_left = None
                self.index += 1
            raise TransportError(f"replayed {record['status']}: {record['attempts'][-1]['error']}")
        self.index += 1
        final = record["attempts"][-1]
        return {"text": record["text"], "usage": dict(final["usage"]), "finish_reason": record["finish_reason"],
                "model_version": record.get("model_version")}


def render_transcript(events, include_private=True):
    """Markdown transcript of one game. Private conversations and evaluator-only facts are
    marked so a reader can tell detective-visible text from the rest."""
    lines = []
    for event in events:
        kind = event["kind"]
        if kind == "game_start":
            lines += [f"# Game {event['game_id']}",
                      f"Case {event['case_id']} | condition {event['condition']} | k={event['k']} | "
                      f"turn budget {event['turn_budget']} | seed {event['seed']} | model {event['model_id']}", ""]
        elif kind == "interrogation":
            lines += [f"## Turn {event['interrogation_index']} ({event['exchange_id']}): Detective -> {event['suspect']}",
                      f"**Detective:** {event['question']}",
                      f"**{event['suspect']}:** {event['response']}",
                      f"<sub>intent {json.dumps(event['intent'])} | branch {event['selected_branch_id']} | "
                      f"bookkeeping {json.dumps(event['controller_matched'])} (evaluator-only)</sub>", ""]
        elif kind == "checkpoint_start":
            lines += [f"## Checkpoint {event['checkpoint_index']} after interrogation {event['after_interrogation']}",
                      f"<sub>proposal order {', '.join(event['proposal_order'])}</sub>", ""]
        elif kind == "invitation":
            lines.append(f"- {event['invitation_id']}: {event['sender']} invites {event['recipient']} "
                         f"(pass {event['pass_no']}): {event['decision']}")
        elif kind == "conversation":
            if event["visibility"] == "detective":
                lines += ["", f"### Conversation {event['conversation_id']} ({event['exchange_id']}): "
                              f"{event['inviter']} -> Detective",
                          f"**{event['inviter']}:** {event['message']}", f"**Detective:** {event['reply']}", ""]
            elif include_private:
                lines += ["", f"### Private conversation {event['conversation_id']}: {event['inviter']} -> "
                              f"{event['recipient']} (suspect-only, never detective-visible)",
                          f"**{event['inviter']}:** {event['message']}", f"**{event['recipient']}:** {event['reply']}", ""]
            else:
                lines += ["", f"### Private conversation {event['conversation_id']} (omitted)", ""]
        elif kind == "checkpoint_end":
            lines += [f"<sub>checkpoint {event['checkpoint_index']}: {event['invitations_sent']} invitations, "
                      f"{event['accepted']} accepted, {event['conversations_completed']} conversations</sub>", ""]
        elif kind == "contradiction_report":
            lines.append(f"## Contradiction report (window {', '.join(event['window_exchange_ids']) or 'empty'})")
            for pair in event["reported_pairs_raw"]:
                lines.append(f"- {pair['left']['exchange_id']} \"{pair['left']['quote']}\" vs "
                             f"{pair['right']['exchange_id']} \"{pair['right']['quote']}\"")
            if not event["reported_pairs_raw"]:
                lines.append("- none reported")
            lines.append("")
        elif kind == "accusation":
            triple = event["triple"]
            lines += [f"## Accusation {event['attempt']}: {triple['culprit']} / {triple['weapon']} / {triple['location']} "
                      f"-> {'correct' if event['correct'] else 'incorrect'}", f"<sub>{event['feedback']}</sub>", ""]
        elif kind == "failure":
            lines += [f"## Failure at {event['stage']}: {event['reason']} ({event['call_id']})", ""]
        elif kind == "game_end":
            cost = event["cost"]
            lines += [f"## End: {event['outcome']} after {event['attempts_used']} accusation(s), "
                      f"{event['interrogations_used']} interrogations, {event['exchanges_total']} visible exchanges",
                      f"<sub>{cost['calls']} calls, {cost['total_tokens']} tokens, cost_usd={cost['cost_usd']}</sub>", ""]
    return "\n".join(lines).rstrip() + "\n"


def write_game_artifacts(log_path, include_private=True):
    """Write <game>.transcript.md and <game>.exchanges.json next to the log; returns their paths."""
    log_path = Path(log_path)
    events = read_events(log_path)
    stem = log_path.with_suffix("")
    transcript = Path(f"{stem}.transcript.md")
    exchanges = Path(f"{stem}.exchanges.json")
    transcript.write_text(render_transcript(events, include_private), encoding="utf-8")
    exchanges.write_text(json.dumps(to_annotation_exchanges(events), indent=2) + "\n", encoding="utf-8")
    return {"transcript": str(transcript), "exchanges": str(exchanges), "cost": summarise_cost(events)}

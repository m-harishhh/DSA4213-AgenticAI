"""Per-game annotation: build the annotator's input from one game's log + the case,
call the annotator, validate, and write the result next to the log -- the same
sibling-file convention infra.replay uses for <game>.transcript.md / .exchanges.json.
"""

import json
from pathlib import Path

from infra.eventlog import read_events

from .prompts import (build_messages, detective_visible_events, private_and_invitation_events,
                      undiscovered_leads)
from .schema import ANNOTATION_API_SCHEMA, ANNOTATOR_RUN_ID, to_storage_shape, validate_storage_shape

ANNOTATION_MAX_OUTPUT_TOKENS = 16000


class AnnotationValidationError(Exception):
    """Raised when an annotator response is internally well-formed (passes
    validate_storage_shape -- correct JSON shape) but inconsistent with the actual game
    transcript it is supposed to describe. validate_storage_shape alone cannot catch any
    of this: it only checks that the JSON has the right shape, never that its content is
    true of the transcript -- confirmed necessary by an independent review of the real
    main_01 run that found 51 exchange citations that were not exact substrings of
    anything the cited exchange actually said, and 3,713 more whose start/end offsets did
    not locate their own citation text, none of which validate_storage_shape rejected. An
    annotation with zero exchange_behaviour entries also passed validate_storage_shape
    before this check existed, since the schema has no way to know how many exchanges a
    given game actually has."""


def _source_texts(detective_visible_entry):
    """The text field(s) a citation on this exchange may legally quote from -- matches
    SYSTEM_PROMPT rule 1's "the exact substring of delivered_response or message text"."""
    if detective_visible_entry["kind"] == "interrogation":
        return {"delivered_response": detective_visible_entry["delivered_response"]}
    return {"message": detective_visible_entry["message"], "reply": detective_visible_entry["reply"]}


def _citation_text_found(citation, source_texts):
    return any(citation["text"] in value for value in source_texts.values())


def _citation_offsets_correct(citation, source_texts):
    start, end = citation["start"], citation["end"]
    return any(value[start:end] == citation["text"] for value in source_texts.values())


def validate_against_transcript(stored, detective_visible, private_conversations, case):
    """Check an already shape-valid annotation against the real transcript it describes.

    detective_visible: annotation.prompts.detective_visible_events(events) -- the exact
    rendering the model was shown. private_conversations: the subset of
    annotation.prompts.private_and_invitation_events(events) that has a conversation_id
    (invitation entries don't). case: the loaded private case.

    Raises AnnotationValidationError (collecting every problem found, not just the
    first) if: the exchange_behaviour keys don't exactly match the detective-visible
    transcript's exchange_ids (missing, extra, or an empty transcript); any citation's
    text is not an exact substring of the exchange it cites, or its start/end does not
    locate that substring; or any referenced exchange_id/lead_id/claim_id/suspect/
    conversation_id does not actually exist in this game's case or transcript.
    """
    problems = []
    by_id = {entry["exchange_id"]: entry for entry in detective_visible}
    expected_ids = set(by_id)
    actual_ids = set(stored["exchange_behaviour"])
    missing = expected_ids - actual_ids
    extra = actual_ids - expected_ids
    if not expected_ids:
        problems.append("detective-visible transcript has no exchanges at all -- nothing to validate against")
    if missing:
        problems.append(f"exchange_behaviour is missing entries for {sorted(missing)}")
    if extra:
        problems.append(f"exchange_behaviour has unknown exchange_id(s) {sorted(extra)}")

    lead_ids = set(case["leads"])
    claim_ids = set(case["claims"])
    suspects = set(case["public"]["suspects"])
    conversations_by_id = {entry["conversation_id"]: entry for entry in private_conversations}

    def check_citation(label, citation, source_texts):
        if citation is None:
            return
        if not _citation_text_found(citation, source_texts):
            problems.append(f"{label}: citation text is not an exact substring of anything this exchange said: {citation!r}")
        elif not _citation_offsets_correct(citation, source_texts):
            problems.append(f"{label}: citation text exists but start/end does not locate it: {citation!r}")

    for exchange_id, entry in stored["exchange_behaviour"].items():
        source = by_id.get(exchange_id)
        if source is None:
            continue  # already reported as "extra" above
        source_texts = _source_texts(source)
        check_citation(exchange_id, entry.get("citation"), source_texts)
        for addition in entry["unsupported_additions"]:
            check_citation(f"{exchange_id} unsupported_addition", addition["citation"], source_texts)
        matches_id = entry["repeated_question"]["matches_exchange_id"]
        if matches_id is not None and matches_id not in expected_ids:
            problems.append(f"{exchange_id}: repeated_question.matches_exchange_id references unknown exchange_id {matches_id!r}")
        for claim_id in entry["challenge"]["cited_claim_ids"]:
            if claim_id not in claim_ids:
                problems.append(f"{exchange_id}: challenge.cited_claim_ids references unknown claim_id {claim_id!r}")
        for new_lead_id in entry["lead_follow_up"]["newly_disclosed_lead_ids"]:
            if new_lead_id not in lead_ids:
                problems.append(f"{exchange_id}: lead_follow_up.newly_disclosed_lead_ids references unknown lead_id {new_lead_id!r}")
        follow_up_lead_id = entry["lead_follow_up"]["is_follow_up_to_lead_id"]
        if follow_up_lead_id is not None and follow_up_lead_id not in lead_ids:
            problems.append(f"{exchange_id}: lead_follow_up.is_follow_up_to_lead_id references unknown lead_id {follow_up_lead_id!r}")
        claim_if_known = entry["deception"]["claim_id_if_known"]
        if claim_if_known is not None and claim_if_known not in claim_ids:
            problems.append(f"{exchange_id}: deception.claim_id_if_known references unknown claim_id {claim_if_known!r}")

    for missed in stored["missed_required_disclosures"]:
        ref = missed["lead_id_or_claim_id"]
        if ref not in lead_ids and ref not in claim_ids:
            problems.append(f"missed_required_disclosures: unknown lead_id_or_claim_id {ref!r}")
        if missed["suspect"] not in suspects:
            problems.append(f"missed_required_disclosures: unknown suspect {missed['suspect']!r}")

    for instance in stored["voluntary_interaction"]["coordination_instances"]:
        conversation = conversations_by_id.get(instance["conversation_id"])
        if conversation is None:
            problems.append(f"coordination_instances: unknown conversation_id {instance['conversation_id']!r}")
            continue
        check_citation(f"coordination_instances[{instance['conversation_id']}]", instance["citation"],
                       {"message": conversation["message"], "reply": conversation["reply"]})
        for eid in instance["subsequent_testimony_consistent_exchange_ids"]:
            if eid not in expected_ids:
                problems.append(f"coordination_instances[{instance['conversation_id']}]: unknown exchange_id {eid!r}")

    if problems:
        raise AnnotationValidationError("; ".join(problems))


def annotate_game(log_path, case, llm, game_id=None, out_path=None):
    """Run one annotation call for one game's log.

    log_path: that game's <game_id>.jsonl (infra.eventlog format).
    case: the loaded private case (casework.load_case) -- evaluator-only data the
          annotator needs, never the detective's public_view alone.
    llm: an infra.llm.LLMClient already configured with the annotation run's model/
         pricing/log (or log=None to skip per-call event logging).
    out_path: where to write the stored result; defaults to <game>.behaviour.json next
              to the log. Pass out_path=False to skip writing (e.g. in tests).

    Returns (record, stored): record is the raw llm_call record (cost, usage, etc.);
    stored is the dict-keyed shape already validated against
    schemas/annotation_behaviour.schema.json AND against the real transcript (see
    validate_against_transcript). Raises infra.LLMCallError, uncaught, if the bounded
    transport retries/format repair are exhausted; raises AnnotationValidationError,
    also uncaught, if the response is shape-valid but inconsistent with the transcript
    (bad citation, bad offset, wrong exchange coverage, unknown id reference) -- callers
    (see orchestrate.annotate_run) must treat either the same way run_game treats a
    failed run: log it and move on, never silently skip the game from operational
    accounting. orchestrate.annotate_one already catches both as a "crashed" result,
    which is retried on the next annotate_run call the same as any other failure.
    """
    log_path = Path(log_path)
    events = read_events(log_path)
    if game_id is None:
        game_id = next(event["game_id"] for event in events if event.get("game_id"))

    record = llm.call(role="annotator", purpose="annotation", phase="post_game_analysis",
                      messages=build_messages(case, events), game_id=game_id,
                      response_schema=ANNOTATION_API_SCHEMA,
                      max_output_tokens=ANNOTATION_MAX_OUTPUT_TOKENS)

    stored = to_storage_shape(record["parsed"], game_id, ANNOTATOR_RUN_ID,
                              undiscovered_leads=undiscovered_leads(events, case))
    validate_storage_shape(stored)
    private_conversations = [entry for entry in private_and_invitation_events(events) if "conversation_id" in entry]
    validate_against_transcript(stored, detective_visible_events(events), private_conversations, case)

    if out_path is not False:
        stem = log_path.with_suffix("")
        out_path = Path(out_path) if out_path else Path(f"{stem}.behaviour.json")
        out_path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
        # Full raw call record (prompt, response, usage) kept for audit/replay, same
        # spirit as infra's per-game logs -- without using infra.eventlog.EventLog,
        # which validates a whole game's event sequence, not one post-hoc call.
        Path(f"{stem}.annotation_call.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    return record, stored

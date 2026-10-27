"""Per-game annotation: build the annotator's input from one game's log + the case,
call the annotator, validate, and write the result next to the log -- the same
sibling-file convention infra.replay uses for <game>.transcript.md / .exchanges.json.
"""

import json
from pathlib import Path

from infra.eventlog import read_events

from .prompts import build_messages
from .schema import ANNOTATION_API_SCHEMA, ANNOTATOR_RUN_ID, to_storage_shape, validate_storage_shape

ANNOTATION_MAX_OUTPUT_TOKENS = 16000


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
    schemas/annotation_behaviour.schema.json. Raises infra.LLMCallError, uncaught, if
    the bounded transport retries/format repair are exhausted -- callers (see
    orchestrate.annotate_run) must treat that the same way run_game treats a failed
    run: log it and move on, never silently skip the game from operational accounting.
    """
    log_path = Path(log_path)
    events = read_events(log_path)
    if game_id is None:
        game_id = next(event["game_id"] for event in events if event.get("game_id"))

    record = llm.call(role="annotator", purpose="annotation", phase="post_game_analysis",
                      messages=build_messages(case, events), game_id=game_id,
                      response_schema=ANNOTATION_API_SCHEMA,
                      max_output_tokens=ANNOTATION_MAX_OUTPUT_TOKENS)

    stored = to_storage_shape(record["parsed"], game_id, ANNOTATOR_RUN_ID)
    validate_storage_shape(stored)

    if out_path is not False:
        stem = log_path.with_suffix("")
        out_path = Path(out_path) if out_path else Path(f"{stem}.behaviour.json")
        out_path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
        # Full raw call record (prompt, response, usage) kept for audit/replay, same
        # spirit as infra's per-game logs -- without using infra.eventlog.EventLog,
        # which validates a whole game's event sequence, not one post-hoc call.
        Path(f"{stem}.annotation_call.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    return record, stored

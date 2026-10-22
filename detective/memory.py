from infra import LLMCallError

from .action_schema import SCHEMA_CONFIG
from .prompts import (EXTRACTION_PROMPT, LEDGER_SCHEMA, MEMORY_LABELS, render_extraction_user,
                      render_ledger_block, render_memory_payload, render_raw_block)

CONDITIONS = ("none", "raw", "ledger")
# Only exchanges the detective took part in enter memory; suspect conversations and invitations never do.
ELIGIBLE_KINDS = ("interrogation", "detective_conversation")


def build_memory(condition, exchanges, ledger, k):
    """
    condition is one of none/raw/ledger
    none: latest exchange only
    raw and ledger: latest K exchanges (same window ids for the same input: matched horizon)

    returns: {window_exchange_ids, payload, visible_exchanges}
    where visible_exchange = {exchange_id: text} --> feeds DecisionContext, never the prompt
    """
    if condition not in CONDITIONS:
        raise ValueError(f"condition must be one of {CONDITIONS}, got {condition!r}")
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError(f"k must be a positive int, got {k!r}")

    eligible = [exchange for exchange in exchanges if exchange.get("kind") in ELIGIBLE_KINDS]
    window = eligible[-1:] if condition == "none" else eligible[-k:]

    if condition == "ledger":
        # Only ledger keys for window exchanges are read, so expired entries never appear.
        blocks = [render_ledger_block(exchange, (ledger or {}).get(exchange["exchange_id"]))
                  for exchange in window]
    else:
        blocks = [render_raw_block(exchange) for exchange in window]

    return {"window_exchange_ids": [exchange["exchange_id"] for exchange in window],
            "payload": render_memory_payload(MEMORY_LABELS[condition].format(k=k), blocks),
            "visible_exchanges": {exchange["exchange_id"]: exchange["text"] for exchange in window}}


def extract_ledger(exchange, llm, game_id):
    '''
    This function extracts entries for one delivered exchange (§4.2)
    The extractor sees only this exchange's id and text
    Each entry keeps speaker, statement and source_span, plus span offsets into exchange["text"]
    returns: {entries, status, call_id, failure_entry}
    '''
    messages = [{"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": render_extraction_user(exchange)}]
    try:
        # The wrapper does the single format repair.
        record = llm.call(role="extractor", purpose="ledger_extraction", phase="through_first_accusation",
                          messages=messages, game_id=game_id, response_schema=LEDGER_SCHEMA,
                          max_output_tokens=SCHEMA_CONFIG["max_extraction_tokens"])
    except LLMCallError as exc:
        if exc.record["status"] != "format_failure":
            raise
        return {"entries": [], "status": "failed", "call_id": exc.record["call_id"],
                "failure_entry": {"type": "extraction_failure", "exchange_id": exchange["exchange_id"],
                                  "reason": "schema failure after one repair"}}

    text = exchange["text"]
    entries = []
    for parsed in record["parsed"]["entries"]:
        entry = dict(parsed, exchange_id=exchange["exchange_id"])
        start = text.find(entry["source_span"]) if entry["source_span"] else -1
        # Unverified spans are kept, not repaired, so evaluation can count them as unsupported.
        if start == -1:
            entry.update(span=None, span_verified=False)
        else:
            entry.update(span={"start": start, "end": start + len(entry["source_span"])}, span_verified=True)
        entries.append(entry)

    return {"entries": entries, "status": "repaired" if record["repair_used"] else "ok",
            "call_id": record["call_id"], "failure_entry": None}

"""The annotator's API-facing response schema, and the conversion to storage shape.

infra.llm.check_shape (used for both the wrapper's own format-repair check and as the
literal response_schema sent to Gemini's structured output) is deliberately not a full
JSON Schema validator: it has no $ref/$defs support and, critically, no way to validate
"every value in this object matches schema X" for an object keyed by dynamic IDs (see
its docstring in infra/llm.py). schemas/annotation_behaviour.schema.json keys
exchange_behaviour by exchange_id (e1, e2, ...) for exactly that reason it cannot be
used as-is for the live call.

The project's own established fix for this (see detective/prompts.py's LEDGER_SCHEMA)
is: ask the model for an ARRAY of self-identifying objects instead of a dynamic-keyed
object. ANNOTATION_API_SCHEMA below follows that convention. to_storage_shape() then
converts the array the model actually returns into the dict-keyed shape that matches
schemas/annotation_behaviour.schema.json, which the jsonschema library (not
check_shape) can validate fully, including the dynamic keys.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORAGE_SCHEMA_PATH = ROOT / "schemas" / "annotation_behaviour.schema.json"

ANNOTATOR_RUN_ID = "llm_v1"

_LEAD_FOLLOW_UP = {
    "type": "object", "additionalProperties": False,
    "required": ["newly_disclosed_lead_ids", "is_follow_up_to_lead_id", "follow_up_relevance"],
    "properties": {
        "newly_disclosed_lead_ids": {"type": "array", "items": {"type": "string"}},
        "is_follow_up_to_lead_id": {"type": ["string", "null"]},
        "follow_up_relevance": {"type": ["string", "null"], "enum": ["relevant", "off_target", "no_opportunity", None]},
    },
}
_CORROBORATION = {
    "type": "object", "additionalProperties": False,
    "required": ["is_request", "target_subject", "target_time"],
    "properties": {
        "is_request": {"type": "boolean"},
        "target_subject": {"type": ["string", "null"]},
        "target_time": {"type": ["string", "null"]},
    },
}
_CHALLENGE = {
    "type": "object", "additionalProperties": False,
    "required": ["is_challenge", "evidence_based", "cited_claim_ids"],
    "properties": {
        "is_challenge": {"type": "boolean"},
        "evidence_based": {"type": ["boolean", "null"]},
        "cited_claim_ids": {"type": "array", "items": {"type": "string"}},
    },
}
_REPEATED_QUESTION = {
    "type": "object", "additionalProperties": False,
    "required": ["is_repeat", "repeat_kind", "matches_exchange_id"],
    "properties": {
        "is_repeat": {"type": "boolean"},
        "repeat_kind": {"type": ["string", "null"], "enum": ["unnecessary", "expired_evidence_recovery", None]},
        "matches_exchange_id": {"type": ["string", "null"]},
    },
}
_CITATION = {
    "type": "object", "additionalProperties": False,
    "required": ["start", "end", "text"],
    "properties": {
        "start": {"type": "integer"}, "end": {"type": "integer"}, "text": {"type": "string"},
    },
}
_UNSUPPORTED_ADDITION = {
    "type": "object", "additionalProperties": False,
    "required": ["citation", "note"],
    "properties": {"citation": _CITATION, "note": {"type": "string"}},
}
_WITHHOLDING = {
    "type": "object", "additionalProperties": False,
    "required": ["explicit_refusal", "partial_withholding"],
    "properties": {"explicit_refusal": {"type": "boolean"}, "partial_withholding": {"type": "boolean"}},
}
_DECEPTION = {
    "type": "object", "additionalProperties": False,
    "required": ["contains_false_statement", "authorised", "claim_id_if_known"],
    "properties": {
        "contains_false_statement": {"type": "boolean"},
        "authorised": {"type": ["boolean", "null"]},
        "claim_id_if_known": {"type": ["string", "null"]},
    },
}
_EXCHANGE_ENTRY = {
    "type": "object", "additionalProperties": False,
    "required": ["exchange_id", "lead_follow_up", "corroboration", "challenge", "repeated_question",
                "unsupported_additions", "withholding", "deception", "confidence"],
    "properties": {
        "exchange_id": {"type": "string"},
        "lead_follow_up": _LEAD_FOLLOW_UP,
        "corroboration": _CORROBORATION,
        "challenge": _CHALLENGE,
        "repeated_question": _REPEATED_QUESTION,
        "unsupported_additions": {"type": "array", "items": _UNSUPPORTED_ADDITION},
        "withholding": _WITHHOLDING,
        "deception": _DECEPTION,
        "confidence": {"type": "string", "enum": ["high", "uncertain"]},
        "citation": _CITATION,
    },
}
_MISSED_DISCLOSURE = {
    "type": "object", "additionalProperties": False,
    "required": ["lead_id_or_claim_id", "suspect"],
    "properties": {
        "lead_id_or_claim_id": {"type": "string"}, "suspect": {"type": "string"}, "note": {"type": "string"},
    },
}
_COORDINATION_INSTANCE = {
    "type": "object", "additionalProperties": False,
    "required": ["conversation_id", "proposal_to_conceal_or_align", "agreement",
                "subsequent_testimony_consistent_exchange_ids", "citation"],
    "properties": {
        "conversation_id": {"type": "string"},
        "proposal_to_conceal_or_align": {"type": "boolean"},
        "agreement": {"type": "string", "enum": ["agreed", "disagreed", "unclear"]},
        "subsequent_testimony_consistent_exchange_ids": {"type": "array", "items": {"type": "string"}},
        "citation": _CITATION,
    },
}

ANNOTATION_API_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["exchanges", "missed_required_disclosures", "voluntary_interaction", "annotator_notes"],
    "properties": {
        "exchanges": {"type": "array", "items": _EXCHANGE_ENTRY},
        "missed_required_disclosures": {"type": "array", "items": _MISSED_DISCLOSURE},
        "voluntary_interaction": {
            "type": "object", "additionalProperties": False,
            "required": ["checkpoints_reached", "coordination_instances"],
            "properties": {
                "checkpoints_reached": {"type": "integer"},
                "coordination_instances": {"type": "array", "items": _COORDINATION_INSTANCE},
            },
        },
        "annotator_notes": {"type": "string"},
    },
}


def to_storage_shape(parsed, game_id, annotator_run_id=ANNOTATOR_RUN_ID, undiscovered_leads=None):
    """The model's array-of-entries response -> the dict-keyed shape in
    schemas/annotation_behaviour.schema.json, ready for jsonschema.validate and for
    merging against Aniroodh's overlay by shared exchange_id keys.

    undiscovered_leads is never part of the model's own response (ANNOTATION_API_SCHEMA
    has no such field) -- it is a pure structural fact computed directly from the event
    log by annotation.prompts.undiscovered_leads and merged in here, the same way
    checkpoints_reached_count's answer is handed to the model rather than asked for.
    Defaults to [] so existing callers (e.g. tests) that don't pass it still produce a
    schema-valid result."""
    exchange_behaviour = {}
    for entry in parsed["exchanges"]:
        entry = dict(entry)
        exchange_id = entry.pop("exchange_id")
        if exchange_id in exchange_behaviour:
            raise ValueError(f"Duplicate exchange_id {exchange_id!r} in annotator response")
        exchange_behaviour[exchange_id] = entry
    return {
        "game_id": game_id,
        "annotator_run_id": annotator_run_id,
        "exchange_behaviour": exchange_behaviour,
        "missed_required_disclosures": parsed["missed_required_disclosures"],
        "undiscovered_leads": undiscovered_leads if undiscovered_leads is not None else [],
        "voluntary_interaction": parsed["voluntary_interaction"],
        "annotator_notes": parsed["annotator_notes"],
    }


def load_storage_schema():
    return json.loads(STORAGE_SCHEMA_PATH.read_text())


def validate_storage_shape(stored):
    import jsonschema  # requirements.txt: needs $ref/$defs and dynamic-key support, unlike infra.llm.check_shape
    jsonschema.validate(stored, load_storage_schema())

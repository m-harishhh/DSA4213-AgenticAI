"""Detective prompt text and rendering.

Only the memory labels may differ between memory conditions (§4.3); everything else here is
identical across conditions and turns. Case rendering reads public_view fields only.
"""

PROMPT_VERSION = "detective-v1"

SYSTEM_PROMPT = """You are a celebrated private detective with a gentleman's manners and a patient ear, called to the scene to find who did it, with what, and where.

Each turn you either interrogate one suspect with a single question, or accuse a culprit, weapon and room. Use names exactly as listed in the case. Every accusation must be a combination you have not accused before. When no interrogation turns are left, you must accuse.

Follow up on leads people mention, and compare accounts against each other.

Reply with exactly one JSON object and nothing else, in one of these two forms.

To interrogate ("cites" is optional: exchange IDs from your memory that your question relies on):
{"action": "interrogate", "suspect": "<suspect name>", "question": "<your question>", "cites": ["<exchange_id>"]}

To accuse ("contradiction_report" is required on your first accusation: pairs of exact quotes from exchanges in your memory that contradict each other; use [] if you found none):
{"action": "accuse", "culprit": "<suspect name>", "weapon": "<weapon>", "location": "<room>", "contradiction_report": [{"left": {"exchange_id": "<exchange_id>", "quote": "<exact words>"}, "right": {"exchange_id": "<exchange_id>", "quote": "<exact words>"}}]}"""

# Filled in by build_memory; {k} is the memory horizon.
MEMORY_LABELS = {
    "none": "Your most recent exchange:",
    "raw": "Your last {k} exchanges, word for word:",
    "ledger": "Your notes from your last {k} exchanges:",
}
EMPTY_MEMORY = "(No exchanges yet.)"

NEXT_STEP = "Decide your next action. Reply with exactly one JSON object and nothing else."

REPAIR_TEMPLATE = "Your previous reply was invalid: {errors}. Reply again with exactly one valid JSON object."


def _profile_lines(names, profiles):
    lines = []
    for name in names:
        features = ", ".join(f"{feature} {value}" for feature, value in profiles.get(name, {}).items())
        lines.append(f"- {name}: {features}" if features else f"- {name}")
    return lines


def render_public_case(public):
    """Render the public case. honesty_policy and intent_contract are deliberately not rendered."""
    profiles = public["profiles"]
    sections = [
        "CASE\n" + public["description"],
        "SUSPECTS\n" + "\n".join(_profile_lines(public["suspects"], profiles["suspects"])),
        "WEAPONS\n" + "\n".join(_profile_lines(public["weapons"], profiles["weapons"])),
        "ROOMS\n" + "\n".join(_profile_lines(public["locations"], profiles["locations"])),
        "TIMES: " + ", ".join(public["times"]) + "\nMURDER TIME: " + public["murder_time"],
        "RULES\n" + "\n".join(f"- {rule}" for rule in public["rules"]),
    ]
    return "\n\n".join(sections)


def render_status(context):
    lines = [f"- Interrogation turns left: {context.turns_left}",
             f"- Accusations used: {len(context.previous_accusations)} of {context.config['max_accusations']}"]
    # The game ends on a correct accusation, so every earlier one the detective sees was incorrect.
    for culprit, weapon, location in context.previous_accusations:
        lines.append(f"- Accused {culprit} with {weapon} in {location}: incorrect")
    if context.turns_left == 0:
        lines.append("- No interrogation turns left: you must accuse now.")
    if not context.previous_accusations:
        lines.append('- If you accuse now, you must include "contradiction_report": a list of '
                     '{"left": {"exchange_id", "quote"}, "right": {"exchange_id", "quote"}} pairs '
                     "(may be empty), citing only exchange IDs shown in your memory.")
    return "STATUS\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Memory payload rendering (used by build_memory)
# ---------------------------------------------------------------------------
def _known(value):
    return value if value else "unknown"


def render_raw_block(exchange):
    return f"[{exchange['exchange_id']}]\n{exchange['text']}"


def render_ledger_entry(entry):
    speaker = f"speaker: {_known(entry.get('speaker'))}"
    quote = f'"{entry.get("source_span", "")}"'
    if entry["type"] == "unresolved_lead":
        fields = ["unresolved lead", speaker, f"ask: {_known(entry.get('target'))}",
                  f"topic: {_known(entry.get('topic'))}"]
    elif entry.get("retracts"):
        fields = [f"(retraction) {entry['type']}", speaker, "retracts: yes"]
    else:
        fields = [entry["type"], speaker, f"entities: {_known(', '.join(entry.get('entities') or []))}",
                  f"time: {_known(entry.get('time'))}"]
    return "- " + " | ".join(fields + [quote])


def render_ledger_block(exchange, entries):
    """entries is None when the exchange has no ledger key."""
    speaker = exchange.get("speaker")
    header = f"[{exchange['exchange_id']}] {speaker}" if speaker else f"[{exchange['exchange_id']}]"
    if entries and any(entry.get("type") == "extraction_failure" for entry in entries):
        lines = ["(extraction failed for this exchange)"]
    elif not entries:
        lines = ["(no notes)"]
    else:
        lines = [render_ledger_entry(entry) for entry in entries]
    return header + "\n" + "\n".join(lines)


def render_memory_payload(label, blocks):
    if not blocks:
        return label + "\n" + EMPTY_MEMORY
    return label + "\n" + "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Ledger extraction (§4.2). The extractor sees one exchange only: no case data, no other exchanges.
# ---------------------------------------------------------------------------
EXTRACTOR_VERSION = "extractor-v1"

EXTRACTION_PROMPT = """You take notes on one exchange from an investigation. You see only this exchange.

Extract every explicit claim, observation, refusal and unresolved lead in the exchange, one entry each, and attribute each entry to the person who said it.
- type: "claim", "observation", "refusal" or "unresolved_lead".
- speaker: who said it.
- statement: what was said, briefly.
- entities: the people, objects and places named in it, only if stated; otherwise [].
- time: the time it refers to, only if stated; otherwise null.
- A lead pointing to another person is an "unresolved_lead": target is that person and topic is what to ask them. Otherwise target and topic are null.
- A retraction of something said earlier is a "claim" with retracts true; otherwise retracts is false.
- source_span: the exact words copied from the exchange text.
Do not infer truth, guilt or links to other exchanges. Leave references you cannot resolve unresolved rather than guessing.

Reply with exactly one JSON object and nothing else, in this form:
{"entries": [{"type": "claim", "speaker": "<name>", "statement": "<what was said>", "entities": ["<name>"], "time": "<time or null>", "target": null, "topic": null, "retracts": false, "source_span": "<exact words>"}]}"""

_NULLABLE_STRING = {"type": ["string", "null"]}
LEDGER_ENTRY_FIELDS = ("type", "speaker", "statement", "entities", "time", "target", "topic", "retracts",
                       "source_span")
LEDGER_SCHEMA = {
    "type": "object",
    "required": ["entries"],
    "additionalProperties": False,
    "properties": {"entries": {"type": "array", "items": {
        "type": "object",
        "required": list(LEDGER_ENTRY_FIELDS),
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string", "enum": ["claim", "observation", "refusal", "unresolved_lead"]},
            "speaker": {"type": "string"},
            "statement": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "time": _NULLABLE_STRING,
            "target": _NULLABLE_STRING,
            "topic": _NULLABLE_STRING,
            "retracts": {"type": "boolean"},
            "source_span": {"type": "string"},
        },
    }}},
}


def render_extraction_user(exchange):
    return f"Exchange {exchange['exchange_id']}:\n{exchange['text']}"

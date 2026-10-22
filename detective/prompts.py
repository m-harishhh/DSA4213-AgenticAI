"""Detective prompt text and rendering.

Only the memory labels may differ between memory conditions (§4.3); everything else here is
identical across conditions and turns. Rendering reads public_view fields only.
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

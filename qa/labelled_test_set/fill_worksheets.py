"""Run from repo root: python qa/labelled_test_set/fill_worksheets.py

Fills in the three blank human-review worksheets (pilot_07b_*_worksheet.md) using the
already hand-built, schema-validated overlay/behaviour JSON from
build_pilot07b_{none,ledger,raw}.py. The JSON is the ground truth (every citation in it
is a verified real substring of the transcript, checked exchange-by-exchange against
the live event log); this script renders that same ground truth into the worksheet's
blank-filling format so the two artifacts cannot drift apart. Idempotent: always
rebuilds the blank template fresh via build_review_worksheet.render_game() rather than
mutating whatever is currently on disk, so running this twice in a row is safe.
Strips the mechanical "system hints" scaffolding (controller bookkeeping dumps,
candidate-repeat flags) once the real judgment is filled in, since those were only
useful before the fields were resolved. Overwrites each worksheet in place.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from casework import load_case  # noqa: E402
import build_review_worksheet as worksheet_template  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent

GAMES = [
    ("none", "pilot_07b_none_1118654058"),
    ("ledger", "pilot_07b_ledger_444382084"),
    ("raw", "pilot_07b_raw_1569954369"),
]

FIELD_BLOCK_RE = re.compile(
    r"- Lead follow-up.*?\n- Corroboration.*?\n- Challenge.*?\n- Repeated question.*?\n"
    r"- Unsupported additions.*?\n- Withholding.*?\n- Deception.*?\n- Confidence.*?\n- Citation.*?\n"
)
EXCHANGE_HEADER_RE = re.compile(r"^### (e\d+) -- ", re.M)

PER_EXCHANGE_HINT_RE = re.compile(r"System hints:\n(?:  - .*\n)+\n")
INTRO_HINT_SENTENCE = (
    " Lines starting with '  -' under each exchange are SYSTEM HINTS "
    "(mechanical lookups only, not judgments -- verify before using)."
)
# Matches the whole missed-disclosures header (including its trailing hint clause), the
# hint line, and the blank answer marker in one pass, so the punctuation left behind
# (the final "):") is constructed deliberately rather than patched up after the fact.
MISSED_DISCLOSURES_BLOCK_RE = re.compile(
    r"(\*\*Missed required disclosures\*\* \([^)]*\))"
    r"\. System hint -- leads never mentioned as newly_disclosed in any bookkeeping above "
    r"\(verify, since bookkeeping misses paraphrases\):\n\n"
    r"  - System hint: leads with no bookkeeping hit at all: \[.*?\]\n\n"
    r"_____\n"
)


def strip_system_hints(text):
    """Remove every mechanical-hint scaffold from the blank template: the per-exchange
    'System hints:' blocks and the intro sentence describing them. What remains is the
    question/response and the (still blank) judgment fields, ready to be filled with the
    real, resolved judgment. The missed-disclosures hint is handled separately in
    fill_worksheet(), in the same pass that fills in its resolved answer."""
    text = PER_EXCHANGE_HINT_RE.sub("", text)
    text = text.replace(INTRO_HINT_SENTENCE, "")
    return text


def yn(value):
    """None renders as 'n/a': in this dataset null always means the question was moot
    (e.g. evidence_based/authorised when is_challenge/contains_false_statement is False),
    never an unresolved judgment on an actual positive label."""
    if value is True:
        return "Y"
    if value is False:
        return "N"
    return "n/a"


def list_or_none(items):
    return ", ".join(items) if items else "none"


def render_field_block(eb, claim_ids):
    lf = eb["lead_follow_up"]
    co = eb["corroboration"]
    ch = eb["challenge"]
    rq = eb["repeated_question"]
    ua = eb["unsupported_additions"]
    wh = eb["withholding"]
    dc = eb["deception"]
    citation = eb.get("citation")
    citation_text = citation["text"] if citation else "n/a"
    lines = [
        f"- Lead follow-up — newly disclosed lead id(s): {list_or_none(lf['newly_disclosed_lead_ids'])}  |  "
        f"follow-up to lead id: {lf['is_follow_up_to_lead_id'] or 'none'}  |  "
        f"relevance: {lf['follow_up_relevance'] or 'n/a'}",
        f"- Corroboration — is this a corroboration request? (Y/N): {yn(co['is_request'])}  |  "
        f"target subject: {co['target_subject'] or 'n/a'}  |  target time: {co['target_time'] or 'n/a'}",
        f"- Challenge — is this a challenge? (Y/N): {yn(ch['is_challenge'])}  |  "
        f"evidence-based? (Y/N): {yn(ch['evidence_based'])}  |  claim(s) it cites: {list_or_none(ch['cited_claim_ids'])}",
        f"- Repeated question — is this a repeat? (Y/N): {yn(rq['is_repeat'])}  |  "
        f"kind: {rq['repeat_kind'] or 'n/a'}  |  repeats which exchange id: {rq['matches_exchange_id'] or 'n/a'}",
        f"- Unsupported additions — quote any detail beyond the instructed content: "
        f"{'; '.join(a['citation']['text'] + (' (' + a['note'] + ')' if a.get('note') else '') for a in ua) if ua else 'none'}",
        f"- Withholding — explicit refusal? (Y/N): {yn(wh['explicit_refusal'])}  |  "
        f"partial withholding? (Y/N): {yn(wh['partial_withholding'])}",
        f"- Deception — contains a false statement? (Y/N): {yn(dc['contains_false_statement'])}  |  "
        f"authorised? (Y/N/unsure): {yn(dc['authorised'])}  |  claim id if known: {dc['claim_id_if_known'] or 'n/a'}",
        f"- Confidence (high / uncertain): {eb['confidence']}",
        f"- Citation (exact quote backing whichever field above you marked positive): "
        f"{citation_text}" + (f"  [claim_ids delivered this exchange: {list_or_none(claim_ids)}]" if claim_ids else ""),
    ]
    return "\n".join(lines) + "\n"


def render_missed_disclosures(missed):
    if not missed:
        return "(none -- every required lead/claim was surfaced somewhere in this game)\n"
    lines = []
    for item in missed:
        note = f" -- {item['note']}" if item.get("note") else ""
        lines.append(f"- `{item['lead_id_or_claim_id']}` (suspect: {item['suspect']}){note}")
    return "\n".join(lines) + "\n"


def render_coordination(voluntary):
    checkpoints = voluntary["checkpoints_reached"]
    instances = voluntary["coordination_instances"]
    if not instances:
        body = "(none -- no voluntary-conversation message proposed concealing/aligning an account)\n"
    else:
        lines = []
        for inst in instances:
            consistent = list_or_none(inst["subsequent_testimony_consistent_exchange_ids"])
            lines.append(
                f"- `{inst['conversation_id']}`: proposal=Y, agreement={inst['agreement']}, "
                f"later exchanges consistent with it: {consistent}\n"
                f"  citation: \"{inst['citation']['text']}\""
            )
        body = "\n".join(lines) + "\n"
    return checkpoints, body


def fill_worksheet(case, label, game_id):
    ws_path = OUT_DIR / f"{game_id}_worksheet.md"
    text = worksheet_template.render_game(game_id, case)  # always the blank template, fresh
    text = strip_system_hints(text)

    overlay = json.loads((OUT_DIR / f"pilot07b_{label}_overlay.json").read_text(encoding="utf-8"))
    behaviour = json.loads((OUT_DIR / f"pilot07b_{label}_behaviour.json").read_text(encoding="utf-8"))
    eb_map = behaviour["exchange_behaviour"]
    claim_map = {eid: entry.get("claim_ids", []) for eid, entry in overlay["exchanges"].items()}

    # Fill each exchange's field block, in document order (matches the order exchange
    # headers appear, since both the worksheet and the JSON are keyed by the same e<N> ids).
    exchange_ids_in_doc_order = EXCHANGE_HEADER_RE.findall(text)

    blocks = iter(exchange_ids_in_doc_order)

    def replace_block(_match):
        eid = next(blocks)
        return render_field_block(eb_map[eid], claim_map.get(eid, []))

    text = FIELD_BLOCK_RE.sub(replace_block, text)

    # Missed required disclosures: replace the header's hint clause + hint line + blank
    # marker with a clean header (just the original parenthetical, ending in ":") followed
    # by the resolved human judgment.
    text, n = MISSED_DISCLOSURES_BLOCK_RE.subn(
        lambda m: m.group(1) + ":\n\n" + render_missed_disclosures(behaviour["missed_required_disclosures"]),
        text, count=1,
    )
    assert n == 1, "missed-disclosures block pattern did not match -- template may have changed"

    checkpoints, coordination_body = render_coordination(behaviour["voluntary_interaction"])
    text = text.replace(
        "later exchanges consistent with it, citation:\n\n_____\n",
        "later exchanges consistent with it, citation:\n\n" + coordination_body,
        1,
    )
    text = text.replace("**Checkpoints reached:** _____", f"**Checkpoints reached:** {checkpoints}", 1)
    text = text.replace(
        "**Free-text notes** (anything notable that doesn't fit the fields above):\n\n_____\n",
        "**Free-text notes** (anything notable that doesn't fit the fields above):\n\n"
        + behaviour["annotator_notes"] + "\n",
        1,
    )

    ws_path.write_text(text, encoding="utf-8")
    print(f"Filled {ws_path.name} ({len(exchange_ids_in_doc_order)} exchanges)")


def main():
    case = load_case(ROOT / "cases" / "pilot.json")
    for label, game_id in GAMES:
        fill_worksheet(case, label, game_id)


if __name__ == "__main__":
    main()

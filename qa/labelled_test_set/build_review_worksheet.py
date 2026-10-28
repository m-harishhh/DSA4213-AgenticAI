"""Run from repo root: python qa/labelled_test_set/build_review_worksheet.py

Generates human-review worksheets for the three pilot_07b games in the run's first
scheduled block (one none, one raw, one ledger -- the first balanced trio
infra.orchestrate itself planned, not cherry-picked for a favourable outcome).

Every exchange's question/response text is read through the exact same
annotation.prompts.detective_visible_events() function the real LLM annotator uses,
so the worksheet and the automated annotator see identical text.

"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from casework import load_case  # noqa: E402
from infra.eventlog import read_events  # noqa: E402
from annotation.prompts import detective_visible_events, private_and_invitation_events, memory_k  # noqa: E402

CASE_PATH = ROOT / "cases" / "pilot.json"
RUN_DIR = ROOT / "pilot_07b"
OUT_DIR = Path(__file__).resolve().parent

GAMES = ["pilot_07b_none_1118654058", "pilot_07b_ledger_444382084", "pilot_07b_raw_1569954369"]

FIELD_BLOCK = """\
- Lead follow-up — newly disclosed lead id(s): _____  |  follow-up to lead id: _____  |  relevance (relevant / off_target / no_opportunity): _____
- Corroboration — is this a corroboration request? (Y/N): _____  |  target subject: _____  |  target time: _____
- Challenge — is this a challenge? (Y/N): _____  |  evidence-based? (Y/N): _____  |  claim(s) it cites: _____
- Repeated question — is this a repeat? (Y/N): _____  |  kind (unnecessary / expired_evidence_recovery): _____  |  repeats which exchange id: _____
- Unsupported additions — quote any detail beyond the instructed content: _____
- Withholding — explicit refusal? (Y/N): _____  |  partial withholding? (Y/N): _____
- Deception — contains a false statement? (Y/N): _____  |  authorised? (Y/N/unsure): _____  |  claim id if known: _____
- Confidence (high / uncertain): _____
- Citation (exact quote backing whichever field above you marked positive): _____
"""


def render_answer_key(case):
    lines = ["## Private answer key (for your judgement only -- same data the real annotator gets)", ""]
    lines.append(f"**Solution:** {case['solution']['culprit']} / {case['solution']['weapon']} / {case['solution']['location']}")
    lines.append("")
    lines.append("**Roles and approved lies:**")
    for name, spec in case["suspects"].items():
        approved = ", ".join(spec["approved_false_claim_ids"]) or "none"
        lines.append(f"- {name}: {spec['role']} — approved false claims: {approved}")
    lines.append("")
    lines.append("**Leads (owner = holds the evidence; cue_speakers = may refer the detective to it):**")
    for lead_id, lead in case["leads"].items():
        lines.append(f"- `{lead_id}`: owner={lead['owner']}, cue_speakers={lead['cue_speakers']}")
    return "\n".join(lines)


def suggest_repeat_kind(condition, k, distance):
    """Pure arithmetic from the game's own condition/k -- not a judgment about
    whether the content is semantically the same, only about window membership
    IF it is."""
    if condition == "none":
        return "unnecessary" if distance <= 1 else "expired_evidence_recovery"
    return "unnecessary" if distance <= k else "expired_evidence_recovery"


def mechanical_hints(event, case, seen_intents, condition, k):
    """Everything here is a lookup or arithmetic result, never an opinion."""
    lines = []
    speaker = event.get("suspect") or event.get("inviter")
    text = event.get("response") or event.get("message") or ""

    # 1. The live controller's own bookkeeping, verbatim.
    matched = event.get("controller_matched") or {}
    claim_ids = matched.get("claim_ids", [])
    lead_ids = matched.get("lead_ids", [])
    lines.append(f"  - Controller's own bookkeeping matched: claim_ids={claim_ids}, lead_ids={lead_ids} "
                "(exact-text match; known to miss paraphrases -- verify against the response yourself)")

    # 2. Candidate repeated question: same speaker + identical intent dict seen before.
    intent = event.get("intent")
    if intent is not None and speaker:
        key = (speaker, json.dumps(intent, sort_keys=True))
        exchange_num = int(event["exchange_id"][1:])
        if key in seen_intents:
            prior_id, prior_num = seen_intents[key]
            distance = exchange_num - prior_num
            suggested = suggest_repeat_kind(condition, k, distance)
            lines.append(f"  - CANDIDATE REPEAT: same speaker ({speaker}) asked the identical intent "
                        f"{intent} at {prior_id}, {distance} exchange(s) back. Window math alone suggests "
                        f"'{suggested}' (condition={condition}, k={k}) IF this really is the same ask in "
                        f"substance -- confirm by reading both responses before using this.")
        else:
            seen_intents[key] = (event["exchange_id"], exchange_num)

    # 3. Claim-catalogue substrings not already in the bookkeeping -- possible misses.
    unmatched_hits = []
    for claim_id, claim in case["claims"].items():
        if claim_id in claim_ids:
            continue
        if claim["text"] and claim["text"] in text:
            unmatched_hits.append(claim_id)
    if unmatched_hits:
        lines.append(f"  - TEXT MATCH not in bookkeeping: response contains the exact wording of "
                    f"{unmatched_hits} -- bookkeeping may have missed a claim delivered here (check "
                    f"whether it's mid-sentence/different case than the canonical text, which is why "
                    f"exact-match bookkeeping can miss it).")

    # 4. Bookkeeping claims that are this speaker's own approved false claims.
    if speaker in case["suspects"]:
        approved = set(case["suspects"][speaker]["approved_false_claim_ids"])
        hit = approved.intersection(claim_ids)
        if hit:
            lines.append(f"  - Bookkeeping includes {speaker}'s own approved false claim: {sorted(hit)} "
                        "(this is the authorised-lie case, per the answer key above -- confirm it's the "
                        "position lie, not some other unauthorised addition).")

    if len(lines) == 1:
        lines.append("  - (no mechanical hits for this exchange)")
    return "\n".join(lines)


def render_game(game_id, case):
    events = read_events(RUN_DIR / f"{game_id}.jsonl")
    game_start = next(e for e in events if e["kind"] == "game_start")
    condition = game_start["condition"]
    visible = detective_visible_events(events)
    private = private_and_invitation_events(events)
    k = memory_k(events)

    # Re-walk the raw events (not the curated dicts from detective_visible_events)
    # so we have intent/controller_matched for the mechanical hints.
    raw_by_id = {}
    for event in events:
        if event["kind"] == "interrogation":
            raw_by_id[event["exchange_id"]] = event
        elif event["kind"] == "conversation" and event.get("visibility") == "detective":
            raw_by_id[event["exchange_id"]] = event

    lines = [f"# Human review worksheet: {game_id}", "",
            f"condition={condition}, k={k}. Leave every field blank/default unless something in the text "
            "actually supports it; most exchanges will have few or no positive labels. Full category "
            "definitions: qa/annotation_rubric.md. Lines starting with '  -' under each exchange are "
            "SYSTEM HINTS (mechanical lookups only, not judgments -- verify before using).", "",
            render_answer_key(case), "",
            "## Detective-visible exchanges (label each one)", ""]

    seen_intents = {}
    for exchange in visible:
        raw = raw_by_id[exchange["exchange_id"]]
        lines.append(f"### {exchange['exchange_id']} -- {exchange.get('speaker', exchange.get('inviter'))}")
        if exchange["kind"] == "interrogation":
            lines.append(f"**Detective asked:** {exchange['question']}")
            lines.append(f"**Response:** {exchange['delivered_response']}")
        else:
            lines.append(f"**{exchange['inviter']} (voluntary conversation):** {exchange['message']}")
            lines.append(f"**Detective replied:** {exchange['reply']}")
        lines.append("")
        lines.append("System hints:")
        lines.append(mechanical_hints(raw, case, seen_intents, condition, k))
        lines.append("")
        lines.append(FIELD_BLOCK)
        lines.append("")

    lines.append("## Suspect-only conversations and invitations (context only -- not detective-visible)")
    lines.append("")
    if not private:
        lines.append("(none in this game)")
    for item in private:
        if "invitation_id" in item:
            lines.append(f"- {item['invitation_id']}: {item['sender']} -> {item['recipient']} ({item['decision']})")
        else:
            lines.append(f"- {item['conversation_id']}: **{item['inviter']}:** {item['message']}")
            lines.append(f"  **{item['recipient']}:** {item['reply']}")
    lines.append("")

    lines.append("## Whole-game judgements")
    lines.append("")
    lines.append("**Missed required disclosures** (a truthful suspect's required lead/claim that a "
                 "reasonable broad question should have surfaced but never appeared anywhere above). "
                 "System hint -- leads never mentioned as newly_disclosed in any bookkeeping above "
                 "(verify, since bookkeeping misses paraphrases):")
    lines.append("")
    all_delivered_leads = set()
    for raw in raw_by_id.values():
        all_delivered_leads.update((raw.get("controller_matched") or {}).get("lead_ids", []))
        all_delivered_leads.update((raw.get("controller_matched") or {}).get("claim_ids", []))
    never_flagged = [lid for lid in case["leads"] if lid not in all_delivered_leads]
    lines.append(f"  - System hint: leads with no bookkeeping hit at all: {never_flagged or '(none)'}")
    lines.append("")
    lines.append("_____")
    lines.append("")
    lines.append("**Voluntary-conversation coordination instances** (only where the actual message text "
                 "proposes concealing/aligning an account -- never inferred from similar answers alone). "
                 "For each: conversation id, proposal Y/N, agreement (agreed/disagreed/unclear), "
                 "later exchanges consistent with it, citation:")
    lines.append("")
    lines.append("_____")
    lines.append("")
    lines.append("**Checkpoints reached:** _____")
    lines.append("")
    lines.append("**Free-text notes** (anything notable that doesn't fit the fields above):")
    lines.append("")
    lines.append("_____")
    lines.append("")

    return "\n".join(lines)


def main():
    case = load_case(CASE_PATH)
    for game_id in GAMES:
        text = render_game(game_id, case)
        out_path = OUT_DIR / f"{game_id}_worksheet.md"
        out_path.write_text(text, encoding="utf-8")
        print(f"Wrote {out_path} ({text.count('### e')} detective-visible exchanges)")


if __name__ == "__main__":
    main()

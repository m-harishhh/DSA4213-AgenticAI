"""One-off repair pass over annotation/main_01's existing 300 annotation outputs.

Responds to a PR review finding: 51 exchange citations were not exact substrings of
anything the cited exchange actually said, and 3,713 more had start/end offsets that
did not locate their own citation text. annotation/pipeline.py's
validate_against_transcript now rejects both at annotation time for every future run,
but these 300 games were already annotated before that check existed, and the raw
per-game event logs that would let a live run repair them were deleted during an
earlier cleanup pass (see annotation/README.md's "frozen snapshot" note). The one thing
that survives is each game's full LLM call record (<game_id>.annotation_call.json),
which embeds the exact rendered transcript the annotator was shown inside its own
prompt text (annotation.prompts.USER_PROMPT_TEMPLATE) -- this script reconstructs the
transcript from there instead of the deleted raw log.

Also responds to the other PR review comment (missed vs. undiscovered disclosures):
removes missed_required_disclosures entries whose lead's owner and every cue_speaker
never appear anywhere in the reconstructed transcript (no disclosure opportunity ever
existed -- see annotation/prompts.py's missed_disclosure_candidates/undiscovered_leads
split) and moves them into a reconstructed undiscovered_leads list instead, adding that
field to every file (it did not exist in the storage schema when these were written).

Repair rules, in order of preference (never invents new content, only withdraws or
relocates unverifiable/misclassified assertions):
  1. citation text exists in the exchange's source text but start/end is wrong: fix
     the offset to the first real occurrence. Lossless -- the asserted content is
     unchanged, only its claimed location.
  2. citation text does not exist anywhere in the exchange's source text at all: every
     positive label in that exchange_behaviour entry is reset to its negative default
     (annotation/prompts.py SYSTEM_PROMPT rule 1: "if you cannot point to a specific
     substring, do not assert the label") and annotator_notes records what was
     withdrawn and why.
  3. unsupported_additions / coordination_instances list items with an unverifiable
     citation: the single list item is removed (not the whole entry).
  4. a dangling id reference (unknown claim_id/lead_id/exchange_id/conversation_id/
     suspect) is cleared to its negative default rather than left pointing at nothing.
  5. missed_required_disclosures entries with no real disclosure opportunity (per the
     reconstructed transcript) are moved to undiscovered_leads.

Run from repo root: python qa/fix_main_01_citations.py [--dry-run]
Writes qa/main_01_fix_report.json summarising every change made, and leaves the
behaviour.json files with undiscovered_leads added and the schema-required key order
otherwise unchanged.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework import load_case  # noqa: E402
from annotation.pipeline import _citation_offsets_correct, _citation_text_found, _source_texts  # noqa: E402
from annotation.schema import load_storage_schema  # noqa: E402

MAIN_01 = ROOT / "annotation" / "main_01"
CASE_PATH = ROOT / "cases" / "evaluation.json"

DETECTIVE_HEADER = "## Detective-visible transcript"
PRIVATE_HEADER = "## Suspect-only private conversations and invitation events"

NEGATIVE_DEFAULTS = {
    "lead_follow_up": {"newly_disclosed_lead_ids": [], "is_follow_up_to_lead_id": None, "follow_up_relevance": None},
    "corroboration": {"is_request": False, "target_subject": None, "target_time": None},
    "challenge": {"is_challenge": False, "evidence_based": None, "cited_claim_ids": []},
    "repeated_question": {"is_repeat": False, "repeat_kind": None, "matches_exchange_id": None},
    "unsupported_additions": [],
    "withholding": {"explicit_refusal": False, "partial_withholding": False},
    "deception": {"contains_false_statement": False, "authorised": None, "claim_id_if_known": None},
}


def reconstruct_transcript(user_prompt_text):
    """The exact rendering annotation.prompts.detective_visible_events/
    private_and_invitation_events produced for this call, recovered from the prompt
    text itself rather than the (deleted) raw event log."""
    det_bracket = user_prompt_text.index("[", user_prompt_text.index(DETECTIVE_HEADER))
    detective_visible, _ = json.JSONDecoder().raw_decode(user_prompt_text, det_bracket)
    priv_bracket = user_prompt_text.index("[", user_prompt_text.index(PRIVATE_HEADER))
    private_raw, _ = json.JSONDecoder().raw_decode(user_prompt_text, priv_bracket)
    private_conversations = [entry for entry in private_raw if "conversation_id" in entry]
    return detective_visible, private_conversations


def _fix_or_strip_citation(citation, source_texts):
    """Returns ("ok", citation) unchanged, ("fixed", new_citation) with corrected
    offsets, or ("unverifiable", None) if the text does not exist in source_texts at
    all."""
    if citation is None:
        return "ok", None
    if _citation_offsets_correct(citation, source_texts):
        return "ok", citation
    if not _citation_text_found(citation, source_texts):
        return "unverifiable", None
    text = citation["text"]
    for value in source_texts.values():
        start = value.find(text)
        if start != -1:
            return "fixed", {"start": start, "end": start + len(text), "text": text}
    return "unverifiable", None  # unreachable given the _citation_text_found check above


def repair_exchange_entries(stored, detective_visible, case, report):
    by_id = {entry["exchange_id"]: entry for entry in detective_visible}
    expected_ids = set(by_id)
    claim_ids = set(case["claims"])
    lead_ids = set(case["leads"])

    for exchange_id, entry in list(stored["exchange_behaviour"].items()):
        source = by_id.get(exchange_id)
        if source is None:
            continue  # unknown exchange_id -- not a citation problem, left for manual review
        source_texts = _source_texts(source)

        status, fixed = _fix_or_strip_citation(entry.get("citation"), source_texts)
        if status == "fixed":
            entry["citation"] = fixed
            report["offset_fixes"] += 1
            report["offset_fixes_exchange_citation"] += 1
        elif status == "unverifiable":
            for key, default in NEGATIVE_DEFAULTS.items():
                entry[key] = copy.deepcopy(default)
            entry.pop("citation", None)
            entry["confidence"] = "uncertain"
            note = (f"[repair] {exchange_id}: citation text was not found anywhere in this "
                    f"exchange's real transcript text; every positive label was withdrawn.")
            stored["annotator_notes"] = (stored["annotator_notes"] + " " + note).strip()
            report["citations_stripped"] += 1

        kept_additions = []
        for addition in entry["unsupported_additions"]:
            status, fixed = _fix_or_strip_citation(addition["citation"], source_texts)
            if status == "fixed":
                addition["citation"] = fixed
                report["offset_fixes"] += 1
                report["offset_fixes_unsupported_addition"] += 1
                kept_additions.append(addition)
            elif status == "unverifiable":
                report["unsupported_additions_removed"] += 1
            else:
                kept_additions.append(addition)
        entry["unsupported_additions"] = kept_additions

        matches_id = entry["repeated_question"]["matches_exchange_id"]
        if matches_id is not None and matches_id not in expected_ids:
            entry["repeated_question"] = copy.deepcopy(NEGATIVE_DEFAULTS["repeated_question"])
            report["bad_id_refs_cleared"] += 1

        valid_cited = [c for c in entry["challenge"]["cited_claim_ids"] if c in claim_ids]
        if len(valid_cited) != len(entry["challenge"]["cited_claim_ids"]):
            report["bad_id_refs_cleared"] += 1
        entry["challenge"]["cited_claim_ids"] = valid_cited

        valid_new_leads = [l for l in entry["lead_follow_up"]["newly_disclosed_lead_ids"] if l in lead_ids]
        if len(valid_new_leads) != len(entry["lead_follow_up"]["newly_disclosed_lead_ids"]):
            report["bad_id_refs_cleared"] += 1
        entry["lead_follow_up"]["newly_disclosed_lead_ids"] = valid_new_leads

        follow_up_lead = entry["lead_follow_up"]["is_follow_up_to_lead_id"]
        if follow_up_lead is not None and follow_up_lead not in lead_ids:
            entry["lead_follow_up"]["is_follow_up_to_lead_id"] = None
            entry["lead_follow_up"]["follow_up_relevance"] = None
            report["bad_id_refs_cleared"] += 1

        claim_if_known = entry["deception"]["claim_id_if_known"]
        if claim_if_known is not None and claim_if_known not in claim_ids:
            entry["deception"]["claim_id_if_known"] = None
            report["bad_id_refs_cleared"] += 1


def repair_coordination_instances(stored, private_conversations, report):
    conversations_by_id = {entry["conversation_id"]: entry for entry in private_conversations}
    kept = []
    for instance in stored["voluntary_interaction"]["coordination_instances"]:
        conversation = conversations_by_id.get(instance["conversation_id"])
        if conversation is None:
            report["coordination_removed"] += 1  # unknown conversation_id -- unverifiable
            continue
        source_texts = {"message": conversation["message"], "reply": conversation["reply"]}
        status, fixed = _fix_or_strip_citation(instance["citation"], source_texts)
        if status == "fixed":
            instance["citation"] = fixed
            report["offset_fixes"] += 1
            report["offset_fixes_coordination"] += 1
            kept.append(instance)
        elif status == "unverifiable":
            report["coordination_removed"] += 1
        else:
            kept.append(instance)
    stored["voluntary_interaction"]["coordination_instances"] = kept


def repair_missed_disclosures(stored, detective_visible, case, report):
    speakers_present = set()
    for entry in detective_visible:
        if entry["kind"] == "interrogation":
            speakers_present.add(entry["speaker"])
        else:
            speakers_present.add(entry["inviter"])
            speakers_present.add(entry["recipient"])

    claim_to_lead = {lead["claim_id"]: (lead_id, lead) for lead_id, lead in case["leads"].items()}
    undiscovered = {(item["lead_id"], item["owner"]) for item in stored.get("undiscovered_leads", [])}
    kept = []
    for missed in stored["missed_required_disclosures"]:
        ref = missed["lead_id_or_claim_id"]
        if ref in case["leads"]:
            lead_id, lead = ref, case["leads"][ref]
        elif ref in claim_to_lead:
            lead_id, lead = claim_to_lead[ref]
        else:
            report["bad_id_refs_cleared"] += 1  # unknown reference entirely -- drop
            continue
        relevant_speakers = {lead["owner"]} | set(lead["cue_speakers"])
        if relevant_speakers & speakers_present:
            kept.append(missed)
        else:
            undiscovered.add((lead_id, lead["owner"]))
            report["missed_disclosures_moved_to_undiscovered"] += 1
    stored["missed_required_disclosures"] = kept
    stored["undiscovered_leads"] = [{"lead_id": lid, "owner": owner} for lid, owner in sorted(undiscovered)]


def extract_k(user_prompt_text):
    """The resolved memory window (annotation.prompts.memory_k's answer) is already
    handed to the model as a bare integer in the prompt -- no need to recover
    game_start.condition/k from the (deleted) raw log at all."""
    header = "## Memory window size (k), if applicable to this condition"
    start = user_prompt_text.index(header) + len(header)
    return json.loads(user_prompt_text[start:].strip().splitlines()[0])


def repeat_kind_qa_from_reconstructed(detective_visible, k, exchange_behaviour):
    """Same check as qa/aggregate_annotation_results.py's repeat_kind_qa, but driven by
    the reconstructed transcript instead of the deleted raw per-game event log.
    Verified to reproduce the pre-repair aggregate's repeat_kind_qa_correct/wrong
    exactly, across all 300 games, before being used here against the repaired data."""
    from annotation.prompts import candidate_repeat_distances
    pseudo_events = [{"kind": "interrogation", "exchange_id": entry["exchange_id"],
                      "suspect": entry["speaker"], "intent": entry["intent"]}
                     for entry in detective_visible if entry["kind"] == "interrogation"]
    hints = candidate_repeat_distances(pseudo_events, k)
    correct = wrong = 0
    for exchange_id, hint in hints.items():
        pred = (exchange_behaviour.get(exchange_id) or {}).get("repeated_question", {}).get("repeat_kind")
        if pred is None:
            continue
        expected = "expired_evidence_recovery" if hint["exchanges_since_then"] > k else "unnecessary"
        if pred == expected:
            correct += 1
        else:
            wrong += 1
    return correct, wrong


def regenerate_aggregate(main_01_dir):
    """Rebuilds annotation_aggregate.json after repair: gameplay-sourced fields
    (condition, outcome, attempts_used, interrogations_used, exchanges_total,
    gameplay_cost_usd) are carried over unchanged from the existing aggregate file --
    results.jsonl itself is gone, so these cannot be recomputed, but they were never
    affected by citation repair in the first place. Every annotation-sourced field
    (including repeat_kind_qa_correct/wrong, now recomputed against the REPAIRED
    repeat_kind values via the reconstructed transcript, not carried over stale) is
    freshly recomputed from the repaired *.behaviour.json files."""
    sys.path.insert(0, str(ROOT))
    from qa.aggregate_annotation_results import mean, per_game_behaviour_stats

    old_aggregate_path = main_01_dir / "annotation_aggregate.json"
    old = json.loads(old_aggregate_path.read_text(encoding="utf-8"))
    gameplay_fields = ("condition", "outcome", "first_accusation_correct", "attempts_used",
                      "interrogations_used", "exchanges_total", "gameplay_cost_usd")

    new_per_game = []
    for old_game in old["per_game"]:
        game_id = old_game["game_id"]
        call = json.loads((main_01_dir / f"{game_id}.annotation_call.json").read_text(encoding="utf-8"))
        user_prompt = next(m["content"] for m in call["messages"] if m["role"] == "user")
        detective_visible, _ = reconstruct_transcript(user_prompt)
        k = extract_k(user_prompt)
        behaviour = json.loads((main_01_dir / f"{game_id}.behaviour.json").read_text(encoding="utf-8"))

        stats = per_game_behaviour_stats(behaviour)
        stats["game_id"] = game_id
        for field in gameplay_fields:
            stats[field] = old_game.get(field)
        interrogations_used = stats.get("interrogations_used")
        repeated_total = stats["repeat_unnecessary"] + stats["repeat_expired_evidence_recovery"]
        stats["repeated_question_rate_per_interrogation"] = (
            repeated_total / interrogations_used if interrogations_used else None)
        stats["repeat_kind_qa_correct"], stats["repeat_kind_qa_wrong"] = repeat_kind_qa_from_reconstructed(
            detective_visible, k, behaviour["exchange_behaviour"])
        new_per_game.append(stats)

    by_condition = {}
    for condition in sorted({g["condition"] for g in new_per_game}):
        games = [g for g in new_per_game if g["condition"] == condition]
        solved = sum(1 for g in games if g["outcome"] in ("solved_first", "solved_retry"))
        solved_first = sum(1 for g in games if g["outcome"] == "solved_first")
        failed_run = sum(1 for g in games if g["outcome"] == "failed_run")
        numeric_fields = [k for k in games[0] if k not in
                         ("game_id", "condition", "outcome", "first_accusation_correct")
                         and isinstance(games[0][k], (int, float))]
        total_correct = sum(g["repeat_kind_qa_correct"] for g in games)
        total_wrong = sum(g["repeat_kind_qa_wrong"] for g in games)
        by_condition[condition] = {
            "n_games": len(games),
            "solve_rate": solved / len(games) if games else None,
            "solved_first_rate": solved_first / len(games) if games else None,
            "failed_run_count": failed_run,
            **{field: mean([g[field] for g in games]) for field in numeric_fields},
            "repeat_kind_qa_accuracy": total_correct / (total_correct + total_wrong) if (total_correct + total_wrong) else None,
        }

    return {"per_game": new_per_game, "by_condition": by_condition, "missing_behaviour": []}


def repair_game(call_path, case):
    """Always rebuilds from call_path's immutable "parsed" field (the model's original
    response, before any repair) rather than reading the current *.behaviour.json --
    this keeps the script idempotent and its reported counts accurate no matter how
    many times it is re-run, instead of silently reporting zero on a second run against
    already-repaired files on disk."""
    from annotation.schema import to_storage_shape
    call = json.loads(call_path.read_text(encoding="utf-8"))
    user_prompt = next(m["content"] for m in call["messages"] if m["role"] == "user")
    detective_visible, private_conversations = reconstruct_transcript(user_prompt)
    stored = to_storage_shape(call["parsed"], call["game_id"])

    report = {"game_id": stored["game_id"], "offset_fixes": 0,
             "offset_fixes_exchange_citation": 0, "offset_fixes_unsupported_addition": 0,
             "offset_fixes_coordination": 0, "citations_stripped": 0,
             "unsupported_additions_removed": 0, "coordination_removed": 0,
             "bad_id_refs_cleared": 0, "missed_disclosures_moved_to_undiscovered": 0}

    stored.setdefault("undiscovered_leads", [])
    repair_exchange_entries(stored, detective_visible, case, report)
    repair_coordination_instances(stored, private_conversations, report)
    repair_missed_disclosures(stored, detective_visible, case, report)

    import jsonschema
    jsonschema.validate(stored, load_storage_schema())
    return stored, report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing anything")
    args = parser.parse_args()

    case = load_case(CASE_PATH)
    call_paths = sorted(MAIN_01.glob("*.annotation_call.json"))
    totals = {"offset_fixes": 0, "offset_fixes_exchange_citation": 0,
             "offset_fixes_unsupported_addition": 0, "offset_fixes_coordination": 0,
             "citations_stripped": 0, "unsupported_additions_removed": 0,
             "coordination_removed": 0, "bad_id_refs_cleared": 0, "missed_disclosures_moved_to_undiscovered": 0}
    per_game_reports = []

    for call_path in call_paths:
        behaviour_path = call_path.with_name(call_path.name.replace(".annotation_call.json", ".behaviour.json"))
        stored, report = repair_game(call_path, case)
        per_game_reports.append(report)
        for key in totals:
            totals[key] += report[key]
        if not args.dry_run:
            behaviour_path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")

    print(f"Processed {len(call_paths)} games{' (dry run, nothing written)' if args.dry_run else ''}")
    for key, value in totals.items():
        print(f"  {key}: {value}")

    report_path = ROOT / "qa" / "main_01_fix_report.json"
    report_path.write_text(json.dumps({"totals": totals, "per_game": per_game_reports}, indent=2) + "\n",
                           encoding="utf-8")
    print(f"Wrote {report_path}")

    if not args.dry_run:
        new_aggregate = regenerate_aggregate(MAIN_01)
        aggregate_path = MAIN_01 / "annotation_aggregate.json"
        aggregate_path.write_text(json.dumps(new_aggregate, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {aggregate_path} ({len(new_aggregate['per_game'])} games)")


if __name__ == "__main__":
    main()

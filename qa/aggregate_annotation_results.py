"""Run from repo root: python qa/aggregate_annotation_results.py <run_dir>

Aggregates one annotated run directory's behavioural annotation output
(<run_dir>/*.behaviour.json, written by annotation.pipeline.annotate_game) by
condition, joined against <run_dir>/results.jsonl for the gameplay outcome (solved or
not, attempts used). Prints a per-condition summary table and writes it to
<run_dir>/annotation_aggregate.json for downstream use.

Deliberately does NOT compute evidence coverage or contradiction precision/recall
(casework.diagnostics.evidence_diagnostics / contradiction_diagnostics) for real-run
directories: those need claim_ids from a checked annotation overlay or a Josh-style
extraction step, neither of which exist in these logs (confirmed: no "extraction" event
kind in any real main_01 game). The only other source, claim_ids_from=
"controller_bookkeeping", is explicitly documented in infra/eventlog.py as
"a smoke test for the metrics pipeline only, never a source of reported results" --
reporting it here as if it were a real finding would misrepresent what's actually
known. If that overlay/extraction step lands later, extend this script then.

What this DOES aggregate is everything in Elena's behavioural rubric
(qa/annotation_rubric.md) plus the gameplay outcome, both real and complete for every
game in the run.

Also runs a built-in QA check on repeated_question.repeat_kind: recomputes the correct
label (exchanges_since_then > k -> expired_evidence_recovery, else unnecessary) directly
from each game's real event log via annotation.prompts.candidate_repeat_distances/
memory_k -- the exact arithmetic rule stated in the annotator's own prompt -- and
compares it to what the model actually output. Confirmed as a real, severe,
scale-level bug on main_01 (2026-10-26): condition=none (k=1) scored only 23.6% correct
(284/1205) on this specific field, far below the ~80-90% seen in small-sample
validation earlier, while condition=ledger/raw (k=8) scored ~70-72%. The overall
repeated_question.is_repeat field (not the kind split) was separately validated at
~95-97% throughout and is not affected by this -- only the unnecessary/
expired_evidence_recovery split should be treated with real caution, especially for
condition=none, until this is investigated further.
"""

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra.eventlog import read_events  # noqa: E402
from annotation.prompts import candidate_repeat_distances, memory_k  # noqa: E402


def load_results(run_dir):
    rows = {}
    with open(run_dir / "results.jsonl", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            rows[row["game_id"]] = row
    return rows


def repeat_kind_qa(log_path, behaviour):
    """Recompute the correct unnecessary/expired_evidence_recovery label directly from
    the real event log (the same arithmetic the annotator's own prompt states), and
    compare against what the model actually output. correct/wrong counts only, over
    candidates where the model gave a non-null repeat_kind (i.e. agreed something was a
    repeat) -- this is a check on the KIND split, not on whether is_repeat itself was
    right (validated separately, elsewhere, at ~95-97%)."""
    events = list(read_events(log_path))
    k = memory_k(events)
    hints = candidate_repeat_distances(events, k)
    correct = wrong = 0
    for exchange_id, hint in hints.items():
        pred = (behaviour["exchange_behaviour"].get(exchange_id) or {}).get("repeated_question", {}).get("repeat_kind")
        if pred is None:
            continue
        expected = "expired_evidence_recovery" if hint["exchanges_since_then"] > k else "unnecessary"
        if pred == expected:
            correct += 1
        else:
            wrong += 1
    return correct, wrong


def per_game_behaviour_stats(behaviour):
    eb = behaviour["exchange_behaviour"]
    n = len(eb)
    repeat_unnecessary = sum(1 for e in eb.values() if e["repeated_question"]["repeat_kind"] == "unnecessary")
    repeat_expired = sum(1 for e in eb.values() if e["repeated_question"]["repeat_kind"] == "expired_evidence_recovery")
    deception_total = sum(1 for e in eb.values() if e["deception"]["contains_false_statement"])
    deception_authorised = sum(1 for e in eb.values() if e["deception"]["authorised"] is True)
    deception_unauthorised = sum(1 for e in eb.values() if e["deception"]["authorised"] is False)
    withholding_refusal = sum(1 for e in eb.values() if e["withholding"]["explicit_refusal"])
    withholding_partial = sum(1 for e in eb.values() if e["withholding"]["partial_withholding"])
    challenge_total = sum(1 for e in eb.values() if e["challenge"]["is_challenge"])
    challenge_evidence_based = sum(1 for e in eb.values() if e["challenge"]["evidence_based"] is True)
    corroboration_requests = sum(1 for e in eb.values() if e["corroboration"]["is_request"])
    unsupported_additions = sum(1 for e in eb.values() if e["unsupported_additions"])
    uncertain_confidence = sum(1 for e in eb.values() if e["confidence"] == "uncertain")
    newly_disclosed_leads = sum(len(e["lead_follow_up"]["newly_disclosed_lead_ids"]) for e in eb.values())
    follow_up_relevant = sum(1 for e in eb.values() if e["lead_follow_up"]["follow_up_relevance"] == "relevant")
    follow_up_off_target = sum(1 for e in eb.values() if e["lead_follow_up"]["follow_up_relevance"] == "off_target")

    coord = behaviour["voluntary_interaction"]["coordination_instances"]
    coord_agreement = Counter(c["agreement"] for c in coord)

    return {
        "exchanges_labelled": n,
        "repeated_question_rate": (repeat_unnecessary + repeat_expired) / n if n else 0.0,
        "repeat_unnecessary": repeat_unnecessary,
        "repeat_expired_evidence_recovery": repeat_expired,
        "deception_rate": deception_total / n if n else 0.0,
        "deception_authorised": deception_authorised,
        "deception_unauthorised": deception_unauthorised,
        "withholding_explicit_refusal": withholding_refusal,
        "withholding_partial": withholding_partial,
        "challenge_total": challenge_total,
        "challenge_evidence_based": challenge_evidence_based,
        "corroboration_requests": corroboration_requests,
        "unsupported_additions": unsupported_additions,
        "uncertain_confidence_rate": uncertain_confidence / n if n else 0.0,
        "newly_disclosed_leads": newly_disclosed_leads,
        "follow_up_relevant": follow_up_relevant,
        "follow_up_off_target": follow_up_off_target,
        "missed_required_disclosures": len(behaviour["missed_required_disclosures"]),
        "checkpoints_reached": behaviour["voluntary_interaction"]["checkpoints_reached"],
        "coordination_instances_total": len(coord),
        "coordination_agreed": coord_agreement.get("agreed", 0),
        "coordination_disagreed": coord_agreement.get("disagreed", 0),
        "coordination_unclear": coord_agreement.get("unclear", 0),
    }


def mean(values):
    return statistics.mean(values) if values else None


def aggregate(run_dir):
    results = load_results(run_dir)
    per_game = []
    missing_behaviour = []

    for game_id, row in results.items():
        behaviour_path = run_dir / f"{game_id}.behaviour.json"
        if not behaviour_path.exists():
            missing_behaviour.append(game_id)
            continue
        behaviour = json.loads(behaviour_path.read_text(encoding="utf-8"))
        stats = per_game_behaviour_stats(behaviour)
        repeat_kind_correct, repeat_kind_wrong = repeat_kind_qa(run_dir / f"{game_id}.jsonl", behaviour)
        stats.update({
            "game_id": game_id,
            "condition": row["condition"],
            "outcome": row.get("outcome"),
            "first_accusation_correct": row.get("first_accusation_correct"),
            "attempts_used": row.get("attempts_used"),
            "interrogations_used": row.get("interrogations_used"),
            "exchanges_total": row.get("exchanges_total"),
            "gameplay_cost_usd": row.get("cost_usd"),
            "repeat_kind_qa_correct": repeat_kind_correct,
            "repeat_kind_qa_wrong": repeat_kind_wrong,
        })
        per_game.append(stats)

    by_condition = {}
    for condition in sorted({g["condition"] for g in per_game}):
        games = [g for g in per_game if g["condition"] == condition]
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

    return {"per_game": per_game, "by_condition": by_condition, "missing_behaviour": missing_behaviour}


def print_summary(report):
    conditions = sorted(report["by_condition"])
    print(f"{'metric':38s} " + " ".join(f"{c:>12s}" for c in conditions))
    print("-" * (38 + 13 * len(conditions)))

    def row(label, key, fmt="{:.3f}"):
        print(f"{label:38s} " + " ".join(
            fmt.format(report["by_condition"][c][key]) if report["by_condition"][c].get(key) is not None else f"{'n/a':>12s}"
            for c in conditions))

    print(f"{'n_games':38s} " + " ".join(f"{report['by_condition'][c]['n_games']:12d}" for c in conditions))
    row("solve_rate", "solve_rate")
    row("solved_first_rate (no retry needed)", "solved_first_rate")
    row("mean attempts_used", "attempts_used")
    row("mean interrogations_used", "interrogations_used")
    print()
    row("repeated_question_rate (per exchange)", "repeated_question_rate")
    print("  ^ is_repeat itself: validated reliable (~95-97%) across all prior checks.")
    row("  of which: unnecessary (mean count)", "repeat_unnecessary")
    row("  of which: expired_evidence_recovery", "repeat_expired_evidence_recovery")
    row("  >> QA: repeat_kind split accuracy <<", "repeat_kind_qa_accuracy")
    print("  ^^ CAUTION: the unnecessary/expired_evidence_recovery split above is NOT")
    print("     reliable where this QA accuracy is low -- recomputed directly from each")
    print("     game's real event log against the same rule stated in the annotator's")
    print("     own prompt (distance > k -> expired). See this script's module docstring.")
    row("deception_rate (per exchange)", "deception_rate")
    row("  authorised (mean count)", "deception_authorised")
    row("  unauthorised (mean count)", "deception_unauthorised")
    row("withholding: explicit_refusal (mean count)", "withholding_explicit_refusal")
    row("withholding: partial (mean count)", "withholding_partial")
    row("challenge_total (mean count)", "challenge_total")
    row("challenge_evidence_based (mean count)", "challenge_evidence_based")
    row("corroboration_requests (mean count)", "corroboration_requests")
    row("unsupported_additions (mean count)", "unsupported_additions")
    row("uncertain_confidence_rate (per exchange)", "uncertain_confidence_rate")
    print()
    row("newly_disclosed_leads (mean count)", "newly_disclosed_leads")
    row("missed_required_disclosures (mean count)", "missed_required_disclosures")
    row("checkpoints_reached (mean)", "checkpoints_reached")
    row("coordination_instances_total (mean count)", "coordination_instances_total")
    row("  agreed (mean count)", "coordination_agreed")
    row("  disagreed (mean count)", "coordination_disagreed")
    row("  unclear (mean count)", "coordination_unclear")


def main():
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "annotation" / "main_01"
    report = aggregate(run_dir)
    if report["missing_behaviour"]:
        print(f"WARNING: {len(report['missing_behaviour'])} games have no .behaviour.json "
              f"(not yet annotated) and were excluded: {report['missing_behaviour'][:5]}...")
        print()
    print_summary(report)
    out_path = run_dir / "annotation_aggregate.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out_path} ({len(report['per_game'])} games)")


if __name__ == "__main__":
    main()

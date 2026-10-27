"""Run from repo root: python qa/aggregate_annotation_results.py <run_dir> [--source-dir DIR]

Aggregates one annotated run directory's behavioural annotation output
(<run_dir>/*.behaviour.json, written by annotation.pipeline.annotate_game) by
condition, joined against <source_dir>/results.jsonl for the gameplay outcome (solved
or not, attempts used) and <source_dir>/<game_id>.jsonl for the repeat_kind QA check.
--source-dir defaults to run_dir (the historical assumption that gameplay and
annotation output share one directory), but can point elsewhere -- e.g. annotation
output written to annotation/main_01 while the games that produced it were played into
a different run directory. A missing results.jsonl raises a clear error (see
load_results) rather than silently reporting zero games.

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

repeated_question_rate_per_interrogation uses interrogations_used (the gameplay row's
own count of interrogation-kind exchanges) as its denominator, per the proposal's
definition of the metric. repeated_question_rate (kept alongside it, unchanged) uses
exchanges_labelled instead -- every detective-visible exchange, including voluntary
disclosures, which structurally can never be a repeated question
(annotation.prompts.candidate_repeat_distances only ever considers interrogation-kind
exchanges) -- so that denominator dilutes the rate below the proposal's intended
reading. Report the _per_interrogation figure when citing this metric; the original is
kept only for continuity with earlier runs that only have it.
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


def load_results(source_dir):
    """Rows from the gameplay run's results.jsonl. Raises FileNotFoundError with a
    specific message if it's missing -- this is a required input (which games to
    aggregate), not something that can legitimately be empty, so it must not be
    confused with "this run has zero games" (same principle as
    annotation.orchestrate.game_rows, which has the matching live-annotation bug)."""
    path = Path(source_dir) / "results.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. This is the gameplay run's required results.jsonl "
            f"-- if annotation output lives in a different directory than where the "
            f"games were played, pass --source-dir separately from the run_dir argument.")
    rows = {}
    with path.open(encoding="utf-8") as handle:
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
        "undiscovered_leads": len(behaviour.get("undiscovered_leads", [])),
        "checkpoints_reached": behaviour["voluntary_interaction"]["checkpoints_reached"],
        "coordination_instances_total": len(coord),
        "coordination_agreed": coord_agreement.get("agreed", 0),
        "coordination_disagreed": coord_agreement.get("disagreed", 0),
        "coordination_unclear": coord_agreement.get("unclear", 0),
    }


def mean(values):
    return statistics.mean(values) if values else None


def aggregate(run_dir, source_dir=None):
    """run_dir: where *.behaviour.json / annotation_aggregate.json live. source_dir:
    where the gameplay run's results.jsonl and *.jsonl logs live, if different from
    run_dir (defaults to run_dir)."""
    run_dir = Path(run_dir)
    source_dir = Path(source_dir) if source_dir else run_dir
    results = load_results(source_dir)
    per_game = []
    missing_behaviour = []

    for game_id, row in results.items():
        behaviour_path = run_dir / f"{game_id}.behaviour.json"
        if not behaviour_path.exists():
            missing_behaviour.append(game_id)
            continue
        behaviour = json.loads(behaviour_path.read_text(encoding="utf-8"))
        stats = per_game_behaviour_stats(behaviour)
        repeat_kind_correct, repeat_kind_wrong = repeat_kind_qa(source_dir / f"{game_id}.jsonl", behaviour)
        interrogations_used = row.get("interrogations_used")
        repeated_question_total = stats["repeat_unnecessary"] + stats["repeat_expired_evidence_recovery"]
        stats.update({
            "game_id": game_id,
            "condition": row["condition"],
            "outcome": row.get("outcome"),
            "first_accusation_correct": row.get("first_accusation_correct"),
            "attempts_used": row.get("attempts_used"),
            "interrogations_used": interrogations_used,
            "exchanges_total": row.get("exchanges_total"),
            "gameplay_cost_usd": row.get("cost_usd"),
            "repeat_kind_qa_correct": repeat_kind_correct,
            "repeat_kind_qa_wrong": repeat_kind_wrong,
            # Per the proposal's own definition: repeated questions per interrogation
            # turn, NOT per detective-visible exchange (repeated_question_rate above
            # dilutes the denominator with voluntary disclosures, which structurally
            # can never be a repeated question -- see module docstring).
            "repeated_question_rate_per_interrogation":
                repeated_question_total / interrogations_used if interrogations_used else None,
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
    row("repeated_question_rate_per_interrogation", "repeated_question_rate_per_interrogation")
    print("  ^ the proposal's definition of this metric -- denominator is")
    print("    interrogations_used, not every detective-visible exchange.")
    row("repeated_question_rate (per exchange, legacy)", "repeated_question_rate")
    print("  ^ kept for continuity with earlier runs; denominator includes voluntary")
    print("    disclosures, which can never be a repeated question -- see module docstring.")
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
    print("  ^ a genuine disclosure violation: someone who was asked never surfaced it.")
    row("undiscovered_leads (mean count, informational)", "undiscovered_leads")
    print("  ^ NOT a violation -- nobody who could deliver this was ever questioned at")
    print("    all, so no disclosure opportunity existed. See qa/annotation_rubric.md.")
    row("checkpoints_reached (mean)", "checkpoints_reached")
    row("coordination_instances_total (mean count)", "coordination_instances_total")
    row("  agreed (mean count)", "coordination_agreed")
    row("  disagreed (mean count)", "coordination_disagreed")
    row("  unclear (mean count)", "coordination_unclear")


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", nargs="?", default=str(ROOT / "annotation" / "main_01"),
                        help="Where *.behaviour.json / annotation_aggregate.json live")
    parser.add_argument("--source-dir", default=None,
                        help="Where the gameplay run's results.jsonl and *.jsonl logs live, if different from run_dir")
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    report = aggregate(run_dir, source_dir=args.source_dir)
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

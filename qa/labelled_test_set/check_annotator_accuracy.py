"""Run from repo root: python qa/labelled_test_set/check_annotator_accuracy.py [--samples N]

Checks the real LLM annotator's accuracy against the manually labelled ground truth
built in this directory (pilot07b_{none,ledger,raw}_behaviour.json / _overlay.json),
for the same three pilot_07b games the worksheets cover.

For each game, runs N independent real annotation calls (default 3, override with
--samples) at the project's real decoding settings (temperature=0.7, no fixed seed) --
single-sample runs were previously misleading: identical, unmodified prompts produced
visibly different outputs call to call (e.g. checkpoints_reached swinging between 1, 3,
and 15 across runs; see qa/labelled_test_set/annotator_accuracy_report.json's history).
Averaging across samples, and reporting the spread, separates real prompt-quality
signal from this sampling noise.

For each (game, sample):
  1. Calls annotation.pipeline.annotate_game -- one real LLM annotation call, same
     code path the project would use for the full 300-game run (same model_id as
     gameplay, gemini-3.1-flash-lite, per configs/experiment_config.final.json).
  2. Writes the annotator's raw output to
     annotator_runs/<game_id>.sample<i>.behaviour.json and .annotation_call.json
     (full call record, for audit) -- kept separate from both the original pilot_07b/
     run directory and the hand-built ground truth files.
  3. Diffs the annotator's exchange_behaviour field-by-field against the manually
     labelled ground truth, plus missed_required_disclosures and voluntary_interaction.

Then reports, per field: mean accuracy across all samples, the per-sample spread (so a
field that's "90% accurate on average but swings 70-100%" reads differently from one
that's "90% accurate, every sample"), and the majority-vote accuracy (what accuracy
would be if we took the most common answer across the N samples per exchange instead of
trusting any single call) -- a concrete answer to "would sampling multiple times and
voting actually help the real 300-game run."

Requires GEMINI_API_KEY in the environment (.env, loaded via python-dotenv) -- this
makes a small number of real, billed API calls (samples x 3 games).
"""

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from casework import load_case  # noqa: E402
from infra.llm import LLMClient, GeminiBackend  # noqa: E402
from infra.pricing import pricing_for  # noqa: E402
from annotation.pipeline import annotate_game  # noqa: E402

RUN_DIR = ROOT / "pilot_07b"
OUT_DIR = Path(__file__).resolve().parent
ANNOTATOR_OUT_DIR = OUT_DIR / "annotator_runs"
MODEL_ID = "gemini-3.1-flash-lite"  # same model as gameplay, per configs/experiment_config.final.json

GAMES = [
    ("none", "pilot_07b_none_1118654058"),
    ("ledger", "pilot_07b_ledger_444382084"),
    ("raw", "pilot_07b_raw_1569954369"),
]

SCALAR_FIELDS = [
    "lead_follow_up.is_follow_up_to_lead_id",
    "lead_follow_up.follow_up_relevance",
    "corroboration.is_request",
    "corroboration.target_subject",
    "corroboration.target_time",
    "challenge.is_challenge",
    "challenge.evidence_based",
    "repeated_question.is_repeat",
    "repeated_question.repeat_kind",
    "repeated_question.matches_exchange_id",
    "withholding.explicit_refusal",
    "withholding.partial_withholding",
    "deception.contains_false_statement",
    "deception.authorised",
    "deception.claim_id_if_known",
    "confidence",
]
SET_FIELDS = [
    "lead_follow_up.newly_disclosed_lead_ids",
    "challenge.cited_claim_ids",
]


def get_path(entry, path):
    node = entry
    for part in path.split("."):
        node = node[part]
    return node


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def make_llm():
    pricing = pricing_for(MODEL_ID)
    decoding = {"temperature": 0.7, "top_p": 0.95, "thinking_level": "minimal"}
    return LLMClient(GeminiBackend(), MODEL_ID, decoding, log=None, pricing=pricing)


def field_value(entry, path):
    if path in SET_FIELDS:
        return tuple(sorted(get_path(entry, path)))
    return get_path(entry, path)


def diff_exchange(gold, pred, exchange_id, game_id, sample_idx, mismatches):
    for path in SCALAR_FIELDS + SET_FIELDS:
        gold_value = field_value(gold, path)
        pred_value = field_value(pred, path)
        mismatches.append({
            "game_id": game_id, "exchange_id": exchange_id, "sample": sample_idx, "field": path,
            "gold": gold_value, "pred": pred_value, "match": gold_value == pred_value,
        })
    # unsupported_additions: presence/absence agreement only (free-text citations
    # can't be exact-matched the same way; whether either side flagged *anything* is
    # still a meaningful signal).
    gold_has_addition = bool(gold["unsupported_additions"])
    pred_has_addition = bool(pred["unsupported_additions"])
    mismatches.append({
        "game_id": game_id, "exchange_id": exchange_id, "sample": sample_idx,
        "field": "unsupported_additions.any_flagged",
        "gold": gold_has_addition, "pred": pred_has_addition, "match": gold_has_addition == pred_has_addition,
    })


def diff_whole_game(gold_behaviour, pred_behaviour, game_id, sample_idx, whole_game_rows):
    gold_missed = tuple(sorted({item["lead_id_or_claim_id"] for item in gold_behaviour["missed_required_disclosures"]}))
    pred_missed = tuple(sorted({item["lead_id_or_claim_id"] for item in pred_behaviour["missed_required_disclosures"]}))
    gold_voluntary = gold_behaviour["voluntary_interaction"]
    pred_voluntary = pred_behaviour["voluntary_interaction"]
    gold_coord = tuple(sorted({item["conversation_id"] for item in gold_voluntary["coordination_instances"]}))
    pred_coord = tuple(sorted({item["conversation_id"] for item in pred_voluntary["coordination_instances"]}))
    whole_game_rows.append({
        "game_id": game_id, "sample": sample_idx,
        "missed_required_disclosures": {"gold": gold_missed, "pred": pred_missed, "match": gold_missed == pred_missed},
        "checkpoints_reached": {"gold": gold_voluntary["checkpoints_reached"], "pred": pred_voluntary["checkpoints_reached"],
                                "match": gold_voluntary["checkpoints_reached"] == pred_voluntary["checkpoints_reached"]},
        "coordination_instances_flagged": {"gold": gold_coord, "pred": pred_coord, "match": gold_coord == pred_coord},
    })


def per_sample_accuracy(mismatches, field):
    """One accuracy number per (game_id, sample) for this field, for computing spread."""
    by_sample = {}
    for row in mismatches:
        if row["field"] != field:
            continue
        key = (row["game_id"], row["sample"])
        bucket = by_sample.setdefault(key, {"total": 0, "matches": 0})
        bucket["total"] += 1
        bucket["matches"] += row["match"]
    return [b["matches"] / b["total"] for b in by_sample.values() if b["total"]]


def summarise(mismatches):
    fields = sorted({row["field"] for row in mismatches})
    summary = {}
    for field in fields:
        rows = [row for row in mismatches if row["field"] == field]
        accuracies = per_sample_accuracy(mismatches, field)
        summary[field] = {
            "mean_accuracy": sum(row["match"] for row in rows) / len(rows),
            "n": len(rows),
            "per_sample_min": min(accuracies) if accuracies else None,
            "per_sample_max": max(accuracies) if accuracies else None,
            "per_sample_stdev": statistics.pstdev(accuracies) if len(accuracies) > 1 else 0.0,
        }
    return summary


def majority_vote_accuracy(mismatches):
    """If we'd taken the most common predicted value across samples per exchange
    (instead of trusting any one call), what accuracy would that ensemble get? Answers
    "would sampling N times and voting help" directly, rather than by inference."""
    by_exchange_field = {}
    for row in mismatches:
        key = (row["game_id"], row["exchange_id"], row["field"])
        by_exchange_field.setdefault(key, {"gold": row["gold"], "preds": []})["preds"].append(row["pred"])
    by_field = {}
    for (_game_id, _exchange_id, field), data in by_exchange_field.items():
        preds = data["preds"]
        winner = Counter(preds).most_common(1)[0][0] if preds else None
        bucket = by_field.setdefault(field, {"total": 0, "matches": 0})
        bucket["total"] += 1
        bucket["matches"] += (winner == data["gold"])
    return {field: b["matches"] / b["total"] for field, b in sorted(by_field.items())}


def main(num_samples=3, use_cache=False):
    load_dotenv()
    ANNOTATOR_OUT_DIR.mkdir(exist_ok=True)
    case = load_case(ROOT / "cases" / "pilot.json")

    all_mismatches = []
    whole_game_rows = []
    coverage_rows = []
    total_cost = 0.0

    for label, game_id in GAMES:
        log_path = RUN_DIR / f"{game_id}.jsonl"
        gold = load_json(OUT_DIR / f"pilot07b_{label}_behaviour.json")
        gold_ids = set(gold["exchange_behaviour"])

        for sample_idx in range(num_samples):
            print(f"=== {label} ({game_id}) sample {sample_idx + 1}/{num_samples} ===")
            out_path = ANNOTATOR_OUT_DIR / f"{game_id}.sample{sample_idx}.behaviour.json"

            if use_cache and out_path.exists():
                predicted = load_json(out_path)
                print(" (using cached annotator output, no new API call)")
            else:
                record, predicted = annotate_game(log_path, case, make_llm(), game_id=game_id, out_path=out_path)
                total_cost += record.get("cost_usd") or 0.0
                print(f" annotator call cost: ${record.get('cost_usd'):.4f}" if record.get("cost_usd") is not None
                      else " annotator call cost: unknown (no pricing table entry)")

            pred_ids = set(predicted["exchange_behaviour"])
            missing = sorted(gold_ids - pred_ids, key=lambda eid: int(eid[1:]))
            extra = sorted(pred_ids - gold_ids, key=lambda eid: int(eid[1:]))
            coverage_rows.append({"game_id": game_id, "sample": sample_idx, "total_exchanges": len(gold_ids),
                                  "annotator_covered": len(gold_ids & pred_ids),
                                  "missing_from_annotator": missing, "unexpected_extra": extra})
            if missing or extra:
                print(f" WARNING: SKIPPED {len(missing)}/{len(gold_ids)} exchanges entirely: {missing}."
                      + (f" Unexpected extra id(s): {extra}." if extra else ""))

            for exchange_id in sorted(gold_ids & pred_ids, key=lambda eid: int(eid[1:])):
                diff_exchange(gold["exchange_behaviour"][exchange_id], predicted["exchange_behaviour"][exchange_id],
                              exchange_id, game_id, sample_idx, all_mismatches)
            diff_whole_game(gold, predicted, game_id, sample_idx, whole_game_rows)

    print()
    print("=== Coverage (mean exchanges covered per sample) ===")
    total_exchanges = sum(r["total_exchanges"] for r in coverage_rows)
    total_covered = sum(r["annotator_covered"] for r in coverage_rows)
    print(f"  TOTAL across {len(coverage_rows)} samples: {total_covered}/{total_exchanges} ({total_covered/total_exchanges*100:.1f}%)")
    any_gaps = [r for r in coverage_rows if r["missing_from_annotator"]]
    if any_gaps:
        print(f"  {len(any_gaps)}/{len(coverage_rows)} samples had at least one skipped exchange.")
    print()

    field_summary = summarise(all_mismatches)
    vote_summary = majority_vote_accuracy(all_mismatches)
    print(f"=== Per-field accuracy: mean across {num_samples} samples x 3 games, "
          f"[min-max] spread, and majority-vote ensemble accuracy ===")
    for field, stats in field_summary.items():
        print(f"  {field:45s} mean={stats['mean_accuracy']*100:5.1f}%  "
              f"[{stats['per_sample_min']*100:5.1f}-{stats['per_sample_max']*100:5.1f}%]  "
              f"stdev={stats['per_sample_stdev']*100:4.1f}pp  "
              f"vote={vote_summary[field]*100:5.1f}%  (n={stats['n']})")

    overall_matches = sum(row["match"] for row in all_mismatches)
    print(f"\nOverall mean field-level agreement: {overall_matches}/{len(all_mismatches)} "
          f"({overall_matches/len(all_mismatches)*100:.1f}%)")

    print("\n=== Whole-game judgements (per sample) ===")
    for label, game_id in GAMES:
        rows = [r for r in whole_game_rows if r["game_id"] == game_id]
        missed_matches = sum(r["missed_required_disclosures"]["match"] for r in rows)
        checkpoints_matches = sum(r["checkpoints_reached"]["match"] for r in rows)
        coord_matches = sum(r["coordination_instances_flagged"]["match"] for r in rows)
        checkpoint_preds = [r["checkpoints_reached"]["pred"] for r in rows]
        print(f" {game_id}: missed_required_disclosures matched gold in {missed_matches}/{len(rows)} samples; "
              f"checkpoints_reached matched in {checkpoints_matches}/{len(rows)} samples "
              f"(gold={rows[0]['checkpoints_reached']['gold']}, predicted values across samples={checkpoint_preds}); "
              f"coordination_instances matched in {coord_matches}/{len(rows)} samples")

    print(f"\nTotal annotator call cost this run: ${total_cost:.4f}")

    disagreements = [row for row in all_mismatches if not row["match"]]
    report_path = OUT_DIR / "annotator_accuracy_report.json"
    report_path.write_text(json.dumps({
        "num_samples_per_game": num_samples,
        "coverage": coverage_rows,
        "per_field_accuracy": field_summary,
        "majority_vote_accuracy": vote_summary,
        "overall_mean_field_agreement": overall_matches / len(all_mismatches),
        "whole_game_judgements": whole_game_rows,
        "disagreements": disagreements,
        "total_cost_usd": total_cost,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {report_path} ({len(disagreements)} field-level disagreements logged for manual review)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=3, help="Independent annotation calls per game (default 3)")
    parser.add_argument("--from-cache", action="store_true",
                       help="Re-score already-saved annotator_runs/*.sample<i>.behaviour.json instead of making new API calls")
    args = parser.parse_args()
    main(num_samples=args.samples, use_cache=args.from_cache)

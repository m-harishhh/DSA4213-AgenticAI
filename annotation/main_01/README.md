# main_01 — the 300-game study's annotation results

300 games (100 per memory condition — `none`, `ledger`, `raw`), `cases/evaluation.json`,
`gemini-3.1-flash-lite`, fully annotated by `annotation/pipeline.py` and verified against
manually hand-labelled ground truth (`qa/labelled_test_set/`) before being trusted.

## Results

Every field from `annotation_aggregate.json`'s per-condition summary, mean per game
unless marked otherwise (n=100 games per condition).

| metric | `ledger` | `none` | `raw` |
|---|---|---|---|
| n_games | 100 | 100 | 100 |
| solve_rate | 0.5400 | 0.0400 | 0.5700 |
| solved_first_rate | 0.3100 | 0.0000 | 0.3100 |
| failed_run_count | 0 | 4 | 0 |
| exchanges_labelled | 20.4300 | 22.3300 | 20.2800 |
| repeated_question_rate (per exchange) | 0.1665 | 0.5386 | 0.2093 |
| repeat_unnecessary | 0.4400 | 0 | 0.3500 |
| repeat_expired_evidence_recovery | 3.1200 | 12.0500 | 4.0500 |
| deception_rate (per exchange) | 0.3439 | 0.6427 | 0.3525 |
| deception_authorised | 7.0100 | 14.3800 | 7.1400 |
| deception_unauthorised | 0.0100 | 0 | 0 |
| withholding_explicit_refusal | 0.0700 | 0 | 0.1300 |
| withholding_partial | 5.7200 | 6.1600 | 5.3200 |
| challenge_total | 1.1600 | 0.6700 | 0.7800 |
| challenge_evidence_based | 0.6400 | 0.5700 | 0.5600 |
| corroboration_requests | 0.4900 | 1.0100 | 0.7000 |
| unsupported_additions | 0.0400 | 0.0200 | 0.0400 |
| uncertain_confidence_rate (per exchange) | 0.0000 | 0.0000 | 0.0000 |
| newly_disclosed_leads | 10.2100 | 8.8300 | 10.4600 |
| follow_up_relevant | 3.4300 | 1.6100 | 5.7600 |
| follow_up_off_target | 0.1200 | 0 | 0.1400 |
| missed_required_disclosures | 0.1700 | 2.3200 | 0.1500 |
| checkpoints_reached | 2.8700 | 3 | 2.9000 |
| coordination_instances_total | 3.5700 | 3.4300 | 3.3800 |
| coordination_agreed | 1.1000 | 1.1500 | 1.1500 |
| coordination_disagreed | 1.9700 | 1.8100 | 1.7200 |
| coordination_unclear | 0.5000 | 0.4700 | 0.5100 |
| attempts_used | 2.1700 | 2.8700 | 2.1800 |
| interrogations_used | 17.5800 | 19.3800 | 17.3800 |
| exchanges_total | 20.4300 | 22.3300 | 20.2800 |
| gameplay_cost_usd | 0.0443 | 0.0312 | 0.0300 |
| repeat_kind_qa_correct (self-check: see below) | 3.5500 | 12.0500 | 4.4000 |
| repeat_kind_qa_wrong | 0 | 0 | 0 |
| repeat_kind_qa_accuracy | 1.0000 | 1.0000 | 1.0000 |

`repeat_kind_qa_*`: a built-in correctness check, not a behavioural metric — it
recomputes the correct `unnecessary`/`expired_evidence_recovery` label directly from
each game's real event log (the same rule the annotator's own prompt states) and
compares it to what the annotator actually output. 100% accuracy across all three
conditions confirms the annotation itself is reliable on this field.

## Files

- `<game_id>.behaviour.json` — the behavioural annotation for one game (one per game,
  300 total), matching `schemas/annotation_behaviour.schema.json`.
- `<game_id>.annotation_call.json` — the full raw LLM call record for that game's
  annotation (prompt sent, raw response, token usage), kept for audit.
- `annotation_results.jsonl` — one row per game: status, cost, exchange/disclosure/
  coordination counts.
- `annotation_aggregate.json` — the per-condition summary above, plus raw per-game
  data. A frozen snapshot, not regenerable: `qa/aggregate_annotation_results.py` needs
  `results.jsonl` (the gameplay-outcome log) to recompute `solve_rate`/`attempts_used`/
  etc.
- `run.json` — the original gameplay run's configuration (model, decoding settings,
  per-condition game count, seed).

Raw gameplay transcripts (the detective/suspect dialogue the annotator read) are not
kept here — only the annotation output.

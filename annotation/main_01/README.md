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
| repeated_question_rate_per_interrogation | 0.1929 | 0.6072 | 0.2433 |
| repeated_question_rate (per exchange, legacy) | 0.1661 | 0.5270 | 0.2088 |
| repeat_unnecessary | 0.4400 | 0 | 0.3500 |
| repeat_expired_evidence_recovery | 3.1100 | 11.7900 | 4.0400 |
| deception_rate (per exchange) | 0.3421 | 0.6308 | 0.3520 |
| deception_authorised | 6.9700 | 14.1100 | 7.1300 |
| deception_unauthorised | 0.0100 | 0 | 0 |
| withholding_explicit_refusal | 0.0700 | 0 | 0.1300 |
| withholding_partial | 5.7000 | 5.9700 | 5.3200 |
| challenge_total | 1.1500 | 0.6400 | 0.7800 |
| challenge_evidence_based | 0.6300 | 0.5500 | 0.5600 |
| corroboration_requests | 0.4900 | 0.9900 | 0.7000 |
| unsupported_additions | 0.0400 | 0.0200 | 0.0400 |
| uncertain_confidence_rate (per exchange) | 0.0023 | 0.0194 | 0.0010 |
| newly_disclosed_leads | 10.1200 | 8.7600 | 10.3500 |
| follow_up_relevant | 3.4200 | 1.5500 | 5.7600 |
| follow_up_off_target | 0.1200 | 0 | 0.1400 |
| missed_required_disclosures | 0.1500 | 0.3700 | 0.1100 |
| undiscovered_leads (informational, not a violation) | 0 | 1.8300 | 0 |
| checkpoints_reached | 2.8700 | 3 | 2.9000 |
| coordination_instances_total | 3.5100 | 3.2700 | 3.2600 |
| coordination_agreed | 1.0800 | 1.0900 | 1.1100 |
| coordination_disagreed | 1.9500 | 1.7500 | 1.6500 |
| coordination_unclear | 0.4800 | 0.4300 | 0.5000 |
| attempts_used | 2.1700 | 2.8700 | 2.1800 |
| interrogations_used | 17.5800 | 19.3800 | 17.3800 |
| exchanges_total | 20.4300 | 22.3300 | 20.2800 |
| gameplay_cost_usd | 0.0443 | 0.0312 | 0.0300 |
| repeat_kind_qa_correct (self-check: see below) | 3.5400 | 11.7900 | 4.3900 |
| repeat_kind_qa_wrong | 0 | 0 | 0 |
| repeat_kind_qa_accuracy | 1.0000 | 1.0000 | 1.0000 |

`repeated_question_rate_per_interrogation`: repeated questions per interrogation turn
(`interrogations_used` as the denominator), matching the proposal's own definition of
this metric. `repeated_question_rate` (kept alongside it for continuity with earlier
runs) divides by `exchanges_labelled` instead, which includes voluntary disclosures —
exchanges that structurally can never be a repeated question
(`annotation.prompts.candidate_repeat_distances` only ever considers interrogation-kind
exchanges) — so that denominator understates the rate. Use the `_per_interrogation`
figure when citing this metric.

`missed_required_disclosures` vs. `undiscovered_leads`: the former is a genuine
disclosure violation — someone who was actually questioned never surfaced a lead they
were obligated to. The latter is purely informational and NOT a violation — nobody who
could have delivered that lead was ever questioned at all, so no disclosure opportunity
ever existed. See `qa/annotation_rubric.md` §8 for the full distinction.

`repeat_kind_qa_*`: a built-in correctness check, not a behavioural metric — it
recomputes the correct `unnecessary`/`expired_evidence_recovery` label directly from
each game's real transcript (the same rule the annotator's own prompt states) and
compares it to what the annotator actually output. 100% accuracy across all three
conditions confirms the annotation itself is reliable on this field.

## Files

- `<game_id>.behaviour.json` — the behavioural annotation for one game (one per game,
  300 total), matching `schemas/annotation_behaviour.schema.json`.
- `<game_id>.annotation_call.json` — the full raw LLM call record for that game's
  annotation (prompt sent, raw response, token usage), kept for audit.
- `results.jsonl` — the gameplay run's own output: status, outcome, attempts/
  interrogations used, cost, and per-game log paths.
- `annotation_results.jsonl` — one row per game: status, cost, exchange/disclosure/
  coordination counts.
- `annotation_aggregate.json` — the per-condition summary above, plus raw per-game
  data. Regenerable via `qa/aggregate_annotation_results.py annotation/main_01
  --source-dir <raw-per-game-log-directory>`.
- `run.json` — the original gameplay run's configuration (model, decoding settings,
  per-condition game count, seed).

The raw per-game gameplay logs (`<game_id>.jsonl`, `<game_id>.exchanges.json`,
`<game_id>.transcript.md` — the actual detective/suspect dialogue) are not kept in this
repo, to avoid bloating it; `--source-dir` on `qa/aggregate_annotation_results.py` and
`python -m annotation run` can point at wherever that directory lives if you have a copy.

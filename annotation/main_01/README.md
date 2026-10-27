# main_01 — the 300-game study's annotation results

300 games (100 per memory condition — `none`, `ledger`, `raw`), `cases/evaluation.json`,
`gemini-3.1-flash-lite`, fully annotated by `annotation/pipeline.py` and verified against
manually hand-labelled ground truth (`qa/labelled_test_set/`) before being trusted.

## Results

Every field from `annotation_aggregate.json`'s per-condition summary, mean per game
unless marked otherwise (n=100 games per condition). These numbers reflect a post-review
repair pass (see "Citation and disclosure repair" below) — they are not what the
annotator originally output.

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
| missed_required_disclosures | 0.1700 | 0.4900 | 0.1500 |
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
ever existed. These were conflated in the annotator's original output (see "Citation and
disclosure repair" below); they are reported as two separate fields here.

`repeat_kind_qa_*`: a built-in correctness check, not a behavioural metric — it
recomputes the correct `unnecessary`/`expired_evidence_recovery` label directly from
each game's real transcript (the same rule the annotator's own prompt states) and
compares it to what the annotator actually output. 100% accuracy across all three
conditions confirms the annotation itself is reliable on this field.

## Citation and disclosure repair

A PR review of this run's annotation output found two real problems, both now fixed
going forward in `annotation/prompts.py` and `annotation/pipeline.py` (see their
docstrings for the full fix) and repaired here in the existing 300-game output by
`qa/fix_main_01_citations.py`:

1. **Missed vs. undiscovered disclosures.** The annotator's original prompt told the
   model to treat "nobody who could deliver this lead ever spoke" as near-default
   evidence FOR a `missed_required_disclosures` violation — the opposite of correct: a
   speaker never being questioned establishes undiscovered information, not a
   disclosure failure. 183 of 232 `missed_required_disclosures` labels in the
   `none` condition were exactly this non-violation. The repair moved every such entry
   into the new `undiscovered_leads` field instead (added to
   `schemas/annotation_behaviour.schema.json`); `annotation/prompts.py`'s prompt no
   longer suggests these as candidates at all, going forward.
2. **Citations not faithful to the transcript.** An independent review found 51
   exchange citations that were not exact substrings of anything the cited exchange
   actually said, and roughly 3,700 more (across exchange, `unsupported_additions`, and
   `coordination_instances` citations) whose `start`/`end` offsets did not locate their
   own citation text. `annotation/pipeline.py`'s `validate_against_transcript` now
   rejects both at annotation time for every future run. For this run, since the raw
   per-game event logs were already deleted during an earlier cleanup pass (see
   `annotation/README.md`), the repair script reconstructed each game's exact
   transcript from the still-present `<game_id>.annotation_call.json` (the full prompt
   sent to the annotator, which embeds the rendered transcript) — fixed any citation
   whose text was a genuine substring at the wrong offset, and withdrew (reset to
   negative defaults, noted in `annotator_notes`) any citation whose text did not exist
   in the transcript at all.

`qa/main_01_fix_report.json` has the exact per-game and total counts from this pass
(4,608 offsets fixed, 51 citations withdrawn, 34 `coordination_instances` withdrawn, 22
dangling id references cleared, 183 disclosure entries reclassified). The table above
and `annotation_aggregate.json` reflect the repaired data; `repeat_kind_qa_correct`/
`_wrong` were also recomputed against the repaired labels (via the same reconstructed
transcript), not carried over stale.

## Files

- `<game_id>.behaviour.json` — the behavioural annotation for one game (one per game,
  300 total), matching `schemas/annotation_behaviour.schema.json`. Repaired in place
  (see above); the original, unrepaired model output is still recoverable from each
  game's `.annotation_call.json` (its `parsed` field).
- `<game_id>.annotation_call.json` — the full raw LLM call record for that game's
  annotation (prompt sent, raw response, token usage), kept for audit -- and, as of the
  repair pass above, as the only remaining record of each game's real transcript.
- `annotation_results.jsonl` — one row per game: status, cost, exchange/disclosure/
  coordination counts. Regenerated after the repair pass so its counts match the
  repaired `.behaviour.json` files.
- `annotation_aggregate.json` — the per-condition summary above, plus raw per-game
  data. A frozen snapshot, not regenerable from scratch via
  `qa/aggregate_annotation_results.py`: that script needs `results.jsonl` (the
  gameplay-outcome log), which was deleted during an earlier cleanup pass and cannot be
  reconstructed. It was instead regenerated in place by
  `qa/fix_main_01_citations.py`, which carries over the gameplay-sourced fields
  (`condition`/`outcome`/`attempts_used`/`interrogations_used`/`exchanges_total`/
  `gameplay_cost_usd`, untouched by citation repair) from the pre-repair aggregate and
  freshly recomputes every annotation-sourced field from the repaired data.
- `run.json` — the original gameplay run's configuration (model, decoding settings,
  per-condition game count, seed).

Raw gameplay transcripts (the detective/suspect dialogue the annotator read) are not
kept here as their own files — only the annotation output, plus the reconstructable
copy embedded in each `.annotation_call.json`'s prompt.

# Annotation pipeline (owned by Elena)

Turns the rubric in `qa/annotation_rubric.md` into runnable code: one LLM call per game,
reading that game's real event log plus the case, writing a validated,
schema-conformant behavioural annotation next to the log. This is what labelled every
game in the 300-game study (`annotation/main_01/`) — see `qa/labelled_test_set/` for how
its accuracy was checked against manually hand-labelled ground truth before being
trusted at that scale.

```bash
python -m annotation run --run-dir annotation/main_01 --case cases/evaluation.json --backend mock   # offline, no cost
python -m annotation run --run-dir annotation/main_01 --case cases/evaluation.json --backend gemini \
    --config configs/experiment_config.final.json                 # direct Google API key
python -m annotation run --run-dir annotation/main_01 --case cases/evaluation.json --backend openrouter \
    --config configs/experiment_config.final.json                 # same model via OpenRouter instead
python -m annotation run --run-dir annotation/main_01 --case cases/evaluation.json --backend gemini \
    --config configs/experiment_config.final.json --sample-size 30   # a smaller balanced sample instead
python -m annotation run --run-dir runs/annotation_out --source-dir runs/pilot_01 \
    --case cases/evaluation.json --backend gemini --config configs/experiment_config.final.json
    # --source-dir: read results.jsonl from a different directory than the annotation output goes to
python -m annotation summary annotation/main_01
```

## Files

| File | Purpose |
|---|---|
| `prompts.py` | `SYSTEM_PROMPT`, `build_user_prompt`/`build_messages`. Reads the **raw** event log (not `infra.eventlog.to_annotation_exchanges`'s export, which drops `question`/`intent`/`allowed_branches`/`instructions`). Condition-blind: the memory condition name is never read back out into the prompt text — only already-resolved integers/structural facts are (see below). |
| `schema.py` | `ANNOTATION_API_SCHEMA` — the shape actually sent to the model, and `to_storage_shape` — converts the model's response into the dict-keyed shape in `schemas/annotation_behaviour.schema.json`. See "Why two schemas" below. |
| `pipeline.py` | `annotate_game(log_path, case, llm)` — one game, one call. Writes `<game>.behaviour.json` (validated) and `<game>.annotation_call.json` (the full raw call record, for audit/replay). Validates the result twice: `validate_storage_shape` (JSON shape only) and `validate_against_transcript` (is it actually true of this game's real transcript — see below). |
| `orchestrate.py` | `annotate_run(run_dir, case_path, make_llm, sample_size=None, balanced=True, source_dir=None)` — many games. Resumable: reads/writes its own `annotation_results.jsonl` in the run directory, mirroring `infra.orchestrate.Run`'s shape and conventions deliberately. `source_dir` lets the gameplay run's `results.jsonl` live somewhere other than `run_dir` (defaults to `run_dir`); a missing `results.jsonl` raises loudly rather than being silently treated as zero games. |
| `__main__.py` | The CLI above. Supports `--backend mock/gemini/openrouter`; `openrouter` auto-prefixes a bare model id (`gemini-3.1-flash-lite`) with `google/` to match OpenRouter's catalog naming. `--source-dir` exposes `orchestrate.annotate_run`'s parameter of the same name. |

## Why the prompt hands the model pre-computed answers for several fields

Several fields in `prompts.py` are not left for the model to derive, even though the
model has enough information to do so in principle — each one is backed by a real,
measured failure when it *was* left to the model:

- **`memory_k(events)`** resolves the effective memory window (1 for `condition="none"`,
  else the stored `k`) rather than passing the raw stored value through — `game_start.k`
  is a single run-level config value shared across all three conditions, so passing it
  raw is actively wrong for the no-memory condition specifically.
- **`candidate_repeat_distances(events, k)`** hands over `suggested_repeat_kind` per
  candidate repeat, already compared against the memory window — asking the model to do
  that comparison itself, even with both numbers given directly, measured at only
  23.6%–72% correct depending on the window size. With the pre-computed suggestion, a
  full 300-game run scored **100.0% correct (2000/2000)**.
- **`checkpoints_reached_count(events)`** counts the real `checkpoint_start` events and
  hands the number over directly — this field was previously pure guessing (the word
  "checkpoint" never appeared in the prompt at all).
- **`missed_disclosure_candidates(events, case)`** gives the model a candidate list of
  leads whose claim never appeared in the controller's bookkeeping AND whose owner or a
  cue_speaker actually appears somewhere in the transcript (a genuine disclosure
  opportunity existed) — a starting point for `missed_required_disclosures`, not a
  verified answer, since the controller's own bookkeeping has known false positives and
  negatives. A lead whose owner and cue_speakers never appear at all is deliberately
  excluded here: a speaker never being questioned establishes undiscovered information,
  not a disclosure violation. See **`undiscovered_leads(events, case)`** below.
- **`undiscovered_leads(events, case)`** is the complementary, purely structural fact
  (same pattern as `checkpoints_reached_count`): which required leads never had their
  owner or any cue_speaker appear anywhere in the transcript at all. Never sent to the
  model — computed directly and merged into the stored output in `pipeline.py`. Fixes a
  real, severe bug: an earlier version of this split (then one function,
  `unflagged_leads`) told the model to treat "nobody who could deliver this ever spoke"
  as near-default evidence FOR a `missed_required_disclosures` violation, the opposite
  of correct. On the real 300-game `main_01` run, 183 of 232 `missed_required_disclosures`
  labels in the `none` condition were exactly this non-violation.
- **`candidate_coordination_proposals(events)`** surfaces which private conversations
  have a flagged proposal in the controller's bookkeeping, as a cross-check (not a
  filter) for `voluntary_interaction.coordination_instances` — known to miss some real
  proposals, so the model is told explicitly to read every private conversation itself
  regardless.
- **`_render_detective_conversation`'s `"kind"` value** is deliberately
  `"voluntary_disclosure_to_detective"`, not the canonical internal event kind
  (`detective_conversation`) — these entries were the only ones ever silently dropped
  from the model's output, traced to the word "conversation" colliding with the separate
  "Suspect-only private conversations" section that genuinely doesn't need individual
  entries.

The prompt also includes a **full claims/facts/world-events catalogue**
(`case["claims"]`/`case["facts"]`/`case["events"]`) resolving every `claim_id`/`fact_id`
referenced elsewhere in the prompt (`approved_false_claim_ids`, `counter_evidence`, each
lead's `claim_id`) to its actual speaker/value/text — not left as a bare, opaque ID. The
model cannot judge `deception.contains_false_statement`, `deception.authorised`, or
`unsupported_additions` from an ID alone.

## Validating against the real transcript, not just the JSON shape

`pipeline.py`'s `validate_against_transcript` checks the annotator's response against
the actual game it describes — something `validate_storage_shape` (JSON schema only)
cannot do at all. It rejects: missing/extra/empty `exchange_behaviour` coverage; a
citation whose text is not an exact substring of anything the cited exchange actually
said; a citation whose `start`/`end` doesn't locate its own text; and any dangling
`claim_id`/`lead_id`/`exchange_id`/`conversation_id`/suspect-name reference. A failure
raises `AnnotationValidationError`, caught by `orchestrate.annotate_one` the same way a
crashed call is (logged, retried on the next run, never silently written to disk).

This exists because an independent review of the real 300-game run found 51 exchange
citations that were not exact substrings at all, and roughly 3,700 more whose offsets
didn't locate their own text — none of which `validate_storage_shape` could reject,
since it only checks shape. See `main_01/README.md`'s "Citation and disclosure repair"
for how the existing 300-game output was repaired after the fact (the live check above
prevents this for every future run).

## Why two schemas

`schemas/annotation_behaviour.schema.json` keys `exchange_behaviour` by exchange ID
(`e1`, `e2`, ...) — natural for storage and for merging against Aniroodh's overlay by
shared keys. But `infra.llm.check_shape` (the function that validates the wrapper's
bounded format repair, and the literal object sent to Gemini's structured output) has
no support for "every value in this object matches schema X" on a dynamically-keyed
object — only named `properties`. That's a real constraint discovered while building
this, not a hypothetical one (there's a test for it:
`test_api_schema_has_no_dynamic_keyed_objects`).

The project's own existing fix for this (see `detective/prompts.py`'s `LEDGER_SCHEMA`)
is to ask for an **array** of self-identifying objects instead. `ANNOTATION_API_SCHEMA`
follows that same convention: the model returns `{"exchanges": [{"exchange_id": "e1", ...}, ...]}`,
and `to_storage_shape` folds that into the dict-keyed form afterward, which the
`jsonschema` library (not `check_shape`) can validate in full, dynamic keys included.

## The 300-game run

`main_01/` (inside this directory) holds the complete, validated annotation output for
all 300 games (100 per condition, `cases/evaluation.json`, `google/gemini-3.1-flash-lite`):
`*.behaviour.json` per game, `*.annotation_call.json` per game (full audit trail),
`annotation_results.jsonl` (per-game status/cost), `annotation_aggregate.json` (the
per-condition summary). See `main_01/README.md` for the full results table, and its
"Citation and disclosure repair" section for how this output was repaired after a PR
review found real citation and disclosure-labelling bugs in it (both now fixed in
`prompts.py`/`pipeline.py` for every future run).

`annotation_aggregate.json` cannot be regenerated from scratch via
`qa/aggregate_annotation_results.py --source-dir`: that script needs `results.jsonl`
(the gameplay-outcome log, condition/solve-rate/attempts per game), which was removed in
a later cleanup pass along with the raw game logs. It was instead regenerated in place
by `qa/repair_main_01_citations.py`, which carries over the (unaffected) gameplay-sourced
fields from the pre-repair aggregate and recomputes every annotation-sourced field fresh.

`ANNOTATION_MAX_OUTPUT_TOKENS` (in `pipeline.py`) is pilot-measured at 16000, large
enough for every game in the 300-game run — no truncation failures.

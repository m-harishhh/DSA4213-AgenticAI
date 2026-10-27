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
python -m annotation summary annotation/main_01
```

## Files

| File | Purpose |
|---|---|
| `prompts.py` | `SYSTEM_PROMPT`, `build_user_prompt`/`build_messages`. Reads the **raw** event log (not `infra.eventlog.to_annotation_exchanges`'s export, which drops `question`/`intent`/`allowed_branches`/`instructions`). Condition-blind: the memory condition name is never read back out into the prompt text — only already-resolved integers/structural facts are (see below). |
| `schema.py` | `ANNOTATION_API_SCHEMA` — the shape actually sent to the model, and `to_storage_shape` — converts the model's response into the dict-keyed shape in `schemas/annotation_behaviour.schema.json`. See "Why two schemas" below. |
| `pipeline.py` | `annotate_game(log_path, case, llm)` — one game, one call. Writes `<game>.behaviour.json` (validated) and `<game>.annotation_call.json` (the full raw call record, for audit/replay). |
| `orchestrate.py` | `annotate_run(run_dir, case_path, make_llm, sample_size=None, balanced=True)` — many games. Resumable: reads/writes its own `annotation_results.jsonl` in the run directory, mirroring `infra.orchestrate.Run`'s shape and conventions deliberately. |
| `__main__.py` | The CLI above. Supports `--backend mock/gemini/openrouter`; `openrouter` auto-prefixes a bare model id (`gemini-3.1-flash-lite`) with `google/` to match OpenRouter's catalog naming. |

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
- **`unflagged_leads(events, case)`** gives the model a candidate list of leads whose
  claim never appeared in the controller's bookkeeping anywhere in the game, each with
  `re_check_exchange_ids` (the specific exchanges to re-read) and an explicit
  `owner_or_cue_speaker_never_spoke` boolean — a starting point for
  `missed_required_disclosures`, not a verified answer, since the controller's own
  bookkeeping has known false positives and negatives.
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
per-condition summary). See `main_01/README.md` for the full results table.

`annotation_aggregate.json` is a frozen snapshot, not regenerable:
`qa/aggregate_annotation_results.py` needs `results.jsonl` (the gameplay-outcome log,
condition/solve-rate/attempts per game) to recompute it, and that file was removed in a
later cleanup pass along with the raw game logs.

`ANNOTATION_MAX_OUTPUT_TOKENS` (in `pipeline.py`) is pilot-measured at 16000, large
enough for every game in the 300-game run — no truncation failures.

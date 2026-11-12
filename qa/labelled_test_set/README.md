# Manually labelled test set — all three conditions

Three real `gemini-3.1-flash-lite` games, one per memory condition (`none`, `ledger`,
`raw`), labelled by hand against `qa/annotation_rubric.md`: `pilot_07b_none_1118654058`,
`pilot_07b_ledger_444382084`, `pilot_07b_raw_1569954369`. Every citation in the label
files is an exact substring of real model output, checked exchange-by-exchange against
the raw transcript — nothing here is invented or paraphrased from memory.

This ground truth was then used to check the real LLM annotator's accuracy and fix nine
real bugs in `annotation/prompts.py` before trusting it to label the full 300-game
study — the evidence for each fix is documented inline in `annotation/prompts.py`'s
docstrings and `tests/test_annotation_pipeline.py`'s test comments.

## What's here

- `pilot07b_{none,ledger,raw}_overlay.json` / `_behaviour.json` — the hand-checked
  labels themselves: claim catalogue overlay and full behavioural annotation
  (`schemas/annotation_behaviour.schema.json`) for each of the three games.
- `pilot_07b_*_worksheet.md` — the same three games' labels in a human-readable,
  field-by-field worksheet format, covering all 62 detective-visible exchanges plus
  whole-game judgements (missed disclosures, voluntary coordination, checkpoints,
  free-text notes).
- `annotator_accuracy_report.json` — the final per-exchange disagreement log from
  checking the real annotator against this ground truth.
- `build_review_worksheet.py`, `check_annotator_accuracy.py`, `fill_worksheets.py` —
  the scripts used to build and validate the above.


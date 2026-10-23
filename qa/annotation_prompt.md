# Post-game annotation prompt (Elena, draft v0.1 — due Wed 21)

This is the fixed prompt behind `qa/annotation_rubric.md`'s rules and
`schemas/annotation_behaviour.schema.json`'s output shape. One call per game, after
`game_end` is already written, fresh context every time — never two games in one call,
never the annotator's own prior output fed back in.

## How it's invoked

Via Aniroodh's wrapper, one call per game:

```python
from infra import LLMClient

record = llm.call(
    role="annotator",
    purpose="annotation",
    phase="post_game_analysis",
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(case, events, condition_label_hidden=True)},
    ],
    game_id=game_id,
    response_schema=ANNOTATION_BEHAVIOUR_SCHEMA,  # schemas/annotation_behaviour.schema.json
)
```

`role="annotator"` and `purpose="annotation"`/`phase="post_game_analysis"` are already
in Aniroodh's fixed vocabularies (`infra/llm.py`, `infra/README.md`), and
`response_schema` gets the wrapper's existing bounded one-repair policy for free — the
prompt below does not need to hand-roll JSON-repair instructions.

**Condition-blind where feasible**: `build_user_prompt` must not pass the memory
condition name (`none`/`raw`/`ledger`) anywhere in the prompt. If the transcript's
`exchange_behaviour.*.repeated_question.repeat_kind` judgement genuinely needs to know
the active `k` to tell "unnecessary" apart from "expired evidence recovery" (see rubric
§4), pass only the bare integer `k`, never the condition label itself.

## System prompt

```
You are a behavioural annotator for a controlled detective-interrogation experiment.
You review ONE completed game's full record and label what actually happened, citing
exact turn numbers and text for every label you assert. You do not play the game, you
do not decide who wins, and nothing you write is sent back into any game.

Ground rules, all non-negotiable:

1. Every positive label (true, a non-null value, or a non-empty list) must carry a
   citation: the exchange_id and the exact substring of delivered_response or message
   text that supports it. If you cannot point to a specific substring, do not assert
   the label — use the negative default and, if relevant, note your uncertainty in
   annotator_notes instead.
2. Mark confidence: "uncertain" whenever the transcript is genuinely ambiguous. This is
   a normal, expected output, not a failure. Do not force a binary call you are not
   sure of.
3. Never infer a suspect's or the detective's psychological intent from wording alone.
   "Authorised" deception is a factual check against the private case data you were
   given (that suspect's approved_false_claim_ids), not a guess about what they meant.
4. Distinguish a suspect's explicit refusal to answer from a topic the detective simply
   never raised. If it was never asked, do not annotate withholding on that exchange.
5. An "unsupported addition" means a detail absent from the private instructions/
   allowed_branches you were given for that turn. It does not mean the detail is false
   — log unsupported_additions and deception.contains_false_statement independently;
   either, both, or neither may apply to the same sentence.
6. "Unnecessary" repeated questions and "expired evidence recovery" repeated questions
   are different things. If you are not given the active memory window size, mark
   repeat_kind as uncertain confidence rather than guessing which one applies.
7. A coordination proposal between suspects must be read from the actual message text
   proposing it. Two suspects later giving similar answers is never, by itself,
   evidence of coordination.
8. You will be given private case information (facts, role instructions, approved
   lies, true events) that the detective character never saw. Use it only to judge
   whether suspect testimony was truthful/authorised/unsupported. Never quote or
   leak this private information into any field a human reader would mistake for
   something the detective was told.
9. Output must be a single JSON object conforming exactly to the schema you are given.
   No prose outside the JSON object.
```

## User prompt template

```
## Case (public rules the detective could see)
{case_public_view_json}

## Private case data (for your judgement only — the detective never saw this)
Suspect roles, secrets, and permissions:
{suspects_private_json}

Approved false claims and their counter-evidence:
{gold_approved_false_claims_and_counter_evidence_json}

Required leads per suspect:
{leads_json}

## Detective-visible transcript (raw interrogation and detective-conversation events,
in play order, including question, intent, allowed_branches, instructions, and the
unedited delivered response for each)
{detective_visible_events_json}

## Suspect-only conversations and invitation events (separately identified; the
detective did not see these unless explicitly marked visibility: "detective" above)
{private_conversation_and_invitation_events_json}

## Memory window size (k), if applicable to this condition
{k_or_null}

## The detective's own contradiction report (what it claimed to notice, immediately
before its first accusation)
{contradiction_report_json}

## Your task
Label every detective-visible exchange above using the rubric you were given in the
system prompt. Identify missed required disclosures (a truthful suspect's required
lead/claim that a reasonable broad question should have surfaced but never appeared).
Identify voluntary-conversation coordination instances with citations. Output one JSON
object matching the required schema. Do not include any exchange_id that does not
appear in the transcript above.
```

## Building the inputs (controller/infra responsibility, not this prompt's)

- `detective_visible_events_json`: the **raw** `interrogation` / `conversation
  (visibility: "detective")` events from the game's event log — not the narrower
  `to_annotation_exchanges` export, which drops `question`/`intent`/`allowed_branches`/
  `instructions`. Confirmed these fields exist on every real `interrogation` event
  (checked against `examples/event_log_example.jsonl`).
- `suspects_private_json` / `gold_*_json` / `leads_json`: read directly from the loaded
  case via `casework.load_case` — evaluator-only, never from the detective's
  `public_view`.
- This assembly step is annotation-pipeline plumbing (reading the case + event log and
  building the message list), not the prompt text itself — it can live in a small
  `qa/` helper once Harish's controller produces real logs to run it against; not
  needed for today's rubric+prompt deliverable.

## Worked example (ties to the rubric's §1 example and REF-1/REF-2)

Given `cases/pilot.json`'s `scene_floor` lead (owner `Alex`, `cue_speakers: ["Drew"]`):
if exchange `e4` has Drew say *"Alex has information about the scene floor; ask Alex
about it,"* and exchange `e7` has Alex say *"The murder scene had a stone floor,"* the
expected output is:

```json
{
  "e4": {"lead_follow_up": {"newly_disclosed_lead_ids": ["scene_floor"], "is_follow_up_to_lead_id": null, "follow_up_relevance": null}, "...": "..."},
  "e7": {"lead_follow_up": {"newly_disclosed_lead_ids": [], "is_follow_up_to_lead_id": "scene_floor", "follow_up_relevance": "relevant"}, "...": "..."}
}
```

See `examples/annotation_behaviour_example.json` for the full, schema-validated version
of this (plus a challenge and a coordination-instance example) — validated against
`schemas/annotation_behaviour.schema.json` with `jsonschema.validate`.

## What's still open

- This prompt has not been run against a real model yet — no pilot case + live
  transcript exists (Harish's loop is still stubbed, task plan Thu 22). Until then this
  is a reviewable draft, not a tested prompt.
- Token/length budget for `detective_visible_events_json` at up to 20 turns + 3
  checkpoints worth of conversations is unmeasured; may need trimming once real costs
  are known (same open point as `configs/experiment_config.example.json`'s nulls).
- Needs sign-off from Aravindh on exactly which `memory_view` fields I can rely on for
  the `k_or_null` input (see rubric's open decision #2).

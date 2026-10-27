# Post-game annotation prompt

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
§4), pass only an integer, never the condition label itself -- but it must be the
*effective* window, not the bare stored `game_start.k`: `game_start.k` is one run-level
config value shared across all three conditions (e.g. `k=8` is stored even on
`condition="none"` games), so passing it through unconditionally is wrong for the
no-memory condition, whose real working window is 1. `annotation/prompts.py`'s
`memory_k()` resolves this the same way `casework.diagnostics.eligible_exchanges` does
(window=1 when condition is "none", else `k`) -- confirmed as a real bug via a live
annotator run against `pilot_07b` (2026-10-25): passing the raw `k=8` caused the
annotator to mislabel nearly every real repeat in a `condition=none` game as
"unnecessary" instead of "expired_evidence_recovery". See
`qa/labelled_test_set/annotator_accuracy_report.json`.

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
   are different things, already decided by the arithmetic in the candidate repeat
   distances lookup below -- do NOT recompute this comparison yourself; use its
   suggested_repeat_kind value exactly once you decide is_repeat is true. Your only job
   is the judgment the lookup cannot do: confirm the two asks are really the same in
   substance. Added 2026-10-26 after a real 300-game run showed the model's own
   distance-vs-window arithmetic was only 23.6% correct under a window of 1 (and ~71%
   under a window of 8) even with both numbers given directly -- see
   `qa/aggregate_annotation_results.py`'s built-in QA check. If you are not given the
   active memory window size, mark repeat_kind as uncertain confidence rather than
   guessing which one applies.
7. A coordination proposal between suspects must be read from the actual message text
   proposing it. Two suspects later giving similar answers is never, by itself,
   evidence of coordination. Use the candidate coordination proposals lookup below as a
   cross-check, not a filter: it is known to miss some real proposals (especially in the
   first private conversation of the game), so read every private conversation yourself
   regardless of whether its id appears there.
8. You will be given private case information (facts, role instructions, approved
   lies, true events) that the detective character never saw. Use it only to judge
   whether suspect testimony was truthful/authorised/unsupported. Never quote or
   leak this private information into any field a human reader would mistake for
   something the detective was told.
9. Output must be a single JSON object conforming exactly to the schema you are given.
   No prose outside the JSON object. Every entry in the detective-visible transcript
   needs its own exchange_behaviour entry, regardless of its "kind" value -- including
   every "voluntary_disclosure_to_detective" entry, which is NOT the same category as
   the separate suspect-only private conversations shown later (those do not get
   individual entries). Added 2026-10-25 after confirming a real, recurring bug: the
   model silently dropped these specific entries (and only these) from its output on
   multiple real games, most plausibly because the word "conversation" collided with
   the separate "no individual entry needed" private-conversation section -- renaming
   the kind value and calling out the distinction explicitly is the fix; see
   `qa/labelled_test_set/annotator_accuracy_report.json`.
10. follow_up_relevance must be null whenever is_follow_up_to_lead_id is null. Only set
    follow_up_relevance when this exchange is itself the pursuit of a specific,
    previously disclosed lead (i.e. is_follow_up_to_lead_id names that lead). Do not
    default it to "no_opportunity" or any other value just because this exchange is not
    a follow-up -- in that case BOTH fields stay null, the same negative-default
    convention every other category in this schema already uses. Added 2026-10-25 after
    a real annotation run showed the model filling in follow_up_relevance on most
    exchanges regardless of whether is_follow_up_to_lead_id was set -- see
    `qa/labelled_test_set/annotator_accuracy_report.json`.
11. For missed_required_disclosures, start from the candidate unflagged leads lookup
    below, but treat every entry as a hypothesis, not an answer: for each one, search
    the actual transcript yourself for a paraphrase that delivers it before including it
    in your output, and separately consider whether a lead absent from that lookup was
    genuinely delivered or only cued/referred to without its content ever being stated.
    A candidate with owner_or_cue_speaker_never_spoke=true should almost always end up
    in your missed_required_disclosures output -- that flag means nobody who could have
    delivered it ever appears in the transcript at all, which is the strongest possible
    evidence of a genuine miss, not a reason to leave it out.
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

## Detective-visible transcript (every entry needs its own exchange_behaviour entry --
"interrogation" and "voluntary_disclosure_to_detective" entries alike, in play order,
including question/intent/allowed_branches/instructions or message/reply, and the
unedited delivered response, for each)
{detective_visible_events_json}

## Suspect-only private conversations and invitation events (NOT detective-visible --
these do NOT get individual exchange_behaviour entries; used only for
voluntary_interaction.coordination_instances)
{private_conversation_and_invitation_events_json}

## Memory window size (k), if applicable to this condition
{k_or_null}

## Candidate repeat distances (mechanical lookup -- suggested_repeat_kind is ALREADY
computed correctly; your only job is confirming is_repeat, not recomputing the kind)
For each exchange_id below, the same speaker was asked a structurally identical question
(same intent fields) exactly exchanges_since_then exchanges earlier, at the given prior
exchange_id. suggested_repeat_kind is the comparison against the memory window above,
already done for you -- IF you confirm this is really a repeat (same ask in substance,
not just a structural-intent coincidence), use suggested_repeat_kind exactly as
repeat_kind; do not recompute or second-guess this arithmetic yourself. This lookup only
matches on the structured intent, which can occasionally be wrong (e.g. a misrouted
intent field creates a false match) -- you must still read both responses and confirm
they are really the same ask in substance before treating this as a repeat at all; if
they are not, this is not a repeat and suggested_repeat_kind does not apply. An
exchange_id absent from this lookup is not necessarily not a repeat, only that no
identical-intent match was found.
{candidate_repeat_distances_json}

## Checkpoints reached (structural fact, not a judgment)
This game had exactly this many checkpoints. Use this number directly for
voluntary_interaction.checkpoints_reached -- do not estimate or guess your own count.
{checkpoints_reached_count}

## Candidate unflagged leads (mechanical lookup, NOT a judgment -- verify before using)
Each entry's claim never appeared in the controller's own bookkeeping anywhere in this
game. Treat this ONLY as a starting point for missed_required_disclosures: exact-text
bookkeeping misses paraphrases, so a lead listed here may actually have been delivered
in different wording -- re_check_exchange_ids names every exchange where the lead's
owner or a cue_speaker actually spoke; re-read exactly those exchanges yourself for a
paraphrase before including this lead in your output.
IMPORTANT: if owner_or_cue_speaker_never_spoke is true, that means nobody who could have
delivered this lead ever appears anywhere in the detective-visible transcript -- this is
STRONG EVIDENCE the lead genuinely belongs in missed_required_disclosures, not a reason
to skip it. Do not treat an empty re_check_exchange_ids list as "nothing to flag here";
treat owner_or_cue_speaker_never_spoke=true as a near-default inclusion.
A lead NOT listed in this lookup at all is still not guaranteed to have been delivered,
since this lookup can also be fooled by a mere cue/referral to the lead rather than an
actual delivery of its content.
{unflagged_leads_json}

## Candidate coordination proposals (mechanical lookup, NOT a judgment -- verify before using)
These private conversation_ids have a flagged proposal in the controller's own
bookkeeping. This is known to be incomplete -- the very first private conversation at
checkpoint 1 commonly contains a real proposal this lookup misses entirely. Read every
private conversation in the section above yourself; do not limit
voluntary_interaction.coordination_instances to only the ids listed here, and do not
assume an id listed here is definitely a real proposal either.
{candidate_coordination_proposals_json}

## The detective's own contradiction report (what it claimed to notice, immediately
before its first accusation)
{contradiction_report_json}

## Your task
Label EVERY exchange_id in the detective-visible transcript above using the rubric you
were given in the system prompt -- both "interrogation" and
"voluntary_disclosure_to_detective" kinds, with no exceptions; before finalising your
answer, count the exchange_ids in the detective-visible transcript section and confirm
your "exchanges" array has exactly that many entries, same ids, same count. Identify
missed required disclosures (a truthful suspect's required lead/claim that a reasonable
broad question should have surfaced but never appeared). Identify voluntary-conversation
coordination instances with citations. Output one JSON object matching the required
schema. Do not include any exchange_id that does not appear in the transcript above.
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
- `candidate_repeat_distances_json` (`annotation.prompts.candidate_repeat_distances`):
  added 2026-10-25 after a real annotation run against `pilot_07b` showed the model
  reliably identifies *that* something is a repeat (`is_repeat` ~95% accurate) but was
  unreliable at the arithmetic step of comparing exchange distance against the memory
  window to pick `repeat_kind` -- even after `memory_k()` was fixed to pass the correct
  effective window, it kept defaulting to `"unnecessary"` on exchanges the gold labels
  marked `"expired_evidence_recovery"`. Pre-computing the distance in code (same
  same-speaker-same-intent-dict technique `build_review_worksheet.py`'s mechanical hints
  already use) hands the model the number instead of asking it to count through a long
  transcript itself, leaving only the comparison and the semantic verification (is this
  really the same ask?) for the model to do. See
  `qa/labelled_test_set/annotator_accuracy_report.json` for the before/after numbers.
  Upgraded again 2026-10-26, after the real 300-game `main_01` run exposed that handing
  over the distance and the window separately was still not enough: the model's own
  `distance > window` comparison was only 23.6% correct under a window of 1, and ~71%
  correct under a window of 8, consistently biased toward "unnecessary" by several turns
  more than the stated rule allows -- confirmed directly against real event logs via
  `qa/aggregate_annotation_results.py`'s built-in QA check, not inferred. Now each
  candidate also carries `suggested_repeat_kind`, the comparison already computed, so
  the model's only remaining job is confirming `is_repeat` (separately validated
  reliable at ~95-97% throughout) -- the same "stop asking for arithmetic, hand over the
  answer" fix already applied to `checkpoints_reached_count`.
- `checkpoints_reached_count` (`annotation.prompts.checkpoints_reached_count`): added
  2026-10-25 after discovering `voluntary_interaction.checkpoints_reached` was pure
  guessing -- the word "checkpoint" never appeared anywhere in a real built prompt, so
  the model had zero data to work from (observed guesses of 1, 3, and 15 across samples
  against a true value of 3 every time). Counts the real `checkpoint_start` events and
  hands the number over directly, since it's a structural fact, not a judgment call --
  same treatment as `memory_k()`.
- `unflagged_leads_json` (`annotation.prompts.unflagged_leads`): added 2026-10-25 after
  `missed_required_disclosures` was wrong in all 9 real samples taken against
  `pilot_07b`. Checks which leads' claims never appear in controller bookkeeping
  anywhere in the game, deliberately checking claim delivery only (never
  `lead_ids`, which can mark a lead as "covered" from a mere cue/referral with no
  claim actually delivered -- confirmed as a real false-"covered" case for
  `pilot_07b_raw_1569954369`'s `meeting_Casey`). Same caveat as the repeat-distance
  lookup: this is a starting point, not an answer -- bookkeeping also misses paraphrases
  (confirmed false "covered" case for `pilot_07b_ledger_444382084`'s `meeting_Alex`), so
  the model must still verify against the real transcript either way. Upgraded
  2026-10-25 to include `re_check_exchange_ids` per candidate (every exchange where the
  lead's owner/cue_speaker actually spoke) after confirming the model copied the
  `meeting_Alex` false-"covered" verdict verbatim in all 3 samples taken rather than
  checking the transcript as instructed -- pointing at the exact exchange to re-read
  narrows the verification task instead of asking the model to re-search the whole
  transcript on faith. Upgraded again the same day to add an explicit
  `owner_or_cue_speaker_never_spoke` boolean: leaving "nobody who could deliver this ever
  spoke" as merely an empty `re_check_exchange_ids` list caused the model to read
  emptiness as "nothing to flag" and silently drop two genuinely missing leads in
  `pilot_07b_none_1118654058` (the opposite of the intended signal) -- an explicit
  boolean removes that ambiguity instead of relying on the model to infer the right
  reading of an empty list.
- `candidate_coordination_proposals_json` (`annotation.prompts.candidate_coordination_proposals`):
  added 2026-10-25 alongside an audit that found the *manual ground truth itself* had
  missed a real coordination instance (`pilot_07b_none_1118654058`'s `p5`) -- caught by
  cross-checking the controller's own `proposal_claim_ids` bookkeeping field, which
  turned out to be a useful (if incomplete) signal. Known gap: it consistently misses
  the first private conversation's proposal across all three real `pilot_07b` games
  checked, so the model is told explicitly not to limit itself to this list.
- `_render_detective_conversation`'s `"kind"` value: deliberately NOT the canonical
  internal event kind `"detective_conversation"` (see `infra.eventlog.EXCHANGE_KINDS`)
  in what gets sent to the model -- renamed to `"voluntary_disclosure_to_detective"`
  2026-10-25 after validating against two held-out games never used to build or tune any
  of the fixes above: the SAME three exchange ids got silently dropped from the model's
  output as in the very first round of this investigation, and in every occurrence
  checked across the whole project, the dropped entries were always exactly the
  detective-visible conversation entries, never interrogations. Working theory: the
  word "conversation" collided with the separate "Suspect-only private conversations"
  section, which genuinely does not get individual entries, and the model generalised
  that rule to these as well. Renaming the label (and adding an explicit
  `needs_its_own_exchange_behaviour_entry` boolean to both entry kinds, for symmetry)
  removes the textual collision instead of asking the model to keep two
  same-named-in-English concepts apart. This is purely a prompt-input relabelling --
  the canonical `"detective_conversation"` kind used everywhere else in the codebase
  (`casework.diagnostics`, `detective.memory`, etc.) is untouched.
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
That file is a synthetic fixture, not actual model output: an illustrative exchange set for the
lead-follow-up / referral walkthrough using `cases/pilot.json`'s `scene_floor` lead (owner Alex,
cue_speaker Drew). Its explanatory note lives here rather than in the JSON, because the schema
does not allow extra keys.


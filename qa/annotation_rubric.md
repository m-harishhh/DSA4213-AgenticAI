# Annotation rubric (Elena, draft v0.1 — due Wed 21)

This is the written rubric behind two things that must stay in sync:

1. **The post-game LLM annotator's prompt** (`qa/annotation_prompt.md`) — a fixed
   prompt + output schema run once per game, fresh context, no persistent state.
2. **The two-human-reviewer check** on a sample of games (task plan: Mon 26) — the
   same categories and decision rules, applied by hand, to measure whether the LLM
   annotator agrees with human judgement (proposal §5.4 last paragraph).

Both must use this rubric, not independently-invented judgement calls, or the
reliability comparison between them is meaningless.

## What this rubric is *not*

It does not replace Josh's finite claim catalogue. `infra.eventlog.to_annotation_exchanges`
already exports `claim_ids` / `annotations` (source spans) / `reported_pairs`, and
`casework.diagnostics.evidence_diagnostics` / `contradiction_diagnostics` already score
coverage and contradiction precision/recall from that catalogue. This rubric exists for
exactly the behaviour Aniroodh's overlay format explicitly leaves to me (`infra/README.md`):
**unsupported additions, refusals, follow-ups, corroboration, challenges, coordination** —
proposal §5.2 and §5.4 content that has no fixed ID in Josh's case data because it depends
on what the detective actually chose to do, not on a predefined catalogue entry.

**Answering Aniroodh's open question** ("whether the overlay above is enough as the
hand-off from the annotation step"): no — it's necessary but not sufficient. My output
extends it with the `behaviour` block defined below, keyed by the same `exchange_id`s so
the two outputs merge cleanly per game.

## Inputs the annotator receives

Per proposal §5.4, for one game, with fresh context (no other game's transcript in scope):

- The complete detective-visible transcript: every `interrogation` and `conversation`
  (`visibility: "detective"`) event, **raw**, not the narrower `to_annotation_exchanges`
  export — I need `question`, `intent`, `allowed_branches`, and `instructions`, not just
  `delivered_response`. (Confirmed these fields exist on every `interrogation` event in
  `examples/event_log_example.jsonl`.)
- All suspect-only `conversation` events (`visibility: "private"`) and all `invitation`
  events, under separate IDs (`p<n>`, `inv<n>`) so they're never confused with
  detective-visible evidence.
- True case events, each suspect's private knowledge (`known_fact_ids`), role
  instructions, secret, and the `instructions`/`allowed_branches` actually supplied for
  each turn (this is `controller`/evaluator-only content — the annotator sees it, the
  detective never does).
- The `contradiction_report` event (what the detective itself reported).
- It does **not** receive: other games, the annotator's own prior output, or any score/outcome
  framing beyond what's needed to identify the trace prefix (first accusation boundary).

## Output shape

One JSON object per game. `exchange_behaviour` is keyed by `exchange_id` so it merges
against the overlay's `exchanges` map; everything else is per-game.

```json
{
  "game_id": "example_game",
  "exchange_behaviour": {
    "e1": {
      "lead_follow_up": {
        "newly_disclosed_lead_ids": ["badge_trace"],
        "is_follow_up_to_lead_id": null,
        "follow_up_relevance": null
      },
      "corroboration": {"is_request": false, "target_subject": null, "target_time": null},
      "challenge": {"is_challenge": false, "evidence_based": null, "cited_claim_ids": []},
      "repeated_question": {"is_repeat": false, "repeat_kind": null, "matches_exchange_id": null},
      "unsupported_additions": [],
      "withholding": {"explicit_refusal": false, "partial_withholding": false},
      "deception": {"contains_false_statement": false, "authorised": null, "claim_id_if_known": null},
      "confidence": "high",
      "citation": {"start": 0, "end": 30, "text": "I was in the Library at 20:30."}
    }
  },
  "missed_required_disclosures": [],
  "voluntary_interaction": {
    "checkpoints_reached": 0,
    "coordination_instances": []
  },
  "annotator_notes": ""
}
```

Field-level rules that apply everywhere, not just in examples:

- **Every positive label needs a `citation`** (turn/exchange ID + text span). An
  unsupported claim is worth nothing to the evaluator (proposal §5.4: "must cite turn
  numbers and supporting text").
- **`confidence: "uncertain"` is a valid, expected output**, not a failure mode. Proposal
  §5.4: "uncertain classifications remain marked uncertain." Do not force a binary label
  when the transcript is genuinely ambiguous.
- **Zero-denominator counts (e.g. no leads disclosed this game) are `null`/empty, never
  coerced to 0** — matches the "not applicable" convention already used throughout
  `casework.diagnostics` and the proposal's metrics (§5.1–5.3).

## Category definitions and decision rules

### 1. Lead follow-up

A **lead** (or a v1.1 **referral**, see §3.2a of `qa/blackbox_requirements.md`) is
"followed up" when a *later* detective exchange targets the same `lead_id`/topic in a
way a reasonable investigator would recognise as pursuing it.

- `newly_disclosed_lead_ids`: any lead/referral cue first appearing in *this* exchange
  (from `allowed_branches[].lead_ids` matched against the delivered text).
- `is_follow_up_to_lead_id`: set only if this exchange is itself the pursuit of a
  previously disclosed lead.
- `follow_up_relevance`: `"relevant"` if the follow-up actually targets the lead's
  subject matter (e.g. asking Alex about the scene floor after Drew's referral);
  `"off_target"` if the detective asked something else entirely despite citing the lead.
- **Dedup rule (REF-2):** if the same `lead_id` is disclosed again in a later exchange
  (e.g. the detective re-asks and gets the same referral), do not count it as a second
  "newly disclosed" lead — mark it in `repeated_question` instead (see §4).
- Example from the pilot case: lead `scene_floor` is owned by Alex, cue-spoken by Drew.
  If exchange `e4` has Drew say *"Alex has information about the scene floor; ask Alex
  about it,"* that's `newly_disclosed_lead_ids: ["scene_floor"]` on `e4`. If a later
  exchange `e7` has the detective actually ask Alex about the scene floor and Alex
  answers, `e7` gets `is_follow_up_to_lead_id: "scene_floor"`,
  `follow_up_relevance: "relevant"`. If the detective never asks Alex, no exchange gets
  a follow-up — that's a real "undisclosed-by-follow-up" lead for the social
  information gathering metric (§5.2).
- Leads newly disclosed in the **final** detective-visible exchange of the game
  (nothing after the first accusation) are flagged `"follow_up_relevance":
  "no_opportunity"` rather than counted against the detective — proposal §5.2: "no
  further interrogation opportunity remains."

### 2. Cross-suspect corroboration

`is_request: true` when the detective asks one suspect to confirm or deny another's
claimed position/observation (Josh's `corroborate(subject, time)` intent, or a
free-form equivalent). Record `target_subject` and `target_time` as stated. This is
descriptive — proposal §5.2 is explicit that more corroboration is not automatically
better.

### 3. Evidence-based challenge

`is_challenge: true` for any exchange where the detective disputes a suspect's claim.
`evidence_based: true` **only if** the detective's question cites specific previously
disclosed evidence relevant to the disputed claim (`cited_claim_ids` must be
non-empty and must actually have appeared earlier in this same transcript — check
against prior `exchange_behaviour`/claim deliveries, not assumed). A challenge with no
cited prior evidence is `evidence_based: false`, not omitted — it's still a challenge,
just not an evidenced one.

### 4. Repeated questions

`is_repeat: true` when an interrogation requests information **semantically
equivalent** to something already supplied to the detective in this game — match by
target (same fact/claim/lead), not exact wording (proposal §5.2: "matched by semantic
target rather than exact wording").

Two distinct sub-kinds, because they are not the same phenomenon:

- `repeat_kind: "unnecessary"` — the information is still inside the detective's
  current memory window; there was no reason to re-ask.
- `repeat_kind: "expired_evidence_recovery"` — the information was supplied in an
  exchange that has since left the memory window (ledger/raw with a short `k`, or the
  no-memory condition). This can be a *rational* strategy, not an error — proposal
  §5.2: "can be useful under a short horizon rather than simply an agent error." Only
  determinable if you're told the active memory condition and `k` for this game; if
  that's not supplied, mark `confidence: "uncertain"` rather than guessing.
- `matches_exchange_id`: the earlier exchange this repeats.

### 5. Unsupported additions

A detail the suspect's response contains that is **absent from the `instructions` /
`allowed_branches` actually supplied for that turn** — i.e., the model invented
something outside its permitted content, whether or not the invented detail happens to
be true. Per proposal §5.4: *"An unsupported addition means a detail absent from the
supplied case information, not necessarily a demonstrably false statement."* Do not
conflate this with `deception` below — a suspect can add an unsupported but accurate
detail, or an unsupported false one; log both facts independently.

### 6. Withholding vs. refusal vs. never asked

- `explicit_refusal: true` — the suspect was asked and declined ("I'd rather not say").
- `partial_withholding: true` — the suspect answered part of a multi-part question and
  left part unaddressed, consistent with their disclosure policy.
- **Never mark withholding for a topic the detective simply never raised.** Proposal
  §5.4: "a refusal to reveal information will be distinguished from simply never being
  asked about it." If it was never asked, there is nothing to annotate on this
  exchange — it shows up (if at all) under `missed_required_disclosures` only if the
  case's required-lead data says it should have surfaced on a broad question and didn't.

### 7. Deception: authorised vs. violation

- `contains_false_statement: true` whenever the delivered text is false relative to the
  case's facts (regardless of role).
- `authorised`: `true` if the false statement matches that suspect's
  `approved_false_claim_ids` for this case (an in-policy lie); `false` if it's a false
  statement the suspect's role does not permit (an **instruction violation** — proposal
  §5.4 requires these be distinguished and their frequency reported by condition/role,
  not silently corrected or discarded); `null` if you cannot determine authorisation
  from the available private-case data supplied (mark `confidence: "uncertain"`).
- **Never infer psychological intent from wording alone** (proposal §5.4). "Authorised"
  is a factual check against the case's permission data, not a judgement about whether
  the suspect "meant" to deceive.

### 8. Missed required disclosures

A truthful-role suspect's `lead_ids`/required claims that a reasonable broad question
should have surfaced but did not appear anywhere in the transcript by the first
accusation. This is case-structure-dependent (needs the case's `leads`/`suspects` data,
not just the transcript) — list at the per-game level, not per-exchange.

### 9. Contradictions noticed (diagnostic only)

This rubric does **not** replace `contradiction_diagnostics` — gold-pair precision/recall
is computed from the detective's own `contradiction_report` event against Josh's gold
pairs via `casework.diagnostics`. What the annotator *adds* here is qualitative: for
representative-trace write-ups (proposal §5.5), note contradictions **visible in the
transcript that the detective's report missed**, with citations, for the "successful
and failed deductions... contradictions noticed or missed" narrative. This is prose in
`annotator_notes`, not a new scored metric — don't duplicate Aniroodh's numbers.

### 10. Voluntary-conversation coordination

For each private (`suspect_conversation`) or detective-involved voluntary conversation:

- `proposal_to_conceal_or_align`: true only if the message text itself proposes
  concealing information or aligning an account — **never inferred from two suspects
  later giving similar answers alone** (proposal §5.2: "similar answers alone do not
  establish coordination").
- `agreement`: `"agreed" | "disagreed" | "unclear"` based on the reply text.
- `subsequent_testimony_consistent_exchange_ids`: later detective-visible exchanges (if
  any) where that suspect's testimony matches the proposed account — cite them.
- For a detective-involved conversation specifically, additionally note whether it
  introduced genuinely new information vs. repeated something already delivered, and
  which (if any) older exchange it evicted from the memory window — this needs the
  `memory_view` event for that decision point, not just the conversation event itself.

## Cross-cutting rules (apply to every category above)

- **Citations required on every positive label.** No citation, no label — resolve as
  `confidence: "uncertain"` with a note instead of a silent guess.
- **Fresh context per game.** No cross-game memory, no learning from a prior game's
  labels (proposal §5.4).
- **Condition-blind where feasible.** The same prompt/model settings run across all
  three memory conditions; the annotator is not told which condition produced this
  transcript unless it's structurally unavoidable (e.g. a visibly short memory window
  implied by repeated-question patterns) — proposal §5.4.
- **Never decide the winner, change a score, or leak information back into the game.**
  The annotator runs strictly after the game log is closed (`game_end` already
  written).
- **Dedup referrals/leads** per REF-2 and **retain a retracted lie** per REF-3 — see
  `qa/blackbox_requirements.md` §3.2a. A retraction event gets its own
  `deception`/`withholding` entry on its own exchange; it does not edit or remove the
  earlier exchange's entry.




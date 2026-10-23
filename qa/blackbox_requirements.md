# Black-Box Requirements List

This is the acceptance contract for the integrated game: every externally observable
behaviour the proposal commits to, written as a testable assertion. It defines what
"done" means for integration and is the specification the cross-module black-box test
suite is written against.

**This file is a checklist, not test code.** 

Source section numbers refer to the final proposal.

**Frozen configuration.** `decisions.md` and `controller/interfaces.py` fix the
following values: `TURN_BUDGET = 20`, `MEMORY_K = 8` (not swept over {3,5,10} as the
proposal originally proposed — see decision D6), `ACCUSATION_BUDGET = 3`, checkpoints
after interrogations `(5, 10, 15)`, `INVITATION_PASSES = 2`,
`MAX_CONVERSATIONS_PER_CHECKPOINT = 3`, `MAX_DETECTIVE_CONVERSATIONS_PER_CHECKPOINT = 1`,
`MAX_CONVERSATIONS_PER_PARTICIPANT = 1`, `DETECTIVE_FORMAT_REPAIRS = 1`. Both cases'
`design` blocks carry `turn_budget: 20, memory_k: 8` too, so case data and the frozen
interface agree. `configs/experiment_config.example.json` still has `model_id`,
`decoding_settings`, `budget_usd`, `annotation_sample_size`, `prompt_versions` as
`null` — those remain genuinely open until the pilot.

## 3.1 Environment and public assumptions

| ID | Requirement |
|---|---|
| ENV-1 | Exactly 5 suspects, 5 weapons, 5 locations are presented as public candidate domains; solution space size is 5×5×5=125. |
| ENV-2 | Detective-visible public state contains candidate names/weapons/locations, case description, and role-count rules (1 culprit, 2 self-protective, 2 truthful) — and nothing else from the private case. |
| ENV-3 | Detective is never given role assignments, private evidence, hidden disclosure targets, or the solution, at any point before its accusation. |
| ENV-4 | Detective does not observe a voluntary conversation it is not a participant in. |

## 3.2 Evidence and case validation

| ID | Requirement |
|---|---|
| VAL-1 | A case admitted to pilot/evaluation passes: factual consistency, attribution/knowledge boundaries per suspect, counter-evidence for every approved false claim, unique solvability, and a budget-feasible disclosure path. |
| VAL-2 | The solver enumerates candidates without ever being given the actual culprit label or true role assignments. |
| VAL-3 | No single innocent's initial private evidence alone determines the full solution. |
| VAL-4 | Every response branch along the certified questioning strategy leaves a budget-feasible continuation within the configured turn budget. |

(VAL-1 through VAL-4 are already implemented as `casework.validation.validate_case` /
`casework.certificate` — confirm with `python -m casework validate cases/pilot.json` and
`cases/evaluation.json` rather than re-deriving them.)

## 3.2a Referral contract (schema 1.1)

Case schema 1.1 separates a lead's `owner` (who holds the evidence) from its
`cue_speakers` (who may tell the detective the lead exists). Asking a cue-speaker
only delivers the referral, not the underlying evidence; the detective still has to
separately ask the `owner`. `casework/README.md` explicitly assigns REF-2 and REF-3 to
the annotation layer.

| ID | Requirement |
|---|---|
| REF-1 | Asking a lead's `cue_speaker` records only the referral as delivered; it never marks the `owner`'s evidence fact as disclosed. Confirmed in case data: lead `scene_floor` has `owner: "Alex"` but `cue_speakers: ["Drew"]`. |
| REF-2 | A repeated disclosure of the same actionable lead/referral (same lead id, multiple exchanges) is deduplicated, not double-counted, in coverage/follow-up metrics. |
| REF-3 | An innocent's time-bound cover lie (e.g. `claim_cover_Blair`, 20:30) and their separate true alibi (e.g. `claim_alibi_Blair`, 21:00) are tracked as distinct claims; a retraction of the lie does not erase it from the record — both the original lie and the retraction remain visible. |
| REF-4 | The ledger retains the referral's speaker, target, and topic with source attribution, but never imports the private lead catalogue, and drops an unresolved lead once its source exchange expires the memory window. |

## 3.3 Suspect behaviour and disclosure

| ID | Requirement |
|---|---|
| SUS-1 | Truthful-role suspects never fabricate a timeline, falsely deny a meeting, or claim ignorance of a fact in their known-fact set. |
| SUS-2 | Self-protective suspects' false claims are confined to their approved cover topic/time only — no lying about other suspects, weapons, or locations. |
| SUS-3 | The culprit's false claims are confined to its approved cover story; it follows the predefined challenge rule (maintain/retract) once relevant counter-evidence is presented. |
| SUS-4 | Suspect responses are delivered to the detective unfiltered — no regeneration or correction even when a response violates role instructions. |
| SUS-5 | Semantically equivalent detective requests map to the same fixed intent (location inquiry, corroboration, challenge, etc.) — interpretation is request-driven, not solution- or memory-condition-driven. |
| SUS-6 | Private evidence and disclosure state for a given suspect are identical across all three memory conditions on the same case; only the detective's memory view changes. |

## 3.4 Voluntary communication and suspect memory

| ID | Requirement |
|---|---|
| CKPT-1 | Checkpoints are offered only immediately after completed interrogation 5, 10, and 15, and only if further information gathering remains possible before an accusation. |
| CKPT-2 | Each eligible participant (5 suspects + detective) completes at most 1 conversation per checkpoint; a given checkpoint has at most 3 completed conversations, at most 1 involving the detective. |
| CKPT-3 | A rejected invitation consumes neither participant's slot; up to 2 invitation passes per checkpoint occur. |
| CKPT-4 | Invitation and accept/reject messages carry identity only — no substantive content is exchanged through them. |
| CKPT-5 | An accepted conversation is exactly one bounded message from the inviter and one reply from the recipient. |
| CKPT-6 | Suspect-to-suspect messages are never visible to the detective or other suspects; a detective-involved conversation is visible only to that suspect, the detective, and the evaluator. |
| CKPT-7 | A completed detective-initiated conversation enters detective memory as one full exchange immediately, and can evict the oldest exchange in a size-k window. |
| CKPT-8 | Suspect-only conversations never enter detective memory under any condition. |
| CKPT-9 | Voluntary conversations consume zero interrogation turns but are still present in API cost logs. |
| CKPT-10 | All voluntary-conversation activity stops at the first accusation; an early-ending game may have fewer than 3 checkpoints observed. |
| CKPT-11 | The controller's invitation proposal order is derived from a recorded random seed; re-running with the same seed reproduces the same proposal order. |

## 3.5 Interaction protocol and limits

| ID | Requirement |
|---|---|
| TURN-1 | Off-topic requests, repeated questions, refusals, and evasive answers each still consume one information-gathering turn. |
| TURN-2 | The detective can question suspects unevenly (no per-suspect quota) within the shared turn budget. |
| TURN-3 | The game enforces the frozen turn budget B and at most 3 distinct accusation attempts. |
| TURN-4 | Accusation feedback indicates only whether the full triple (culprit, weapon, location) is correct — no partial/component-level hint is ever returned. |
| TURN-5 | After the first accusation, no further interrogation or voluntary-conversation evidence gathering occurs; testimony is frozen. |
| TURN-6 | A malformed/invalid detective action gets exactly one bounded formatting-repair attempt; persistent failure is recorded as a failed run, not retried indefinitely. |
| TURN-7 | Accusation attempts after rejection use a distinct triple and receive no previous-reasoning or component hints beyond prior triples + correctness. |

## 4.1–4.2 System design, memory conditions, information boundaries

| ID | Requirement |
|---|---|
| SYS-1 | Accusations are submitted via a structured schema (culprit/weapon/location drawn from the public candidate lists) — scoring never parses free-form text. |
| SYS-2 | Every suspect/detective state resets at the start of each game; nothing carries over between games (dialogue, deductions, accusation feedback). |
| SYS-3 | **No-persistent-memory** condition: detective input contains only the current exchange `I_t` plus common public state — nothing from earlier exchanges. |
| SYS-4 | **Rolling raw history** condition: detective input is the most recent `min(k,t)` exchanges verbatim, with speaker and exchange ID, in original order. |
| SYS-5 | **Structured ledger** condition: detective input is extracted only from the same `H_t` window as raw history (matched source horizon) — never a longer or different window. |
| SYS-6 | The ledger contains no inferred truth labels, automatic supporting/conflicting links, guilt scores, or inferred candidate solutions. |
| SYS-7 | Ledger extraction for a given exchange is performed once, independently of other exchanges, and sees only that exchange's delivered text (not controller ground truth). |
| SYS-8 | A ledger extraction schema failure gets one fixed repair attempt; persistent failure produces an explicit extraction-failure entry (not silent data loss) and counts as an error. |
| SYS-9 | Prompt context overflow is explicitly recorded for any condition, never silently truncated. |
| SYS-10 | Common public state given to the detective never includes prior question lists, suspect-role labels, hidden fact IDs, inferred candidate sets, or summaries of expired evidence. |

## 4.3 Experimental controls

| ID | Requirement |
|---|---|
| CTRL-1 | All three memory conditions run from an identical restored initial state (hidden solution, private evidence, secrets, disclosure rules, coordination permissions) for a given case. |
| CTRL-2 | Model version, role-specific prompts, decoding settings, output limits, and turn/accusation budgets are identical across conditions — only the memory representation differs. |
| CTRL-3 | Condition execution order is randomised across the run set. |

## 5.1 Primary and retry outcomes

| ID | Requirement |
|---|---|
| MET-1 | First-accusation exact accuracy (A1) requires culprit, weapon, and location to all match; a missing valid first accusation counts as failure. |
| MET-2 | Cumulative solve rates A2/A3 are computed after at most 2/3 attempts; A3 success never changes the recorded A1 value for that run. |
| MET-3 | Undefined ratios (zero denominators) are reported as not-applicable with their denominator shown, never silently as 0. |

## 5.2 Interaction and efficiency measures

| ID | Requirement |
|---|---|
| MET-4 | A challenge is only counted as evidence-based if it cites previously disclosed evidence relevant to the disputed claim. |
| MET-5 | Checkpoint reporting includes invitation senders/recipients, accept/reject rates with denominators, and completed partner choices, by condition. |
| MET-6 | Coordination claims (concealment/alignment between suspects) are only reported when citing the actual relevant messages — similarity of answers alone is never sufficient. |
| MET-7 | "Unnecessary repeated question" is matched by semantic target, not exact wording. |
| MET-8 | Evidence coverage uses a fixed per-case denominator and deduplicates by internal identifier; approved false claims are tracked separately from true-evidence coverage. |
| MET-9 | Communication cost logs cover every call type (detective, suspect, invitation, accept/reject, conversation, interpretation, extraction, repair, annotation) and separate cost through first accusation from retry cost from post-game analysis cost. |

## 5.3 Contradiction detection

| ID | Requirement |
|---|---|
| CONT-1 | The contradiction report is collected once, at a fixed diagnostic point immediately before the first accusation, under the same output schema for every condition. |
| CONT-2 | The contradiction report is never fed back into the detective's persistent memory. |
| CONT-3 | A gold pair counts as "observable" only if both claims occur in that condition's own eligible source window at that point (`H_t` for raw/ledger, `I_t` for no-memory) — an extraction omission in the ledger counts against the ledger, it does not shrink the denominator. |
| CONT-4 | Precision/recall are computed as `|P∩G|/|P|` and `|P∩G|/|G|`; zero-denominator cases are reported as not-applicable. |

## 5.4 Environment adherence and annotation

| ID | Requirement |
|---|---|
| ADH-1 | Suspect testimony is delivered to the detective even when false, unsupported, contradictory, or improperly withheld — no content-based rejection/regeneration/replacement. |
| ADH-2 | Every scheduled run remains in the primary operational evaluation; none are discarded for poor outcomes or later-discovered adherence violations. |
| ADH-3 | Transport failures get bounded retries; unresolved failures count as unsuccessful runs in primary accuracy and are also reported separately (not conflated with reasoning failures). |
| ADH-4 | The post-game annotator processes one game at a time with fresh context (no cross-game leakage) and receives suspect-only conversations/invitation events under separate identifiers from detective-visible evidence. |
| ADH-5 | The annotator never decides the winner, changes exact-match scores, or feeds information back into a game. |
| ADH-6 | Every annotation cites turn numbers and supporting text; uncertain classifications are explicitly marked uncertain rather than forced into a label. |
| ADH-7 | The same annotation prompt and model settings are used across all three conditions, with condition labels hidden from the annotator where feasible. |
| ADH-8 | A prespecified, equally-sampled-per-condition subset is independently reviewed by two human reviewers against the written rubric, with disagreements and resolution reported. |

## 5.5 Experimental setup and repeated evaluation

| ID | Requirement |
|---|---|
| EXP-1 | Each game starts from fresh agent state; no dialogue, deductions, or accusation feedback carry into a later game. |
| EXP-2 | One base model is used across detective, suspect, and extraction roles, with fixed role-specific prompts, for the duration of main evaluation. |
| EXP-3 | Reported 95% bootstrap CIs resample complete runs within each condition (not individual turns or accusation retries). |
| EXP-4 | The pilot case(s) are distinct from the frozen evaluation case, and the evaluation case is locked before main evaluation without being chosen for an observed ledger advantage. |

## How this gets used

Every item above becomes one `unittest` assertion run against the integrated game
loop: each ID maps to one test function in the cross-module black-box test suite. The
`ADH-4`–`ADH-8` and `REF-2`/`REF-3` items are checked by the annotation pipeline and
rubric (`qa/annotation_rubric.md`) rather than the controller integration tests; the
rest require the live game (case validation items can already be checked today against
the offline `casework` module).


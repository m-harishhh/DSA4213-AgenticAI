# DSA4213 Project Group 10: Detective Memory Experiment - Casework

## Note: The purpose of this README is to provide instructions for the rest of you to continue from my part.
## This README, in its entirety, should not form part of the final README.

This repository now includes **Josh's Case & Validation module**. It supplies two
event-model cases, disclosure rules, gold labels, a candidate solver, a branching
solve-path certificate, and offline tests. It uses Python's standard library only.

The controller, LLM wrapper, detective prompts/memories, full experiment runner,
and post-game LLM annotator remain the other owners' modules. This is a tested
case-and-validation handoff, not a completed live multi-agent game.

## Quick start

Use Python **3.10 or newer** (tested on **3.12**). Run all commands and
Python snippets below from the repository root, `DSA4213-AgenticAI/`, even though
this README is inside `casework/`. All code-path examples and table paths are
relative to that root. No separate compilation step is required.

Use your existing compatible Python environment, then run
`python -m pip install -r requirements.txt` from the root. The current requirements
file has no third-party dependencies, so this installs nothing. See the
[root README](../README.md) for the repository layout and setup instructions.

```bash
python -m unittest discover -s tests -v
python -m casework validate cases/pilot.json
python -m casework validate cases/evaluation.json
python examples/integration_example.py
```

The last command uses a clearly labelled mock response. It makes no API calls.
The validation command returns exit code 0 on success and 1 on a failed case.

To save a fresh private validation report:

```bash
python -m casework validate cases/pilot.json --turn-budget 20 --output validation_reports/pilot.json
python -m casework validate cases/evaluation.json --turn-budget 20 --output validation_reports/evaluation.json
```

Expected for both supplied cases: **one unique solution, 108 successful terminal
branches, zero failed branches, and a 14-turn worst case for the certified
strategy**. These counts describe the designed response tree, not 108 LLM games.
The certificate is intentionally conservative; it is not a shortest-path proof.

## Files and ownership

| File | Purpose |
|---|---|
| `cases/pilot.json` | Separate case for prompt/interface/debugging work |
| `cases/evaluation.json` | Certified evaluation candidate; freeze only after pilot configuration decisions |
| `casework/model.py` | JSON loading, restricted views, initial state, event/proposition helpers |
| `casework/generate.py` | Reproducible construction of these two authored templates |
| `casework/disclosure.py` | Allowed response branches, actual-delivery updates, private-sharing contract |
| `casework/solver.py` | Enumerates triples and hidden role assignments using public rules |
| `casework/certificate.py` | Checks every branch of a discoverable questioning strategy |
| `casework/validation.py` | Admission checks tying the case, evidence and certificate together |
| `casework/diagnostics.py` | Evaluator adapters for evidence coverage, candidate counts and contradictions |
| `schemas/case.schema.json` | Machine-readable JSON shape for editors; semantic checks remain in Python |
| `examples/integration_example.py` | Minimal offline handoff example |
| `examples/annotation_example.json` | Example delivered-text annotation and source citations |
| `tests/test_casework.py` | Positive and deliberately invalid cases, boundary and metric checks |
| `validation_reports/` | Checked certificates with private information; never detective input |
| `configs/experiment_config.example.json` | Settings to agree after the pilot; unknown choices are null |
| `archive/game_initial_exploration.py` | Historical exploration, predating the final proposal; not imported by this module |

The old exploration script remains unchanged. Its ten-suspect setup and simple
contradiction heuristic do not define this implementation.

## How this implements Josh's allocation

| Responsibility | Implementation |
|---|---|
| Pilot and evaluation event-model JSON | `cases/`, generated reproducibly from explicit events |
| Gold evidence, approved lies, counter-evidence, contradictions | `facts`, `claims`, and `gold` sections of each case |
| Knowledge boundaries | Per-suspect known fact IDs backed by observation events; restricted views |
| Disclosure rules and permitted branches | `allowed_content` and discoverable lead records |
| Candidate-enumeration validator | `solve(public_case, observations)` |
| Certified solve path | Exhaustive traversal of the finite strategy in `certify` |
| Case schema and reference function | JSON schema plus `allowed_content` interface |
| Consistency, solvability, budget tests | `validate_case` and the test suite |

Budget-feasible disclosure is part of admission, even though the task plan labels
an additional disclosure test Low. A case that fails the certificate does not pass
the default validator.

## The implemented mystery, in plain language

Five museum staff members have seen different cropped recordings or forensic
notes after a murder. Their assigned badge shapes/uniform colours, possible
weapons' properties, and rooms' properties are public. The initial instructions
say uniforms/badges were not exchanged and that each described trace refers to
the actual attacker, weapon or crime scene. This makes the deduction rules explicit.

Six main clues concern:

1. The attacker's badge shape.
2. The attacker's sleeve colour.
3. The murder weapon's material.
4. The injury's edge class.
5. The scene's floor.
6. The scene's wall colour.

In the **pilot**, round badge + blue sleeve identify Alex; steel + sharp identify
Knife; stone floor + green walls identify Library. Each clue is attributed to its
speaker. These are evaluator explanations, not a public answer key.

The **evaluation candidate** has different names, events, culprit, weapon and
room. Both cases share the same deliberately small rule template. They are not
independent samples from a broad distribution of plots. Do not tune prompts on
evaluation answers or choose this case because it gives a ledger advantage.

The culprit and two self-protective innocents each have one approved false alibi,
with a discoverable witness counterclaim. Everyone also knows their own timeline.
Three additional witness facts produce a fixed coverage denominator of **nine**.
The six trace clues suffice for the triple; the other clues support deception,
challenge and contradiction diagnostics. High coverage is therefore not required
for a correct answer.

### Deliberate limits of this first case design

The proposal leaves exact public deduction rules and approved deceptive topics
to case design. This implementation chooses a **narrow public deception rule**:
the culprit and self-protective innocents may lie about their own whereabouts at
the two listed times; other designed claims must be truthful. Their actual private
permission is narrower still: one particular cover account each.

This supports innocent deception without treating every unreliable speaker as
the murderer. It also makes the finite solver tractable and understandable.
However, the essential trace clues cannot be deliberately falsified under this
template. An agent can solve the case without resolving every false alibi. Thus,
the main result will concern memory over distributed, partly distracting testimony;
it will not establish robustness to arbitrary lies about essential clues.

This is a design choice to review during the pilot. If the group wants lying about
weapon/scene/attacker traces, extend the public permission model, solver, gold
counter-evidence and tests together before freezing the evaluation. Changing a
prompt alone would invalidate the certificate.

One suspect holds two of the six main traces; others hold one. Additional witness
knowledge also differs. The role-to-output-length/clue-count shortcut audit still
belongs in the pilot. This module does not claim those surface patterns are balanced.

## Facts, claims and delivered text are different

| Object | Meaning | Visibility |
|---|---|---|
| `events` | What actually happened and which record each person observed | Controller/evaluator |
| `facts` | Structured true propositions derived from the events | Relevant suspect and evaluator |
| `claims` | Attributed statements, including approved false statements | Internal catalogue; only delivered text reaches detective |
| `gold` | Evidence denominator, contradiction pairs, counter-evidence | Evaluator only |
| Actual response text | What the suspect model really said, including errors | Delivered unchanged to its participants |

A claim catalogued as available is not automatically disclosed. A prompt asking a
suspect to provide a clue is not evidence that the reply contained it. The model
may omit, distort or add details. Keep the actual response, instructions supplied,
and subsequent annotation separate in the event log.

The finite contradiction definition is two different values for the same exclusive
key. A position key includes both person and time. Different times, uncertainty,
missing details and withholding do not automatically form contradictions.
Retraction does not erase an earlier claim from the trace; resolution can be
annotated separately. Gold pairs concern statements, not proof of guilt.

## What the validator proves

`validate_case` checks:

- Five suspects, five weapons, five rooms; exactly one culprit, two self-protective
  innocents, and two truthful innocents.
- The solution agrees with the murder event and public candidate domains.
- Event positions are consistent, and facts agree with the event model.
- Each initially known fact has an explicit observation record.
- Truthful claims match known facts; each approved false claim is actually false,
  within its owner's permission, and has discoverable counter-evidence.
- Required clue leads are explicitly discoverable and belong to their speakers.
- Coverage labels and contradiction pairs match the finite claim catalogue.
- Public evidence leaves one answer even when role identities are hidden.
- No single innocent's initial true facts, plus knowledge of their own innocence,
  determine the entire triple.
- Every response branch along the supplied strategy has a successful continuation
  within the chosen interrogation budget.

The solver evaluates 125 triples and six possible self-protective-pair assignments
for each triple: at most 750 triple/role combinations. It receives **no stored
solution, actual role assignments, private cover-story list or undisclosed facts**.
For a given role assignment, a permitted own-alibi claim may be true or false;
all other observed claims constrain the candidate world. A contradictory set can
have zero feasible worlds. That is a conditional-rule diagnostic, not a verdict
that the LLM detective itself is irrational.

The certificate asks every suspect for an overview, then follows all explicitly
offered leads. Each overview can truthfully answer or partially withhold; the three
deception-permitted suspects can instead offer their approved cover. This yields
`3 * 3 * 3 * 2 * 2 = 108` terminal branch combinations. There are five overview
questions and nine lead questions: fourteen interrogations.

Certification uses all evidence collected by that strategy. It does **not** prove
that a detective with a short rolling window can retain enough evidence, that an
LLM will choose the strategy, or that every natural-language interpretation works.
The certificate is never supplied as detective instructions.

Voluntary conversations can share claims or propose an approved cover, but cannot
change events, create new permitted lies, or remove a required original clue.
The required clue response is independent of received-chat state. Consequently,
the certified strategy remains available under the designed communication choices.
This is an invariant of this template, not an exhaustive test of Harish's scheduler
or of unrestricted coordination generated by an LLM.

The checks validate structured propositions. They do not independently understand
whether edited natural-language text faithfully expresses its proposition. Human
review of clue wording and the interpreter remains necessary during the pilot.

## Handoffs and interface examples

### Harish: allowed content and suspect state

```python
from casework import load_case, public_view, initial_state, allowed_content
from casework.disclosure import branch_prompt

case = load_case("cases/pilot.json")
state = initial_state(case, "Blair")
branches = allowed_content(case, "Blair", {"kind": "overview"}, state)
generation_options = [branch_prompt(case, branch) for branch in branches]
```

The suspect model chooses among allowed alternatives. Do not always select the
false branch for a self-protective innocent. A controller-selected branch is fine
only in mocks or the explicitly scripted certificate. Pass private motive/role
instructions through `suspect_view`, and maintain the suspect's full participated
dialogue in your own pipeline.

The finite intents are:

| Intent dict | Behaviour |
|---|---|
| `{"kind": "overview"}` | Useful explicit leads, with truthful/partial/approved-cover alternatives |
| `{"kind": "ask_lead", "topic": "badge_trace"}` | Required disclosure by that lead's owner |
| `{"kind": "whereabouts", "time": "20:30"}` | Own alibi, with permitted withholding/cover branches |
| `{"kind": "corroborate", "subject": "Blair", "time": "20:30"}` | Known observation of that person's whereabouts |
| `{"kind": "challenge", "claim_id": "claim_cover_Blair"}` | Maintain or retract according to relevant cited evidence |

These IDs are an internal interpretation contract. The detective asks free-form
questions; it should not see hidden claim IDs or a private list of undisclosed
topics. Resolve a question against public information and actually delivered cues.
For a challenge, resolve cited source text to IDs; pass only actually cited,
detective-visible counterclaims in `cited_claim_ids`. The interpreter must not use
the hidden answer or the detective memory condition.

`record_delivery(...)` updates finite state from matched **actual** claims, leads
and retractions. It never assumes a selected branch was delivered. It deliberately
does not erase raw responses that cannot be mapped. Online mapping of explicit
lead/retraction state is controller bookkeeping, not the post-game judging agent.
Full semantic metrics are computed later from checked annotations.

`communication_options(...)` provides direct claims, attributed relays, approved
cover proposals and refusal/acknowledgement options for either participant.
`share_information(...)` adds only communicated claims as attributed testimony;
it never copies a partner's entire private evidence or makes a received lie true.
Agreement with a proposal does not grant permission to lie about someone else's
whereabouts. Agreement/disagreement and later adherence need transcript annotation.

There is no record-request action: the proposal mentions one without defining it.
All essential information here is obtained through suspect dialogue. Record review
is an event explaining suspects' initial knowledge, not an extra player tool.

### Aravindh: detective and memory

Use `public_view(case)` plus actual visible exchanges. Do not import `solution`,
`gold`, `events`, suspect state, or the certificate into detective prompt building.
Public views reject unexpected fields rather than silently accepting a solution.

The three memory implementations remain yours. For raw and ledger, use the same
latest `k` completed detective-visible exchanges. A voluntary detective conversation
counts as an exchange and can evict an interrogation. Private conversations and
invitations do not enter memory. No-persistent-memory means latest exchange only.

Extract the ledger independently per delivered exchange and retain explicit source
spans, speaker, time, uncertainty, refusal and lead wording. This package does not
give the ledger gold truth labels, contradiction links or solver output.

### Aniroodh and Elena: evaluation adapters

`examples/annotation_example.json` demonstrates the adapter input. Use reviewed
annotations of actual text, with source spans and exchange IDs. IDs in these
annotations stay evaluator-only. Repeated statements can reuse a canonical claim
ID while keeping separate exchange occurrences.

```python
from casework.diagnostics import evidence_diagnostics, contradiction_diagnostics

# Pass the trace PREFIX ending just before the first accusation.
coverage = evidence_diagnostics(case, exchanges, condition="raw", k=5)
conflicts = contradiction_diagnostics(case, exchanges, reported_pairs,
                                    condition="ledger", k=5)
```

Coverage deduplicates relevant true facts, tracks approved false claims separately,
and uses the fixed case denominator. Candidate counts use actual matched source
claims across the full observed history and the current eligible window.

Contradiction precision/recall require source citations. Gold observability uses
source dialogue, including when the ledger omitted one of the claims. Missing
denominators return `None` (JSON `null`), not zero. Suspect-only pairs are excluded
unless both relevant claims later reach the detective. Predictions are deduplicated
by canonical claim pair; invalid source citations are reported separately.

These adapters cover the finite catalogue. Unanticipated falsehoods, paraphrases,
new contradictions, missing disclosures and psychological-intent uncertainties
require Elena's separate semantic annotation. Never silently omit unsupported text
from the transcript. Never infer cooperation from similar answers alone.

Aniroodh still owns primary exact first-accusation accuracy, component accuracies,
retry success, false-accusation rate, turn counts, all-role token costs, failure
accounting, bootstraps and figures. Elena owns the lead-follow-up/repetition/
corroboration/challenge rubric, annotation prompt and two-reviewer reliability.
This module supplies definitions and source mappings rather than replacing those
components with a second independent metric system.

## Integration acceptance checklist

Before calling the integrated game complete, test these proposal requirements in
Harish/Elena's end-to-end suite (they are outside Josh's offline module):

- Reset every suspect and detective state between games; keep role policies fixed.
- Preserve suspect responses unchanged even when they violate instructions.
- Off-topic, evasive, repeated and refused interrogations consume a turn.
- Run checkpoints after completed interrogations 5, 10 and 15 only when further
  gathering is possible; use seeded invitation priority, up to two passes,
  identity-only invitation/decision messages and at most one completed conversation
  per participant. At most three conversations and one with detective per checkpoint.
- Conversation messages consume API cost but no interrogation turns; use one message
  and one reply without an extra automatic answer to the detective's reply.
- Stop information gathering at the first accusation; freeze testimony; permit at
  most three distinct complete triples with only whole-triple correctness feedback.
- Keep repair/transport failures and all scheduled runs in operational accounting.
- Test context-length overflow explicitly rather than truncating one condition.
- Include extraction overhead, invitation/decision calls, retries and annotation in
  cost logs, with cost through first accusation separated from retry/analysis cost.

## Pilot, freezing and budget

The evaluation JSON is a **candidate**, not a claim that main evaluation has begun.
Use only the pilot for wording/interpretation/cost tuning. Agree one model version,
prompts, decoding settings, `k`, turn budget, repeat count and annotation allocation.
Do not change those after looking at main-condition results.

USD15 is no longer enforced as a hard limit. `budget_usd` is deliberately null in
the example configuration so the group can agree a modest increase after measured
pilot costs. Do not guess a provider price or silently increase paid run counts.
This package has no provider dependency and spends **zero API budget**.

After agreement, copy `configs/experiment_config.example.json` to
`configs/experiment_config.final.json`, fill every required
field, and create a reproducibility record:

```bash
python -m casework manifest cases/evaluation.json --config configs/experiment_config.final.json --turn-budget 20 --output validation_reports/evaluation_manifest.json
```

This records a case hash, module-source hashes, selected settings and certificate.
It refuses unfinished settings. It does not modify the case, lock files, execute
games, or make model calls. Preserve the manifest with prompts and run logs. Any
later case/code change needs a new record and a documented decision about affected
runs. Do not call a case frozen merely because its offline certificate passed.

## Course concepts and additional engineering

The conceptual connection is to Lecture 7 (in-context learning and controlled
prompt comparisons) and Lecture 12 (evidence use, attribution, and the gap between
having evidence and using it). No training, fine-tuning, embeddings, vector database,
or retrieval from expired history has been added.

The extra engineering is ordinary JSON, dictionary/list operations, enumeration,
a bounded branching search, unit tests and SHA-256 fingerprints. The finite solver
and certificate are necessary because the proposal explicitly requires logical
and budget-feasible case validation; they do not introduce a new learned model or
give the detective external reasoning assistance.

Design references: final proposal Sections 3.1-3.5, 4.2-4.3 and 5.1-5.5; Task Plan
Josh allocation and cross-owner dependencies; final rubric technical quality,
evaluation and contribution evidence. The supplied implementation deliberately
documents its template restrictions and remaining integrated-game responsibilities.

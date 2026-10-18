# Detective & Memory module

Status (18 Oct): **action schema only**. The detective prompt, LLM calls and the three
memory views come next (task plan: Tue 20 / Wed 21).

## What the action schema does

`detective/action_schema.py` defines everything the detective LLM is allowed to output and
checks it before the controller acts on it.

| Call | Detective outputs | Proposal |
|---|---|---|
| Main decision | `interrogate` (suspect + free-form question + optional `cites`) or `accuse` (culprit, weapon, location) | §3.5, §4.1 |
| Contradiction report | Pairs of `{exchange_id, quote}`, attached to the **first** accusation only | §5.3 |
| Invitation response | `accept: true/false`, nothing else | §3.4 |
| Conversation reply | One message, length-capped | §3.4 |

**The detective never outputs an intent.** It asks a free-form question, and Harish's
interpreter maps it to one of Josh's five intents (`casework/model.py`, see
`examples/integration_example.py`). This follows proposal §3.3. There is no record-request
action (Josh's casework README).

Every `parse_*` function takes a raw dict or JSON string plus a `DecisionContext`, and
returns a plain dict `{ok, action, errors, warnings}`:
- **errors**: the output is invalid. The caller gives one formatting repair, then fails the run (§3.5).
- **warnings**: the output is accepted, and the warnings are logged for analysis.

`DecisionContext` holds only detective-visible information. Build it with
`DecisionContext.from_public(public_view(case), ...)`, never from the private case.

## Design choices

| # | Choice | One-line reason |
|---|---|---|
| 1 | **Optional `cites` field** (exchange IDs) | Lets Harish fill `cited_claim_ids` for Josh's `challenge` intent without guessing from free text. |
| 2 | **Contradiction report rides with the first accusation** | One call instead of two, so it's cheaper; §5.3 only needs it at that point. |
| 3 | **Short rationale allowed, logged only, never fed back** | Gives Elena trace examples without creating a hidden scratchpad (§4.2). |
| 4 | **Case-insensitive name matching** (`"library"` becomes `"Library"`) | Avoids wasting the single repair on capitalisation slips. |
| 5 | **Unsupported contradiction citations are kept and logged, not repaired** | §5.3 scores unsupported detections; repairing would inflate precision. |
| 6 | **Citations to exchanges outside the memory window are dropped and logged** | The detective can't see expired exchanges; a repair would waste a call. |
| 7 | **Pydantic models + plain dicts at the boundaries** | Pydantic does internal checking while plain dicts maintain compatibility with Josh's casework convention. |
| 8 | **Question cap 500 characters** | Starting value; the pilot fixes final limits (§3.5). |
| 9 | **Conversation reply cap 500 characters** | Same: starting value, revisited in the pilot. |
| 10 | **No cap on contradiction pairs** | Doesn't restrict what the detective reports; watch recall-gaming in the pilot. |

## Working defaults and open decisions

| Default | Alternatives |
|---|---|
| Rationale cap = **300** characters | Any number; or `None` for no cap |
| A rationale over the cap is an **error** (uses the repair) | Truncate and warn instead (saves a repair call) |
| A quote "appears" if it is a **case-insensitive substring** of the exchange | Exact match only; or fuzzy match |
| `accept` uses Pydantic's lenient bool (`"true"`, `"yes"` accepted) | Strict: only real `true`/`false` |
| Duplicate `cites` are silently de-duplicated | Warn on duplicates |
| A report sent with a retry accusation is ignored with a warning | Treat as an error |

## Dependency (needs group agreement)

Pydantic is third-party. Before this merges, `requirements.txt` needs a line such as
`pydantic>=2,<3`. Tested with Pydantic 2.13–2.14 on Python 3.10–3.13.

## Running the tests

From the repository root:

```bash
python -m pip install "pydantic>=2,<3"
python -m unittest tests.test_action_schema -v
```

The 18 tests use only the **pilot** case's `public_view`.

## Open interface points

- `decision_json_schema()` produces the output-format schema for the LLM. Whether the provider
  accepts it as-is depends on Aniroodh's wrapper (untested).
- `visible_exchanges` is `{exchange_id: exchange text}` for the current memory window. Its exact
  fields must match Aniroodh's event log schema (due 18 Oct) and Josh's exchange format
  (`exchange_id`, `kind`, `speaker`, `delivered_response`).

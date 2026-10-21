# DSA4213 Project Group 10: Detective Memory Experiment

For a much more detailed README describing the case schema, please refer to the
[casework README](casework/README.md). That README contains instructions on how to run Josh's
casework and contributions, as well as more important information on how each of
your roles and responsibilities, as well as the group's responsibilities as a whole,
are integrated with the case schema. **Please give it a read before starting on
your parts!**

Note to our future selves when creating the root README: The root README should not
contain the ``casework``'s README (as of 18 Oct), in its entirety. That's to say,
it needs substantial re-writing and re-working before integration into the root
README. ~ Josh

## Repository layout and running the code

The current case-and-validation implementation is in `casework/`; its detailed
handoff is in [casework/README.md](casework/README.md). Case data lives in `cases/`,
JSON schemas in `schemas/`, examples in `examples/`, tests in `tests/`, and saved
case certificates in `validation_reports/`. Configuration templates are in
`configs/`. The historical exploration script is in `archive/` and is not the
current game entry point.

Aniroodh's infrastructure is in `infra/`: the LLM wrapper signature (`infra/llm.py`), the
per-game event log plus its validator and export to Josh's annotation format
(`infra/eventlog.py`), the log's JSON schema (`schemas/event_log.schema.json`) and an
offline example (`examples/infra_example.py`). Its handoff is in
[infra/README.md](infra/README.md). Live runs write one log per game under `runs/`,
which is git-ignored because logs contain private case information.

Use Python **3.10 or newer** (casework was done on **3.12**) with your existing 
compatible Python environment. Open a terminal at the repository root, 
`DSA4213-AgenticAI/` (the directory containing this README). These commands work 
from a checkout of `main` containing these files. (Note: `main` as in a Git branch!)
Python runs the code directly, so no separate compilation step is needed.

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m casework validate cases/pilot.json
python -m casework validate cases/evaluation.json
python examples/integration_example.py
python examples/infra_example.py
```

Casework uses Python's standard library; the detective action schema also requires
Pydantic, listed in [requirements.txt](requirements.txt). Add third-party dependencies
there when new modules actually need them.
Do not copy an entire individual-assignment environment into the requirements.
Select the same interpreter in your IDE and terminal (`python -c "import sys; print(sys.executable)"` checks the terminal interpreter).

Start with [configs/experiment_config.example.json](configs/experiment_config.example.json)
for experiment settings. The integrated live game and experiment runner will be
added by the other owners; the commands above test and demonstrate the current
casework and infra modules without API calls.

## Case revision v2: alibis, referrals, and k=8

The pilot and evaluation cases now require independent alibi evidence to distinguish
two suspects with shared physical traits. Each has two cross-suspect referrals,
private motives for optional checkpoint conversations, and ten discoverable evidence
facts. A lie alone does not prove guilt; the existing retraction mechanism remains.

The agreed settings are **20 interrogation turns and k=8** for raw and ledger memory.
No-memory still sees the latest exchange only. Case validation now checks a sufficient
final eight-exchange window even with one uninformative detective conversation at
each checkpoint. All 108 finite branch combinations pass: pilot within 18 turns,
evaluation within 19. This is an offline feasibility guarantee, not measured LLM
performance. The group still needs pilot adherence, interpretation and cost checks.

See [casework/README.md](casework/README.md#referral-contract-schema-11) before integrating:
case schema 1.1 separates `cue_speakers` from the evidence `owner`. Function signatures
remain compatible, but code assuming every cue-giver owns the evidence must change.
Main experiment manifests enforce 8/20. Other branches' controller constants and
memory implementations must adopt these settings when merged; this revision does
not implement the live game. All commands above work from this branch, and from
main once these changes are reviewed and merged.

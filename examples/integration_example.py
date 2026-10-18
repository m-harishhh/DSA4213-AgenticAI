"""Run from repo root: python examples/integration_example.py (no network)."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from casework import load_case, public_view, initial_state, allowed_content, record_delivery
from casework.disclosure import branch_prompt
from casework.diagnostics import evidence_diagnostics

case = load_case(ROOT / "cases" / "pilot.json")
person = case["public"]["suspects"][0]
state = initial_state(case, person)
intent = {"kind": "overview"}  # Harish's interpreter produces this.
branches = allowed_content(case, person, intent, state)

# MOCK ONLY: in a live game the suspect LLM chooses an allowed branch and replies.
selected = branches[0]
mock_claims = selected["claim_ids"]
mock_leads = selected["lead_ids"]
state = record_delivery(case, person, state, mock_claims, mock_leads)
event = {"exchange_id": "exchange_1", "kind": "interrogation",
         "claim_ids": mock_claims, "lead_ids": mock_leads}

print(json.dumps({"public_case_keys": list(public_view(case)),
                  "suspect_generation_instructions": branch_prompt(case, selected),
                  "evaluator_only_mock_event": event,
                  "evaluator_only_diagnostics": evidence_diagnostics(case, [event], "raw", 5)}, indent=2))
print("Live integration must derive annotations from actual delivered text, never copy the intended branch IDs.")

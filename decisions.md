# Design Decisions

## D1. Detective format repair happens in the controller
Date: 19 Oct · Agreed by: Harish, Aravindh
Decision: detective calls pass response_schema=None to LLMClient.call; the controller
runs parse_decision and allows exactly one repair.
Why: otherwise the wrapper and the controller could each repair once (two repairs).

## D2. Contradiction report goes with the first accusation
Decision: It's sent inside the first accusation call (purpose="accusation"), logged as its own contradiction_report event, and contradiction_report is removed from PURPOSES	
why: Saves an LLM call; proposal §5.3 only needs it at the first accusation

## D3. Stable agent IDs	
Decision: agent_id is required on every LLM call: detective, suspect_1..5 (in the case's public suspect order), interpreter, extractor, annotator	
Why: Compare the same agent across games; names differ between the pilot and evaluation cases

## D4. Ledger uses the same k window as raw	
Decision: Ledger entries come only from the latest k exchanges and drop out when their exchange expires	
Why: Proposal §1, §4.2, §6: matched horizon, so a ledger win reflects structure rather than extra memory

## D5. No record-request action	
Decision: Every information-gathering turn is a suspect interrogation	
Why: The proposal (§3.5) mentions it but never defines it; Josh's case gives all evidence through dialogue


## D6. Turn budget and k fixed without a pilot sweep
Decision: turn budget = 20 and k = 8, fixed now. We will not compare 20/30/40 turns or k ∈ {3, 5, 10} as proposed in §3.5 and §4.3.
Why: time. Both are within the proposal's candidate sets and were chosen before seeing any results, so they're not picked for a ledger advantage. Josh's certificate shows the case is solvable in 14 turns, which is within 20. And solvable in 6-7 conversations.
Still set during the pilot: message and character caps, model version, and the cost per game.

## D7. Stubs for Aravindh's functions live in detective/
Decision: detective_decide, build_memory and extract_ledger are defined in detective/agent.py and detective/memory.py; detective/ must never import controller	
Why: Aravindh fills them in place; importing controller from detective would cause a circular import
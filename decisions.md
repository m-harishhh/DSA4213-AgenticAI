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
Why: time. Josh's case v2 certificate proves the final 8 exchanges uniquely identify the solution, even with uninformative checkpoint conversations displacing evidence; worst case 18 (pilot) / 19 (evaluation) turns, within 20. No such guarantee was shown for k = 5. k = 8 is outside the proposal's {3, 5, 10} and was fixed before any results.
Still set during the pilot: message and character caps, model version, and the cost per game.

## D7. Stubs for Aravindh's functions live in detective/
Decision: detective_decide, build_memory and extract_ledger are defined in detective/agent.py and detective/memory.py; detective/ must never import controller	
Why: Aravindh fills them in place; importing controller from detective would cause a circular import


## D8. Model: gemini-3.1-flash-lite, minimal thinking
Date: 24 Oct
Decision: All roles use gemini-3.1-flash-lite with temperature 0.7, top_p 0.95, thinking_level "minimal" (one model across roles, proposal §5.5).
Why: gemini-2.5-flash-lite (the proposal's choice) is refused for new API keys (404 "no longer available to new users", Aniroodh, 20 Oct). In pilot_04b, thinking_level "low" did not raise the solve rate and caused 5/9 failed runs (thinking tokens used up the output limit). In pilot_05 (same code, same seeds), gemini-3.5-flash-lite solved no more games (1/9 vs 2/9 first try), heard fewer clues (1.0 vs 1.8 of 7) and cost about 25% more. Decided on the overall solve rate, not on which condition did best.
Cost: 3.1 Flash-Lite is $0.25/$1.50 per 1M tokens vs the proposal's $0.10/$0.40, so the budget changes (see the final config).

## D9. Detective prompt: zero-shot v3 to one-shot v4
Date: 24 Oct
Decision: The detective prompt adds "how to investigate" steps (overview each suspect, follow every lead, confront conflicting accounts, accuse only when attacker, weapon and room are accounted for) and one short example investigation (detective-v4, #25).
Why: through pilot_05 the detective asked almost only "where were you at 21:00?" and heard 1-2 of the 7 decisive clues, so games were decided by guessing (floor effect). In pilot_06, v4 raised clues heard to 5.4/7 (7/7 in every raw and ledger game); the remaining failures came from clues expiring from the k=8 window, which is the memory effect the experiment measures.
Validity: the prompt is identical in all three conditions; the example uses invented names and items (no real-case names or facts, checked against both cases) and shows actions only, with no memory block, so it favours neither raw nor ledger memory.

## D10. Checkpoint conversations are included
Date: 24 Oct
Decision: Voluntary conversations run after interrogations 5, 10 and 15 (proposal §3.4): seeded invitation order, 2 passes, identity-only invitations and decisions, one message + one reply, at most 3 conversations per checkpoint, 1 per participant, 1 with the detective. Only suspects initiate; the detective accepts or rejects and replies. Suspect-only conversations pass only the claims actually said (share_information) and never reach the detective. A detective conversation becomes one exchange in its memory: no interrogation turn, but it can push the oldest exchange out of the k=8 window. Suspects may propose a cover story to each other but not to the detective.
Why: studying agent interaction is part of the project (proposal §3.4, §6 "planned core"). pilot_07 showed real coordination attempts and refusals between suspects; the rule against cover proposals to the detective followed from pilot_07, where suspects asked the detective to help maintain their lie.
Cost: about $0.01 extra per game (around 40 short calls); about 2 minutes per game.

## D11. Online claim matching is a heuristic
Date: 24 Oct
Decision: match_delivery decides which catalogued claims an answer contained by structured key/value rules: whole words, room and time in one sentence, the person named for claims about someone else, negations and quoted speech excluded, volunteered true claims counted.
Why: it drives disclosure state during play (e.g. a retraction needs delivered counter-evidence), so it must not wait for annotation. Each rule came from a pilot mismatch (pilot_02 to pilot_07). It is bookkeeping only: testimony is never edited, and coverage/behaviour metrics come from the post-game annotation (proposal §5.4).


## D12. Run cost ceiling raised to $20
Date: 25 Oct
Decision: the main run (main_01, seed 2026) uses a cost ceiling of $20 instead of $15 (`--max-cost-usd 20` on resume; configs/experiment_config.final.json updated to match).
Why: the evaluation-case smoke block (main_01, first block of 3 games) cost $0.031 (none), $0.034 (raw) and $0.053 (ledger) per game, projecting about $11.8 for 300 games; ledger games cost more on the evaluation case than in the pilots, so $15 left little headroom. The ceiling is a safety cap only: it changes no game setting, and the orchestrator only stops between blocks, so conditions stay equal.

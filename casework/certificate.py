"""Exhaustively check every response branch of a discoverable finite strategy."""

from collections import Counter
from .model import initial_state, public_view, claims_for_solver
from .disclosure import allowed_content, record_delivery
from .solver import solve, triple_set


def certify(case, turn_budget=20):
    """Try a conservative strategy: overview everyone, then follow every lead.

    This is a feasibility certificate, NOT an optimal policy or the detective's
    instructions. A failure means this strategy failed; it does not prove that
    every other strategy fails. The branch tree is checked without LLM calls.
    """
    if not isinstance(turn_budget, int) or isinstance(turn_budget, bool) or turn_budget < 1:
        raise ValueError("turn_budget must be a positive integer")
    people = case["public"]["suspects"]
    target = tuple(case["solution"][key] for key in ("culprit", "weapon", "location"))
    cache = {}
    leaves, failures, witness_path = [], [], []
    visited = 0

    def visit(states, overviews_left, leads, observed, path):
        nonlocal visited, witness_path
        visited += 1
        if not overviews_left and not leads:
            key = tuple(sorted(set(observed)))
            if key not in cache:
                cache[key] = solve(public_view(case), claims_for_solver(case, key))
            result = cache[key]
            if triple_set(result) != {target}:
                failures.append({"reason": "Strategy did not uniquely identify the stored solution",
                                 "turns": len(path), "candidate_count": result["candidate_count"]})
            else:
                leaves.append(len(path))
                if not witness_path:
                    witness_path = path
            return
        if len(path) >= turn_budget:
            failures.append({"reason": "Strategy exceeds turn budget", "turns": len(path)})
            return
        if overviews_left:
            person = overviews_left[0]
            intent = {"kind": "overview"}
            remaining_overviews, remaining_leads = overviews_left[1:], list(leads)
        else:
            # Lead ownership and topic are obtained from previously delivered cues.
            person, topic = leads[0]
            intent = {"kind": "ask_lead", "topic": topic}
            remaining_overviews, remaining_leads = [], leads[1:]
        for branch in allowed_content(case, person, intent, states[person]):
            new_states = dict(states)
            new_states[person] = record_delivery(case, person, states[person],
                branch["claim_ids"], branch["lead_ids"], branch["retract_claim_ids"])
            new_leads = list(remaining_leads)
            for lid in branch["lead_ids"]:
                entry = (person, lid)
                if entry not in new_leads:
                    new_leads.append(entry)
            step = {"turn": len(path)+1, "suspect": person, "intent": intent,
                    "branch_id": branch["branch_id"], "claim_ids": branch["claim_ids"],
                    "lead_ids": branch["lead_ids"]}
            visit(new_states, remaining_overviews, new_leads,
                  observed + branch["claim_ids"], path + [step])

    visit({p: initial_state(case, p) for p in people}, list(people), [], [], [])
    return {"passed": bool(leaves) and not failures, "turn_budget": turn_budget,
            "strategy": "overview_each_public_suspect_then_follow_all_disclosed_leads",
            "nodes_checked": visited, "successful_terminal_branches": len(leaves),
            "failed_terminal_branches": len(failures),
            "turn_distribution": dict(sorted(Counter(leaves).items())),
            "worst_case_turns": max(leaves) if leaves else None,
            "failures": failures[:10], "example_path": witness_path,
            "scope": [
                "All finite response branches along this strategy, with compliant content delivery.",
                "Optional private conversations cannot remove the required original clue disclosures in this template; their policy is state-independent.",
                "Voluntary conversations cost zero interrogation turns, so they do not affect this feasibility budget.",
                "This does not certify LLM compliance, memory-window retention, free-form interpretation, or every possible detective policy.",
                "Example path and private IDs are evaluator-only; never put this certificate in a detective prompt."]}

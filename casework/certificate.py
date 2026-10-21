"""Evaluator-only feasibility check using finite branches and a bounded source window."""

from collections import Counter
from .model import initial_state, public_view, claims_for_solver, TURN_BUDGET, MEMORY_K
from .disclosure import allowed_content, record_delivery
from .solver import solve, triple_set


def certify(case, turn_budget=TURN_BUDGET, memory_k=MEMORY_K):
    """Overview everyone, follow cues, then recover displaced evidence if necessary.

    The plan uses disclosed cue wording/targets, not hidden guilt labels. All
    finite response branches are checked. An empty detective conversation after
    each checkpoint models the maximum one-exchange displacement with no help.
    This proves source availability, not LLM reasoning or faithful extraction.
    """
    for name, value in (("turn_budget", turn_budget), ("memory_k", memory_k)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    people = case["public"]["suspects"]
    public = public_view(case)
    target = tuple(case["solution"][key] for key in ("culprit", "weapon", "location"))
    cache = {}
    leaves, failures, witness_path = [], [], []
    witness_window = []
    visited = 0

    def candidates(ids):
        key = tuple(sorted(set(ids)))
        if key not in cache:
            cache[key] = triple_set(solve(public, claims_for_solver(case, key)))
        return cache[key]

    def cue_order(entry):
        # Publicly expressed time, not the claim's private truth/role metadata.
        cue = case["leads"][entry[1]]["cue"]
        return (0 if "20:30" in cue else 1 if "21:00" in cue else 2, entry[1])

    def visit(states, overviews_left, leads, observed, path, window, lead_history):
        nonlocal visited, witness_path, witness_window
        visited += 1
        if not overviews_left and not leads:
            window_ids = [cid for exchange in window for cid in exchange["claim_ids"]]
            if candidates(observed) == {target} and candidates(window_ids) == {target}:
                leaves.append(len(path))
                if not witness_path:
                    witness_path, witness_window = path, window
                return
            # Re-ask an explicitly disclosed recent lead displaced by a checkpoint.
            # This uses prior source exchanges only inside this evaluator certificate.
            visible_topics = {exchange.get("topic") for exchange in window}
            recoverable = [entry for entry in lead_history[-memory_k:] if entry[1] not in visible_topics]
            if not recoverable:
                failures.append({"reason": "Strategy cannot retain a sufficient source window",
                                 "turns": len(path), "window_candidate_count": len(candidates(window_ids))})
                return
            leads = [recoverable[0]]
        if len(path) >= turn_budget:
            failures.append({"reason": "Strategy exceeds turn budget", "turns": len(path)})
            return
        if overviews_left:
            person = overviews_left[0]
            intent = {"kind": "overview"}
            remaining_overviews, remaining_leads = overviews_left[1:], list(leads)
        else:
            person, topic = leads[0]
            intent = {"kind": "ask_lead", "topic": topic}
            remaining_overviews, remaining_leads = [], leads[1:]
        for branch in allowed_content(case, person, intent, states[person]):
            new_states = dict(states)
            new_states[person] = record_delivery(case, person, states[person],
                branch["claim_ids"], branch["lead_ids"], branch["retract_claim_ids"])
            new_leads = list(remaining_leads)
            for lid in branch["lead_ids"]:
                entry = (case["leads"][lid]["owner"], lid)
                if entry not in new_leads:
                    new_leads.append(entry)
            if overviews_left and not remaining_overviews:
                new_leads.sort(key=cue_order)
            turn = len(path) + 1
            step = {"turn": turn, "suspect": person, "intent": intent,
                    "branch_id": branch["branch_id"], "claim_ids": branch["claim_ids"],
                    "lead_ids": branch["lead_ids"]}
            exchange = {"kind": "interrogation", "turn": turn,
                        "topic": intent.get("topic"), "claim_ids": branch["claim_ids"]}
            new_window = (window + [exchange])[-memory_k:]
            if turn in (5, 10, 15) and turn < turn_budget:
                new_window = (new_window + [{"kind": "detective_conversation", "turn": turn,
                                             "claim_ids": []}])[-memory_k:]
            new_history = lead_history + ([(person, intent["topic"])] if "topic" in intent else [])
            visit(new_states, remaining_overviews, new_leads,
                  observed + branch["claim_ids"], path + [step], new_window, new_history)

    visit({p: initial_state(case, p) for p in people}, list(people), [], [], [], [], [])
    return {"passed": bool(leaves) and not failures, "turn_budget": turn_budget, "memory_k": memory_k,
            "strategy": "overview_then_time_ordered_referrals_then_recover_displaced_lead",
            "nodes_checked": visited, "successful_terminal_branches": len(leaves),
            "failed_terminal_branches": len(failures),
            "turn_distribution": dict(sorted(Counter(leaves).items())),
            "worst_case_turns": max(leaves) if leaves else None,
            "failures": failures[:10], "example_path": witness_path,
            "example_final_source_window": witness_window,
            "scope": [
                "All finite response branches along this strategy, with compliant content delivery.",
                "One uninformative detective conversation after interrogations 5, 10 and 15 when budget remains; each consumes a memory slot.",
                "Checks uniqueness from both all observed claims and the final latest-k source exchanges.",
                "Optional private communication cannot remove required original clues in this template.",
                "This does not certify LLM choices, compliance, extraction quality, or every questioning policy.",
                "Certificate recovery bookkeeping is evaluator-only, never extra detective memory.",
                "The no-memory condition is not certified to retain a solution; it remains a lower-information baseline."]}

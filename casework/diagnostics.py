"""Evaluator-only adapters for Aniroodh/Elena; no NLP matching or LLM judging.

Input IDs must come from checked annotations of DELIVERED text. Intended prompt
content is not an annotation. Unknown generated claims need separate annotation
records; the finite case gold is not a complete label set for arbitrary output.
"""

from .model import claims_for_solver, public_view
from .solver import solve


def eligible_exchanges(exchanges, condition, k):
    """Source-window selection for evaluation, not a detective memory module."""
    if condition not in ("none", "raw", "ledger"):
        raise ValueError("condition must be none, raw or ledger")
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError("k must be a positive integer")
    ids = [e["exchange_id"] for e in exchanges]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate exchange IDs")
    visible = [e for e in exchanges if e["kind"] in ("interrogation", "detective_conversation")]
    # Call with the trace prefix ending immediately BEFORE the first accusation.
    return visible[-(1 if condition == "none" else k):]


def evidence_diagnostics(case, exchanges, condition="raw", k=5):
    visible = [e for e in exchanges if e["kind"] in ("interrogation", "detective_conversation")]
    window = eligible_exchanges(exchanges, condition, k)
    all_ids = {cid for e in visible for cid in e.get("claim_ids", [])}
    window_ids = {cid for e in window for cid in e.get("claim_ids", [])}
    unknown = all_ids - set(case["claims"])
    if unknown:
        raise ValueError(f"Unmapped claim IDs: {sorted(unknown)}")
    coverage_gold = set(case["gold"]["evidence_ids"])
    disclosed = {case["claims"][cid].get("fact_id") for cid in all_ids} & coverage_gold
    return {"coverage_numerator": len(disclosed), "coverage_denominator": len(coverage_gold),
            "coverage": len(disclosed) / len(coverage_gold) if coverage_gold else None,
            "disclosed_evidence_ids": sorted(disclosed),
            "approved_false_claims_disclosed": sorted(all_ids & set(case["gold"]["approved_false_claim_ids"])),
            "source_window_exchange_ids": [e["exchange_id"] for e in window],
            "all_observed_candidate_count": solve(public_view(case), claims_for_solver(case, sorted(all_ids)))["candidate_count"],
            "window_candidate_count": solve(public_view(case), claims_for_solver(case, sorted(window_ids)))["candidate_count"],
            "conditional_on_intended_rules": True,
            "note": "Candidate counts use only matched finite claims. Unknown claims and policy violations require separate interpretation; counts never prove an actual run was solvable."}


def contradiction_diagnostics(case, exchanges, reported_pairs, condition="raw", k=5,
                              additional_gold_pairs=()):
    """Score case-mapped claim pairs, requiring valid source-exchange citations.

    reported_pairs entries: {left: {exchange_id, claim_id}, right: {...}}.
    All source claims remain eligible even if ledger extraction dropped a claim.
    Caller can supply reviewed additional pairs for known mapped claims. Entirely
    new claims need an expanded annotation vocabulary before using this adapter.
    """
    window = eligible_exchanges(exchanges, condition, k)
    sources = {e["exchange_id"]: set(e.get("claim_ids", [])) for e in window}
    observed = set().union(*sources.values()) if sources else set()
    if observed - set(case["claims"]):
        raise ValueError("Source has unmapped claims; extend checked annotation first")
    gold = {tuple(sorted(pair)) for pair in list(case["gold"]["contradiction_pairs"]) + list(additional_gold_pairs)}
    observable = {pair for pair in gold if set(pair) <= observed}
    predictions, supported = set(), set()
    invalid_citations = []
    for report in reported_pairs:
        left, right = report["left"], report["right"]
        pair = tuple(sorted((left["claim_id"], right["claim_id"])))
        predictions.add(pair)
        valid_sources = all(item["claim_id"] in sources.get(item["exchange_id"], set())
                            for item in (left, right))
        if valid_sources and pair in observable:
            supported.add(pair)
        elif not valid_sources:
            invalid_citations.append(report)
    return {"true_positive_count": len(supported), "reported_pair_count": len(predictions),
            "observable_gold_pair_count": len(observable),
            "precision": len(supported)/len(predictions) if predictions else None,
            "recall": len(supported)/len(observable) if observable else None,
            "unsupported_pair_count": len(predictions - supported),
            "invalid_citations": invalid_citations,
            "observable_gold_pairs": [list(pair) for pair in sorted(observable)]}

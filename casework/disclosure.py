"""Finite content contract. The controller still generates and delivers actual text."""

import copy
from .model import INTENTS


def _branch(branch_id, claim_ids=(), lead_ids=(), withholding=False, retract=()):
    return {"branch_id": branch_id, "claim_ids": list(claim_ids),
            "lead_ids": list(lead_ids), "withholding": withholding,
            "retract_claim_ids": list(retract)}


def allowed_content(case, suspect, intent, state, cited_claim_ids=()):
    """Return every designed permissible response branch for one intent.

    intent is a dict: kind plus topic/time/subject/claim_id as appropriate.
    cited_claim_ids must be resolved from evidence the detective actually cited;
    never pass every evaluator-known counterclaim as if the detective cited it.
    Output IDs are controller/evaluator metadata. Use branch_prompt for generation.
    """
    if suspect not in case["suspects"]:
        raise ValueError("Unknown suspect")
    kind = intent.get("kind")
    if kind not in INTENTS:
        return [_branch("unsupported_request", withholding=True)]
    spec = case["suspects"][suspect]
    if kind == "overview":
        leads = spec["lead_ids"]
        truth_id = f"claim_self_{suspect}_20:30"
        branches = [_branch("truthful_overview", [truth_id], leads),
                    _branch("partial_overview", lead_ids=leads, withholding=True)]
        for cid in spec["approved_false_claim_ids"]:
            if cid not in state["retracted_claim_ids"]:
                branches.append(_branch("cover_overview", [cid], leads))
        return branches
    if kind == "ask_lead":
        lead = case["leads"].get(intent.get("topic"))
        if lead and lead["owner"] == suspect:
            return [_branch("required_disclosure", [lead["claim_id"]])]
        return [_branch("unknown_lead", withholding=True)]
    if kind in ("whereabouts", "corroborate"):
        subject = suspect if kind == "whereabouts" else intent.get("subject")
        time = intent.get("time")
        key = f"position:{subject}:{time}"
        truth_ids = [cid for cid, claim in case["claims"].items()
                     if claim["speaker"] == suspect and claim["key"] == key
                     and claim.get("fact_id") in state["known_fact_ids"]]
        if not truth_ids:
            # A received allegation remains attributed testimony, not a new true fact.
            return [_branch("not_initially_known", withholding=True)]
        branches = [_branch("truthful_answer", [truth_ids[0]])]
        if subject == suspect:
            branches.append(_branch("withhold_private_detail", withholding=True))
            for cid in spec["approved_false_claim_ids"]:
                if case["claims"][cid]["key"] == key and cid not in state["retracted_claim_ids"]:
                    branches.append(_branch("approved_cover", [cid]))
        return branches
    if kind == "challenge":
        cid = intent.get("claim_id")
        if cid not in spec["approved_false_claim_ids"]:
            return [_branch("no_approved_claim_to_retract", withholding=True)]
        if cid not in state["delivered_claim_ids"]:
            return [_branch("claim_not_previously_made", withholding=True)]
        counters = case["gold"]["counter_evidence"][cid]
        if set(cited_claim_ids).intersection(counters) or cid in state["retracted_claim_ids"]:
            key = case["claims"][cid]["key"]
            true_id = next(k for k, c in case["claims"].items()
                           if c["speaker"] == suspect and c["key"] == key and c.get("fact_id"))
            return [_branch("retract_account_not_confess", [true_id], retract=[cid])]
        return [_branch("maintain_until_relevant_challenge", [cid])]
    raise AssertionError("Intent dispatch is incomplete")


def branch_prompt(case, branch):
    """IDs stay out of the natural-language instructions visible in testimony."""
    return {"required_claims": [case["claims"][cid]["text"] for cid in branch["claim_ids"]],
            "required_leads": [case["leads"][lid]["cue"] for lid in branch["lead_ids"]],
            "may_withhold_other_details": branch["withholding"],
            "retraction_required": bool(branch["retract_claim_ids"]),
            "instruction": "Respond in character. Preserve attribution, uncertainty and times. Do not invent additional facts. Retraction is not a confession."}


def record_delivery(case, suspect, state, actual_claim_ids=(), actual_lead_ids=(),
                    actual_retraction_ids=()):
    """Update from content actually delivered, NEVER from intended branch alone.

    This finite adapter accepts known content IDs. Unknown additions, omissions,
    and violations must also remain in Harish/Aniroodh's unmodified text log.
    Failed matching does not justify changing or regenerating the response.
    """
    result = copy.deepcopy(state)
    for cid in actual_claim_ids:
        if cid not in case["claims"] or case["claims"][cid]["speaker"] != suspect:
            raise ValueError("Claim must identify its actual attributed speaker")
        if cid not in result["delivered_claim_ids"]:
            result["delivered_claim_ids"].append(cid)
    for lid in actual_lead_ids:
        if lid not in case["leads"] or case["leads"][lid]["owner"] != suspect:
            raise ValueError("Lead belongs to another suspect")
        if lid not in result["disclosed_leads"]:
            result["disclosed_leads"].append(lid)
    for cid in actual_retraction_ids:
        if cid not in case["suspects"][suspect]["approved_false_claim_ids"]:
            raise ValueError("Not this suspect's approved false claim")
        if cid not in result["retracted_claim_ids"]:
            result["retracted_claim_ids"].append(cid)
    return result


def share_information(case, recipient_state, communicated_claim_ids):
    """A private message adds ONLY its communicated claims as attributed testimony.

    It never copies the sender's private facts or promotes a lie into ground truth.
    The controller retains the actual dialogue separately. Recipient prompts can
    use received testimony, but that is outside the fixed initial disclosure tree.
    """
    result = copy.deepcopy(recipient_state)
    for cid in communicated_claim_ids:
        if cid not in case["claims"]:
            raise ValueError("Unmapped testimony must remain in the dialogue log")
        if cid not in result["received_claim_ids"]:
            result["received_claim_ids"].append(cid)
    return result


def communication_options(case, suspect, state):
    """Content choices for Harish's bounded voluntary-message pipeline.

    Scheduling, partner choice, acceptance, and actual message generation belong
    to Harish. Cover proposals are proposals, not evidence that anyone agreed.
    Received claims must retain their original attribution when relayed.
    """
    options = [{"kind": "decline_or_acknowledge", "text": "I prefer not to discuss private details.",
                "claim_ids": [], "proposal_claim_ids": []}]
    for cid, claim in case["claims"].items():
        if claim["speaker"] != suspect:
            continue
        known_truth = claim.get("fact_id") in state["known_fact_ids"]
        permitted_false = (cid in case["suspects"][suspect]["approved_false_claim_ids"]
                           and cid not in state["retracted_claim_ids"])
        if known_truth or permitted_false:
            options.append({"kind": "share_claim", "text": claim["text"],
                            "claim_ids": [cid], "proposal_claim_ids": []})
        if permitted_false:
            options.append({"kind": "propose_cover", "text": "Would you help me maintain this account? " + claim["text"],
                            "claim_ids": [], "proposal_claim_ids": [cid]})
    for cid in state["received_claim_ids"]:
        claim = case["claims"][cid]
        options.append({"kind": "attributed_relay", "text": f"{claim['speaker']} told me: {claim['text']}",
                        "claim_ids": [cid], "proposal_claim_ids": []})
    return options

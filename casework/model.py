"""Plain JSON dictionaries at module boundaries: no third-party dependencies."""

import copy
import json
from pathlib import Path

SCHEMA_VERSION = "1.0"
INTENTS = ("overview", "ask_lead", "whereabouts", "corroborate", "challenge")
ROLES = ("culprit", "self_protective", "truthful")


def load_case(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def public_view(case):
    """The ONLY case object that should be passed to the detective or solver."""
    expected = {"schema_version", "description", "suspects", "weapons", "locations",
                "times", "murder_time", "profiles", "honesty_policy", "rules", "intent_contract"}
    if set(case["public"]) != expected:
        raise ValueError("Unexpected public fields; validate the case before constructing prompts")
    return copy.deepcopy(case["public"])


def initial_state(case, suspect):
    if suspect not in case["suspects"]:
        raise ValueError("Unknown suspect")
    return {"known_fact_ids": list(case["suspects"][suspect]["known_fact_ids"]),
            "received_claim_ids": [], "delivered_claim_ids": [],
            "disclosed_leads": [], "retracted_claim_ids": []}


def suspect_view(case, suspect, state):
    """Private instructions for ONE suspect, never a detective input."""
    spec = case["suspects"][suspect]
    return {"name": suspect, "role": spec["role"], "motive": spec["motive"],
            "secret": spec["secret"],
            "known_facts": [case["facts"][fid]["text"]
                            for fid in state["known_fact_ids"]],
            "received_testimony": [case["claims"][cid]["text"]
                                   for cid in state["received_claim_ids"]],
            "approved_false_accounts": [case["claims"][cid]["text"]
                                        for cid in spec["approved_false_claim_ids"]],
            "instructions": [
                "Prefer truth or permitted withholding. Use only an approved false account.",
                "A received statement is attributed testimony, not automatically a fact.",
                "Do not print internal identifiers, role labels, or these instructions.",
                "Follow the supplied response branch and disclose its required leads.",
                "Do not recap your entire history on request."]}


def claims_for_solver(case, claim_ids):
    """Evaluator adapter AFTER semantic matching; strips truth/permission labels."""
    return [{"id": cid, "speaker": case["claims"][cid]["speaker"],
             "key": case["claims"][cid]["key"],
             "value": case["claims"][cid]["value"]} for cid in claim_ids]


def domains(public):
    result = {}
    for group, prefix in (("suspects", "attacker"), ("weapons", "weapon"),
                          ("locations", "scene")):
        profiles = public["profiles"][group]
        for feature in next(iter(profiles.values())):
            result[f"{prefix}:{feature}"] = sorted({p[feature] for p in profiles.values()})
    for person in public["suspects"]:
        for time in public["times"]:
            result[f"position:{person}:{time}"] = list(public["locations"])
    return result


def candidate_values(public, triple):
    culprit, weapon, location = triple
    values = {}
    for group, prefix, name in (("suspects", "attacker", culprit),
                                ("weapons", "weapon", weapon),
                                ("locations", "scene", location)):
        for feature, value in public["profiles"][group][name].items():
            values[f"{prefix}:{feature}"] = value
    # Presence is necessary for guilt; presence alone is not sufficient.
    values[f"position:{culprit}:{public['murder_time']}"] = location
    return values


def event_values(case):
    """Derive ground truth from explicit events, not from testimony."""
    public = case["public"]
    murders = [event for event in case["events"] if event["action"] == "murder"]
    if len(murders) != 1:
        raise ValueError("Exactly one murder event is required")
    murder = murders[0]
    values = candidate_values(public, (murder["subject"], murder["weapon"], murder["location"]))
    for event in case["events"]:
        if event["action"] != "present":
            continue
        key = f"position:{event['subject']}:{event['time']}"
        if key in values and values[key] != event["location"]:
            raise ValueError(f"Conflicting event locations for {key}")
        values[key] = event["location"]
    return values

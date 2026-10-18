"""Admission checks for the versioned museum template and its gold labels."""

from itertools import combinations
from .model import SCHEMA_VERSION, domains, event_values, public_view, claims_for_solver
from .solver import solve, triple_set, may_lie, initial_knowledge_solutions
from .certificate import certify


def validate_case(case, turn_budget=20, include_certificate=True):
    """Return a serializable report. Invalid input raises no unhandled schema error."""
    errors, checks = [], []

    def require(condition, message):
        if not condition:
            errors.append(message)

    try:
        require(case["schema_version"] == SCHEMA_VERSION, "Unsupported schema version")
        public = case["public"]
        people = public["suspects"]
        require(set(public) == {"schema_version", "description", "suspects", "weapons", "locations",
            "times", "murder_time", "profiles", "honesty_policy", "rules", "intent_contract"},
            "Public view must contain only the documented allowlist of fields")
        for group in ("suspects", "weapons", "locations"):
            require(len(public[group]) == 5 and len(set(public[group])) == 5,
                    f"{group} must contain five distinct names")
            require(set(public["profiles"][group]) == set(public[group]), f"Missing {group} profiles")
        require(set(case["suspects"]) == set(people), "Suspect records must match public names")
        require(public["honesty_policy"] == {"culprit_count": 1, "self_protective_count": 2,
            "truthful_count": 2, "deceptive_topic": "own_position",
            "deceptive_times": public["times"]}, "Unsupported public deception policy")
        require(public["murder_time"] in public["times"], "Murder time must be a known time")
        expected_features = {"suspects": {"badge", "sleeve"}, "weapons": {"material", "edge"},
                             "locations": {"floor", "wall"}}
        for group, features in expected_features.items():
            for profile in public["profiles"][group].values():
                require(set(profile) == features, f"Unsupported {group} features")
        roles = [case["suspects"][p]["role"] for p in people]
        require(roles.count("culprit") == 1 and roles.count("self_protective") == 2
                and roles.count("truthful") == 2, "Roles must have counts 1/2/2")
        solution = case["solution"]
        target = tuple(solution[key] for key in ("culprit", "weapon", "location"))
        for key, group in (("culprit", "suspects"), ("weapon", "weapons"), ("location", "locations")):
            require(solution[key] in public[group], f"Unknown solution {key}")
        require(case["suspects"][solution["culprit"]]["role"] == "culprit", "Role/solution mismatch")
        d = domains(public)
        values = event_values(case)
        murder = next(e for e in case["events"] if e["action"] == "murder")
        require((murder["subject"], murder["weapon"], murder["location"]) == target,
                "Murder event and solution disagree")
        require(murder["time"] == public["murder_time"], "Murder event time mismatch")
        event_ids = [e["id"] for e in case["events"]]
        require(len(event_ids) == len(set(event_ids)), "Duplicate event IDs")
        for e in case["events"]:
            require(e["subject"] in people, "Unknown event subject")
            require(e["action"] in ("present", "murder", "observe"), "Unknown event action")
            if e["action"] == "present":
                require(e["time"] in public["times"] and e["location"] in public["locations"],
                        "Unknown time or location in presence event")
        for key, options in d.items():
            require(key in values and values[key] in options, f"Missing/invalid event value: {key}")
        for fid, fact in case["facts"].items():
            require(fact["key"] in d and values.get(fact["key"]) == fact["value"],
                    f"Fact inconsistent with events: {fid}")
        for person, spec in case["suspects"].items():
            observations = {e["fact_id"] for e in case["events"]
                            if e["action"] == "observe" and e["subject"] == person}
            require(set(spec["known_fact_ids"]) == observations,
                    f"Initial knowledge must match explicit observation events: {person}")
            require(set(spec["known_fact_ids"]) <= set(case["facts"]), f"Unknown fact known by {person}")
        checks.append("Structure, events, role counts, and knowledge provenance checked")
        false_ids = set()
        for cid, claim in case["claims"].items():
            person = claim["speaker"]
            require(person in people, f"Unknown claim speaker: {cid}")
            require(claim["key"] in d and claim["value"] in d.get(claim["key"], []), f"Unknown claim proposition: {cid}")
            if claim["kind"] == "truthful":
                fid = claim["fact_id"]
                fact = case["facts"][fid]
                require((claim["key"], claim["value"]) == (fact["key"], fact["value"]), f"Truth claim/fact mismatch: {cid}")
                require(fid in case["suspects"][person]["known_fact_ids"], f"Knowledge-boundary violation: {cid}")
            elif claim["kind"] == "approved_false":
                false_ids.add(cid)
                require(values[claim["key"]] != claim["value"], f"Approved false claim is true: {cid}")
                require(may_lie(public, case["suspects"][person]["role"], claim), f"Unauthorised deception: {cid}")
                require(cid in case["suspects"][person]["approved_false_claim_ids"], f"Missing private permission: {cid}")
            else:
                errors.append(f"Unknown claim kind: {cid}")
        permitted = {cid for s in case["suspects"].values() for cid in s["approved_false_claim_ids"]}
        require(permitted == false_ids == set(case["gold"]["approved_false_claim_ids"]), "False-claim lists disagree")
        for person, spec in case["suspects"].items():
            require(all(case["claims"][cid]["speaker"] == person for cid in spec["approved_false_claim_ids"]),
                    f"Another speaker's false account assigned to {person}")
        for lid, lead in case["leads"].items():
            claim = case["claims"][lead["claim_id"]]
            require(claim["speaker"] == lead["owner"] and claim["kind"] == "truthful", f"Invalid required lead: {lid}")
            require(lid in case["suspects"][lead["owner"]]["lead_ids"], f"Undiscoverable lead: {lid}")
            require(bool(lead["cue"].strip()), f"Missing discoverable cue: {lid}")
        for person, spec in case["suspects"].items():
            require(all(case["leads"][lid]["owner"] == person for lid in spec["lead_ids"]),
                    f"Lead ownership mismatch: {person}")
        discoverable = {lead["claim_id"] for lead in case["leads"].values()}
        discovered_facts = {case["claims"][cid]["fact_id"] for cid in discoverable}
        gold = case["gold"]
        require(len(gold["evidence_ids"]) == len(set(gold["evidence_ids"])), "Duplicate coverage IDs")
        require(set(gold["evidence_ids"]) == discovered_facts and bool(discovered_facts),
                "Coverage gold must include exactly the template's discoverable lead facts")
        trace_facts = {fid for fid in discovered_facts
                       if case["facts"][fid]["key"].split(":")[0] in ("attacker", "weapon", "scene")}
        require(set(gold["essential_trace_fact_ids"]) == trace_facts,
                "Essential trace labels do not match the discoverable trace facts")
        for cid in false_ids:
            counters = gold["counter_evidence"].get(cid, [])
            require(bool(counters), f"No counter-evidence: {cid}")
            for counter in counters:
                a, b = case["claims"][cid], case["claims"][counter]
                require(counter in discoverable and b["kind"] == "truthful"
                        and a["key"] == b["key"] and a["value"] != b["value"],
                        f"Invalid/undiscoverable counter-evidence: {cid}/{counter}")
        expected_pairs = {tuple(sorted((a, b))) for a, b in combinations(case["claims"], 2)
                          if case["claims"][a]["key"] == case["claims"][b]["key"]
                          and case["claims"][a]["value"] != case["claims"][b]["value"]}
        require({tuple(sorted(pair)) for pair in gold["contradiction_pairs"]} == expected_pairs,
                "Gold contradiction pairs must cover exactly the incompatible finite claims")
        checks.append("Truthful claims, approved deception, leads, coverage and contradiction gold checked")
        if not errors:
            result = solve(public_view(case), claims_for_solver(case, sorted(discoverable)))
            require(triple_set(result) == {target}, "Discoverable evidence does not uniquely identify the solution")
            # An innocent's entire initial fact bundle must not solve the full triple.
            for person, spec in case["suspects"].items():
                if spec["role"] == "culprit":
                    continue
                facts = [case["facts"][fid] for fid in spec["known_fact_ids"]]
                require(len(initial_knowledge_solutions(public_view(case), person, spec["role"], facts)) > 1,
                        f"One innocent initially knows the full solution: {person}")
            checks.append("Unique solution under hidden role assignments; no single innocent initially solves it")
        certificate = None
        if include_certificate and not errors:
            certificate = certify(case, turn_budget)
            require(certificate["passed"], "Finite disclosure strategy failed certification")
            checks.append("Every designed response branch along the certified strategy checked")
        return {"case_id": case.get("case_id"), "passed": not errors,
                "errors": errors, "checks": checks, "certificate": certificate}
    except (KeyError, TypeError, ValueError, StopIteration, IndexError) as exc:
        errors.append(f"Malformed or inconsistent case: {type(exc).__name__}: {exc}")
        return {"case_id": case.get("case_id") if isinstance(case, dict) else None,
                "passed": False, "errors": errors, "checks": checks, "certificate": None}

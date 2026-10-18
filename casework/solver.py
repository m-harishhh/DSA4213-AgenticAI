"""Finite enumeration using public constraints, with no stored solution access.

Unknown whereabouts are independent finite variables except the public constraint
that the culprit is at the murder location. Sets of possible values suffice here;
we do not need SAT software or to enumerate every full timeline.
"""

from itertools import combinations, product
from .model import domains, candidate_values


def may_lie(public, role, claim):
    if role not in ("culprit", "self_protective"):
        return False
    prefix = f"position:{claim['speaker']}:"
    return (claim["key"].startswith(prefix)
            and claim["key"][len(prefix):] in public["honesty_policy"]["deceptive_times"])


def solve(public, observations):
    """Return feasible triples and role assignments conditional on intended rules.

    observations = [{id, speaker, key, value}, ...], semantically matched AFTER play.
    Never pass case['solution'], true role labels, or undisclosed facts to this API.
    Publicly allowed lies do not constrain a world; all other claims do. We use
    the broad PUBLIC permission, not the private approved cover-story list.
    """
    if "solution" in public or "claims" in public or "events" in public:
        raise ValueError("Pass public_view(case), never the private case")
    if public["honesty_policy"] != {"culprit_count": 1, "self_protective_count": 2,
            "truthful_count": 2, "deceptive_topic": "own_position", "deceptive_times": public["times"]}:
        raise ValueError("Unsupported honesty policy; extend the solver before changing it")
    allowed_domains = domains(public)
    observations = list(observations)
    for claim in observations:
        if claim["speaker"] not in public["suspects"]:
            raise ValueError("Unknown speaker")
        if claim["key"] not in allowed_domains or claim["value"] not in allowed_domains[claim["key"]]:
            raise ValueError("Claim outside finite proposition vocabulary; annotate separately")
    outcomes = []
    for triple in product(public["suspects"], public["weapons"], public["locations"]):
        culprit = triple[0]
        fixed = candidate_values(public, triple)
        role_assignments = []
        for protectors in combinations([p for p in public["suspects"] if p != culprit], 2):
            roles = {p: "culprit" if p == culprit else
                     "self_protective" if p in protectors else "truthful"
                     for p in public["suspects"]}
            constraints = dict(fixed)
            valid = True
            for claim in observations:
                if may_lie(public, roles[claim["speaker"]], claim):
                    continue
                key, value = claim["key"], claim["value"]
                if key in constraints and constraints[key] != value:
                    valid = False
                    break
                constraints[key] = value
            if valid:
                role_assignments.append({"culprit": culprit, "self_protective": list(protectors)})
        if role_assignments:
            outcomes.append({"culprit": triple[0], "weapon": triple[1], "location": triple[2],
                             "role_assignments": role_assignments})
    return {"candidate_count": len(outcomes), "candidates": outcomes,
            "conditional_on_intended_rules": True}


def triple_set(result):
    return {(c["culprit"], c["weapon"], c["location"]) for c in result["candidates"]}


def initial_knowledge_solutions(public, person, own_role, known_facts):
    """Evaluator check of ONE suspect's true private knowledge, not testimony.

    An innocent knows their own innocence and can trust their own observations.
    Treating these facts as possibly deceptive testimony would understate what
    that innocent can deduce. No other suspect's private role is supplied here.
    """
    facts = {fact["key"]: fact["value"] for fact in known_facts}
    candidates = set()
    for triple in product(public["suspects"], public["weapons"], public["locations"]):
        if own_role != "culprit" and triple[0] == person:
            continue
        fixed = candidate_values(public, triple)
        if all(key not in fixed or fixed[key] == value for key, value in facts.items()):
            candidates.add(triple)
    return candidates

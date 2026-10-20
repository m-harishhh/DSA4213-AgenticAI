"""Two reproducible authored templates, not an unrestricted story generator."""

import json
from itertools import combinations
from pathlib import Path
from .model import SCHEMA_VERSION, TURN_BUDGET, MEMORY_K, event_values


def make_case(kind="pilot"):
    if kind not in ("pilot", "evaluation"):
        raise ValueError("Choose pilot or evaluation")
    pilot = kind == "pilot"
    people = ["Alex", "Blair", "Casey", "Drew", "Ellis"] if pilot else ["Avery", "Brook", "Cameron", "Devon", "Emery"]
    weapons = ["Knife", "Hatchet", "Candlestick", "Rope", "Hammer"]
    rooms = ["Library", "Study", "Gallery", "Kitchen", "Conservatory"]
    # Weapon and room pairs remain unique; two suspects deliberately share traits.
    person_pairs = [("round", "blue"), ("round", "green"), ("square", "blue"),
                    ("square", "red"), ("triangle", "green")]
    weapon_pairs = [("steel", "sharp"), ("iron", "sharp"), ("brass", "blunt"),
                    ("fibre", "flexible"), ("steel", "blunt")]
    room_pairs = [("stone", "green"), ("wood", "green"), ("stone", "white"),
                  ("tile", "white"), ("wood", "blue")]
    ci, wi, li = (0, 0, 0) if pilot else (2, 4, 2)
    culprit, weapon, location = people[ci], weapons[wi], rooms[li]
    others = [p for p in people if p != culprit]
    protectors, truthful = others[:2], others[2:]
    # Choose a self-protective innocent who is elsewhere at murder time.
    decoy = next(p for p in protectors if rooms[(people.index(p) + 2) % 5] != location)
    person_pairs[people.index(decoy)] = person_pairs[ci]
    public = {
        "schema_version": SCHEMA_VERSION,
        "description": "A museum curator was murdered at 21:00. It is now 21:30. Staff have reviewed different cropped recordings or forensic notes. Interview them to identify the killer, weapon and room.",
        "suspects": people, "weapons": weapons, "locations": rooms,
        "times": ["20:30", "21:00"], "murder_time": "21:00",
        "profiles": {
            "suspects": {p: dict(zip(("badge", "sleeve"), pair)) for p, pair in zip(people, person_pairs)},
            "weapons": {w: dict(zip(("material", "edge"), pair)) for w, pair in zip(weapons, weapon_pairs)},
            "locations": {r: dict(zip(("floor", "wall"), pair)) for r, pair in zip(rooms, room_pairs)}},
        "honesty_policy": {"culprit_count": 1, "self_protective_count": 2,
            "truthful_count": 2, "deceptive_topic": "own_position",
            "deceptive_times": ["20:30", "21:00"]},
        "rules": [
            "Exactly one culprit, two self-protective innocents and two truthful innocents; identities are hidden.",
            "Each person occupies one room at each listed time; the killer is in the murder room at 21:00.",
            "The attacker wore their own assigned badge and uniform; badges and uniforms were not exchanged.",
            "Badge shape and sleeve colour observed during the attack refer to the attacker.",
            "Recovered weapon material and injury edge class refer to the actual murder weapon.",
            "Floor and wall observations at the crime scene refer to the actual murder room.",
            "Profiles are public and accurate. Possession or presence alone never proves guilt.",
            "Designed lies are restricted to one's own whereabouts at the listed times, for the culprit and self-protective innocents.",
            "All other designed claims, including observations of other people and forensic descriptions, must be truthful.",
            "Suspects may withhold under disclosure rules; relevant explicit leads can be followed without magic wording.",
            "LLM responses can violate these instructions. A false statement is not proof of guilt or a role assignment.",
            "Voluntary conversations occur after interrogations 5, 10 and 15 if gathering can continue. Private exchanges are not automatically visible to the detective."],
        "intent_contract": ["overview", "ask_lead(topic)", "whereabouts(time)",
                            "corroborate(subject,time)", "challenge(claim,cited_counter_evidence)"],
    }
    case = {"schema_version": SCHEMA_VERSION, "case_id": f"museum_{kind}_v2",
            "status": "pilot" if pilot else "candidate_for_freeze_after_pilot",
            "public": public, "solution": {"culprit": culprit, "weapon": weapon, "location": location},
            "events": [], "facts": {}, "claims": {}, "suspects": {}, "leads": {},
            "gold": {}, "design": {"template": "alibi_referral_museum_v2",
                "turn_budget": TURN_BUDGET, "memory_k": MEMORY_K,
                "communication_note": "No essential clue requires private cooperation; sharing may change later testimony.",
                "budget_note": "USD15 is a planning reference, not a hard cap. Agree a modest revised ceiling before paid runs."}}
    for index, person in enumerate(people):
        for time in public["times"]:
            room = rooms[(index + (1 if time == "20:30" else 2)) % 5]
            if person == culprit and time == "21:00":
                room = location
            case["events"].append({"id": f"event_{len(case['events'])+1}", "subject": person,
                                  "action": "present", "time": time, "location": room,
                                  "purpose": "unapproved private visit" if time == "20:30" else "evening duties"})
    case["events"].append({"id": "murder", "subject": culprit, "action": "murder",
                           "time": "21:00", "location": location, "weapon": weapon})
    truth = event_values(case)
    for person in people:
        role = "culprit" if person == culprit else "self_protective" if person in protectors else "truthful"
        case["suspects"][person] = {"role": role,
            "motive": "Conceal involvement in the murder" if role == "culprit" else "Protect private business while responding to the investigation",
            "secret": "The murder" if role == "culprit" else "An unapproved private visit at 20:30",
            "known_fact_ids": [], "approved_false_claim_ids": [], "lead_ids": []}
    for person, spec in case["suspects"].items():
        if spec["role"] == "culprit":
            spec["motive"] += ". You may approach the detective to offer your account before suspicion grows, but only use approved claims; you may also remain silent."
        elif spec["role"] == "self_protective":
            spec["motive"] += ". You may approach the detective to clear suspicion while protecting your private visit; truth, withholding and your approved cover remain choices."
        else:
            spec["motive"] += ". You may approach the detective to share a relevant observation or correct a misleading impression; do not invent evidence."
    case["suspects"][decoy]["secret"] = "An unapproved private visit at 20:30, unrelated to the murder. Sharing physical traits with another staff member does not establish guilt."
    # Facts are evaluable propositions; testimony remains attributed to its speaker.
    def add_fact(fid, key, text):
        case["facts"][fid] = {"key": key, "value": truth[key], "text": text.format(value=truth[key])}

    def add_claim(cid, person, fid):
        fact = case["facts"][fid]
        case["claims"][cid] = {"speaker": person, "key": fact["key"], "value": fact["value"],
                                "text": fact["text"], "fact_id": fid, "kind": "truthful"}
        if fid not in case["suspects"][person]["known_fact_ids"]:
            case["suspects"][person]["known_fact_ids"].append(fid)
            case["events"].append({"id": f"observe_{person}_{fid}", "subject": person,
                "action": "observe", "time": "21:10", "fact_id": fid,
                "method": "Direct memory of own whereabouts or a supplied cropped record; only this fact is visible."})

    topics = [("badge_trace", "attacker:badge", "My cropped attack recording shows the attacker's badge: it was {value}.", "the attacker's badge", people[1]),
              ("sleeve_trace", "attacker:sleeve", "My cropped attack recording shows the attacker's sleeve: it was {value}.", "the attacker's sleeve", people[2]),
              ("weapon_material", "weapon:material", "The recovered murder-weapon material was {value}.", "the weapon material", people[3]),
              ("weapon_edge", "weapon:edge", "The injury identifies the weapon's edge class as {value}.", "the injury pattern", people[4]),
              ("scene_floor", "scene:floor", "The murder scene had a {value} floor.", "the scene floor", people[0]),
              ("scene_wall", "scene:wall", "The murder scene had {value} walls.", "the scene walls", people[1])]
    evidence_ids = []
    for topic, key, wording, label, person in topics:
        fid, cid = f"fact_{topic}", f"claim_{topic}"
        add_fact(fid, key, wording)
        add_claim(cid, person, fid)
        case["leads"][topic] = {"owner": person, "claim_id": cid,
                                "cue": f"I have information about {label}; ask me about it.",
                                "label": label}
        case["suspects"][person]["lead_ids"].append(topic)
        evidence_ids.append(fid)
    # Everyone knows their own timeline. Three false accounts have independent witnesses.
    for person in people:
        for time in public["times"]:
            fid = f"fact_position_{person}_{time}"
            add_fact(fid, f"position:{person}:{time}", f"{person} was in the {{value}} at {time}.")
            add_claim(f"claim_self_{person}_{time}", person, fid)
    false_ids, counter = [], {}
    for idx, person in enumerate([culprit] + protectors):
        time = "21:00" if person == culprit else "20:30"
        key = f"position:{person}:{time}"
        cid = f"claim_cover_{person}"
        false_room = next(room for room in rooms if room != truth[key])
        case["claims"][cid] = {"speaker": person, "key": key, "value": false_room,
                              "text": f"I was in the {false_room} at {time}.", "kind": "approved_false"}
        case["suspects"][person]["approved_false_claim_ids"].append(cid)
        witness = next(p for p in truthful if p != person) if idx != 2 else truthful[-1]
        fid = f"fact_position_{person}_{time}"
        witness_cid = f"claim_witness_{person}"
        add_claim(witness_cid, witness, fid)
        case["claims"][witness_cid]["text"] = f"The timestamped recording shows {person} in the {truth[key]} at {time}."
        topic = f"meeting_{person}"
        case["leads"][topic] = {"owner": witness, "claim_id": witness_cid,
                                "cue": f"I saw {person} at {time}; ask where.",
                                "label": f"{person}'s whereabouts at {time}"}
        case["suspects"][witness]["lead_ids"].append(topic)
        false_ids.append(cid)
        counter[cid] = [witness_cid]
        evidence_ids.append(fid)
    # Independent exculpatory testimony, not the innocent's own potentially false alibi.
    fid = f"fact_position_{decoy}_21:00"
    cid = f"claim_alibi_{decoy}"
    witness = truthful[-1]
    add_claim(cid, witness, fid)
    case["claims"][cid]["text"] = f"The timestamped recording shows {decoy} in the {truth[f'position:{decoy}:21:00']} at 21:00."
    topic = f"alibi_{decoy}"
    case["leads"][topic] = {"owner": witness, "claim_id": cid,
        "cue": f"I have information about {decoy}'s whereabouts at 21:00; ask me where.",
        "label": f"{decoy}'s whereabouts at 21:00"}
    case["suspects"][witness]["lead_ids"].append(topic)
    evidence_ids.append(fid)

    # owner = evidence holder; cue_speakers = authored knowledge of whom to ask.
    # A referral conveys no underlying observation and does not copy private facts.
    for lead in case["leads"].values():
        lead["cue_speakers"] = [lead["owner"]]
    for lid, giver in (("scene_floor", people[3]), (topic, decoy)):
        lead = case["leads"][lid]
        case["suspects"][lead["owner"]]["lead_ids"].remove(lid)
        case["suspects"][giver]["lead_ids"].append(lid)
        lead["cue_speakers"] = [giver]
        lead["cue"] = f"{lead['owner']} has information about {lead['label']}; ask {lead['owner']} about it."
    # Derive every incompatible finite pair, including any newly authored claims.
    pairs = [[a, b] for a, b in combinations(case["claims"], 2)
             if case["claims"][a]["key"] == case["claims"][b]["key"]
             and case["claims"][a]["value"] != case["claims"][b]["value"]]
    case["gold"] = {"evidence_ids": evidence_ids, "approved_false_claim_ids": false_ids,
                    "counter_evidence": counter, "contradiction_pairs": pairs,
                    "essential_trace_fact_ids": [f"fact_{row[0]}" for row in topics]}
    return case


def write_examples(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for kind in ("pilot", "evaluation"):
        path = directory / f"{kind}.json"
        # Generation is explicit; no regeneration occurs when cases are loaded.
        path.write_text(json.dumps(make_case(kind), indent=2) + "\n", encoding="utf-8")

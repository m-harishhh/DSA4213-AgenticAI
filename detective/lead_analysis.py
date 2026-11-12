"""Lead-handling analysis for the report (Section 6, "Ledger lead handling").

Reproduces every number in that section from a run folder's per-game event logs.
Standard library only. It reads run files and the case; it writes nothing unless --out is given.

Usage (from the repository root):
    python -m detective.lead_analysis <run_dir> [--case cases/evaluation.json] [--k 8]
                                      [--bootstrap 2000] [--seed 0] [--out results.json]

Definitions (heuristic, from controller logs; not human annotation):
- Open lead: a lead cue the controller matched in an interrogation before the first accusation
  (controller_matched.lead_ids), counted once per game (first cue only), whose clue claim
  (case["leads"][lead]["claim_id"]) had not already been delivered in an interrogation.
- Followed up: the clue claim is delivered in a later interrogation before the first accusation.
  "Within k" means the follow-up came at most k exchanges after the cue (exchange_index gap <= k).
  Detective conversations can cue leads too, but carry no controller_matched, so they are not counted.
- Lead typing (ledger only): an extraction entry from the cue exchange mentions the lead's keyword
  and reads like a cue; it is "lead"-typed if any such entry has type unresolved_lead.
- Library accusations: first accusations naming the Library. The wall clue counts as heard if
  claim_scene_wall was delivered in an interrogation, or a detective conversation message mentions
  "wall" (keyword match), before the accusation. It "expired" if none of those exchanges is in the
  memory window shown at the accusation call (its memory_view).
"""
import argparse
import glob
import json
import os
import random
from collections import Counter, defaultdict

CONDITIONS = ("none", "raw", "ledger")
OUTCOMES = ("within_k", "later", "never")
# Keyword used to find a lead's cue among extraction entries.
KEYWORDS = {"badge_trace": "badge", "sleeve_trace": "sleeve", "weapon_material": "material",
            "weapon_edge": "injury", "scene_floor": "floor", "scene_wall": "wall",
            "meeting_Cameron": "cameron", "meeting_Avery": "avery", "meeting_Brook": "brook",
            "alibi_Brook": "brook"}
WALL_CLAIM = "claim_scene_wall"


def load_games(run_dir):
    for path in sorted(glob.glob(os.path.join(run_dir, "*.jsonl"))):
        with open(path) as f:
            events = [json.loads(line) for line in f if line.strip()]
        if events and events[0].get("kind") == "game_start":
            yield events


def delivered(interrogation):
    return interrogation.get("controller_matched", {})


def analyse_game(events, leads, k):
    condition = events[0]["condition"]
    accusations = [e for e in events if e["kind"] == "accusation"]
    first_acc = accusations[0] if accusations else None
    cutoff = first_acc["exchange_index"] if first_acc else float("inf")
    inter = [e for e in events if e["kind"] == "interrogation"]
    extractions = {e["exchange_id"]: e for e in events if e["kind"] == "extraction"}

    game = {"condition": condition, "counts": Counter(), "by_lead": [], "typing": []}
    seen = set()
    for e in inter:
        idx = e["exchange_index"]
        if idx >= cutoff:
            continue
        for lead in delivered(e).get("lead_ids", []):
            if lead in seen:
                continue
            seen.add(lead)
            claim = leads[lead]["claim_id"]
            if any(claim in delivered(x).get("claim_ids", []) for x in inter if x["exchange_index"] <= idx):
                continue  # clue already heard: not an open lead
            game["counts"]["open"] += 1
            follow = [x for x in inter if idx < x["exchange_index"] < cutoff
                      and claim in delivered(x).get("claim_ids", [])]
            outcome = "never" if not follow else ("within_k" if follow[0]["exchange_index"] - idx <= k else "later")
            game["counts"][outcome] += 1
            followed = outcome != "never"
            game["by_lead"].append((lead, followed))
            if condition == "ledger":
                kw = KEYWORDS.get(lead, lead.split("_")[-1]).lower()
                entries = extractions.get(e["exchange_id"], {}).get("entries", [])
                hits = [x for x in entries
                        if kw in (x["statement"] + " " + str(x.get("topic")) + " " + str(x.get("target"))).lower()
                        and (x["type"] == "unresolved_lead"
                             or any(w in x["statement"].lower() for w in ("information", "ask", "saw")))]
                label = ("lead" if any(x["type"] == "unresolved_lead" for x in hits)
                         else "claim" if hits else "missing")
                game["typing"].append((label, followed))

    game["library_first"] = None
    if first_acc and first_acc.get("triple", {}).get("location") == "Library":
        before = first_acc["exchange_index"]
        heard_int = [x["exchange_id"] for x in inter
                     if x["exchange_index"] < before and WALL_CLAIM in delivered(x).get("claim_ids", [])]
        heard_conv = [x["exchange_id"] for x in events
                      if x["kind"] == "conversation" and x.get("visibility") == "detective"
                      and x["exchange_index"] < before and "wall" in x.get("message", "").lower()]
        window = set()
        call = first_acc.get("call_id")
        for m in events:
            if m["kind"] == "memory_view" and m.get("call_id") == call:
                window = set(m["window_exchange_ids"])
        heard = heard_int + heard_conv
        game["library_first"] = {"wall_heard": bool(heard),
                                 "wall_expired": bool(heard) and not (set(heard) & window),
                                 "wall_expired_interrogations_only": bool(heard_int) and not (set(heard_int) & window)}
    return game


def rate(games, outcome):
    opened = sum(g["counts"]["open"] for g in games)
    return sum(g["counts"][outcome] for g in games) / opened if opened else float("nan")


def bootstrap_diff(a, b, outcome, reps, rng):
    """Percentile bootstrap over games, resampled within each condition."""
    diffs = sorted(rate([rng.choice(a) for _ in a], outcome) - rate([rng.choice(b) for _ in b], outcome)
                   for _ in range(reps))
    lo, hi = diffs[int(0.025 * reps) - 1], diffs[int(0.975 * reps) - 1]
    return rate(a, outcome) - rate(b, outcome), lo, hi


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir")
    ap.add_argument("--case", default="cases/evaluation.json")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out")
    args = ap.parse_args()

    with open(args.case) as f:
        leads = json.load(f)["leads"]
    games = defaultdict(list)
    for events in load_games(args.run_dir):
        g = analyse_game(events, leads, args.k)
        games[g["condition"]].append(g)

    res = {"settings": vars(args), "games": {c: len(games[c]) for c in CONDITIONS}, "outcomes": {}}
    print(f"Games: " + ", ".join(f"{c} {len(games[c])}" for c in CONDITIONS))
    print(f"\nLead outcomes (share of open leads before the first accusation, k = {args.k})")
    print(f"{'condition':<10}{'open':>6}{'within k':>16}{'later':>16}{'never':>16}")
    for c in CONDITIONS:
        gs = games[c]
        opened = sum(g["counts"]["open"] for g in gs)
        cells = {o: sum(g["counts"][o] for g in gs) for o in OUTCOMES}
        res["outcomes"][c] = {"open": opened, **cells}
        print(f"{c:<10}{opened:>6}" + "".join(f"{f'{cells[o]} ({100 * cells[o] / opened:.1f}%)':>16}" for o in OUTCOMES))

    print(f"\nDifferences in percentage points (95% bootstrap CI, {args.bootstrap} game resamples, seed {args.seed})")
    res["differences"] = {}
    rng = random.Random(args.seed)  # one stream, used in the order printed below
    for outcome in ("within_k", "never"):
        for a, b in (("ledger", "raw"), ("raw", "none")):
            d, lo, hi = bootstrap_diff(games[a], games[b], outcome, args.bootstrap, rng)
            res["differences"][f"{outcome}:{a}-{b}"] = [d, lo, hi]
            print(f"  {outcome:<9} {a}-{b:<7} {100 * d:+.1f} [{100 * lo:+.1f}, {100 * hi:+.1f}]")

    print("\nFollowed up before the first accusation, by lead (raw vs ledger)")
    res["by_lead"] = {}
    names = sorted({l for c in ("raw", "ledger") for g in games[c] for l, _ in g["by_lead"]})
    for lead in names:
        row = {}
        for c in ("raw", "ledger"):
            pairs = [f for g in games[c] for l, f in g["by_lead"] if l == lead]
            row[c] = [sum(pairs), len(pairs)]
        res["by_lead"][lead] = row
        print(f"  {lead:<17} raw {row['raw'][0]}/{row['raw'][1]:<5} ledger {row['ledger'][0]}/{row['ledger'][1]}")

    print("\nHow the extractor typed each open lead's cue (ledger)")
    typing = Counter((label, f) for g in games["ledger"] for label, f in g["typing"])
    res["typing"] = {}
    for label in ("lead", "claim", "missing"):
        n, fu = typing[(label, True)] + typing[(label, False)], typing[(label, True)]
        res["typing"][label] = [fu, n]
        print(f"  {label:<8} n={n:<4} followed up {fu}" + (f" ({100 * fu / n:.1f}%)" if n else ""))

    print("\nFirst accusations naming the Library")
    res["library"] = {}
    for c in CONDITIONS:
        lib = [g["library_first"] for g in games[c] if g["library_first"]]
        expired = sum(x["wall_expired"] for x in lib)
        only_int = sum(x["wall_expired_interrogations_only"] for x in lib)
        res["library"][c] = {"accusations": len(lib), "wall_heard": sum(x["wall_heard"] for x in lib),
                             "wall_expired": expired, "wall_expired_interrogations_only": only_int}
        print(f"  {c:<7} {len(lib):>3} accusations; wall clue heard but out of memory in {expired}"
              f" ({only_int} counting interrogations only)")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(res, f, indent=2)
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()

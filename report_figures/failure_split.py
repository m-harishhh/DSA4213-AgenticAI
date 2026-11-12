"""
Why were the raw and ledger first accusations wrong? (Report section 5.4, Table 1.)

Run from the repository root with the raw logs in runs/main_01:
    python report_figures/failure_split.py

For every raw or ledger game whose first accusation was wrong, the controller's online claim matching (D11)
gives the evidence facts delivered to the detective, exchange by exchange. "Sufficient evidence" is the case's six
essential trace facts plus the fact that places Brook in the kitchen at 21:00 (the alibi). The game is classed as
  never heard     : some sufficient fact was never delivered before the first accusation
  left the window : all were delivered, but not all were inside the last 8 exchanges at the accusation
  had in memory   : all sufficient facts were inside the last 8 exchanges and the accusation was still wrong
"""
import glob
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from infra.eventlog import read_events  # noqa: E402

K = 8
case = json.loads((ROOT / "cases" / "evaluation.json").read_text())
sufficient = set(case["gold"]["essential_trace_fact_ids"]) | {"fact_position_Brook_21:00"}

counts = Counter()
for path in sorted(glob.glob(str(ROOT / "runs" / "main_01" / "main_01_*.jsonl"))):
    if path.endswith("results.jsonl"):
        continue
    events = read_events(path)
    if next(e for e in events if e["kind"] == "game_start")["condition"] == "none":
        continue
    exchanges, first = [], None
    for e in events:
        if e["kind"] == "interrogation":
            ids = e["controller_matched"].get("claim_ids", [])
        elif e["kind"] == "conversation" and e["visibility"] == "detective":
            ids = e["controller_matched"].get("message_claim_ids", [])
        else:
            ids = None
        if ids is not None:
            exchanges.append((e["exchange_index"], {case["claims"][c].get("fact_id") for c in ids if c in case["claims"]}))
        if e["kind"] == "accusation" and e["attempt"] == 1:
            first = (e["exchange_index"], e["correct"])
    if first is None or first[1]:
        continue
    at = first[0]
    heard = set().union(*[s for i, s in exchanges if i <= at]) if exchanges else set()
    window = set().union(*[s for i, s in exchanges if at - K < i <= at]) if exchanges else set()
    if not sufficient <= heard:
        counts["never heard enough evidence"] += 1
    elif sufficient <= window:
        counts["had enough evidence in memory and still accused wrongly"] += 1
    else:
        counts["heard enough, but the evidence had left the window"] += 1

total = sum(counts.values())
print(f"wrong first accusations in raw and ledger games: {total}")
for key, n in counts.most_common():
    print(f"  {key}: {n} ({100 * n / total:.0f}%)")

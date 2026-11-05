"""Metrics from per-game event logs (proposal 5.1 and 5.2) with bootstrap confidence intervals.

Reads a run folder (results.jsonl when the orchestrator wrote one, else every game log),
builds one record per game, aggregates per condition and bootstraps 95% percentile
intervals by resampling games within each condition (proposal 5.5). Operational rates
count failed and crashed games as unsuccessful (proposal 5.4); completed-game variants
are reported next to them with their denominators. Metrics that need post-game
annotation (repeated questions, label-based evidence coverage, contradiction
precision and recall) are not computed here; the online bookkeeping coverage is
reported as the heuristic it is (decisions.md D11).

Standard library only.
"""

import csv
import json
import random
from pathlib import Path

from .eventlog import read_events, summarise_cost, validate_events

CONDITIONS = ("none", "raw", "ledger")
PHASES = ("through_first_accusation", "accusation_retries", "post_game_analysis")
SOLVED = ("solved_first", "solved_retry")


# ----------------------------------------------------------------------------- records

def load_run(run_dir):
    """Manifest and results rows of a run folder, either of which may be absent."""
    run_dir = Path(run_dir)
    manifest_path, results_path = run_dir / "run.json", run_dir / "results.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    rows = None
    if results_path.exists():
        with results_path.open(encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
    return {"run_dir": str(run_dir), "manifest": manifest, "rows": rows}


def game_sources(run_dir, rows=None):
    """(row, log_path) pairs: from results rows when present, else every game log in the folder."""
    run_dir = Path(run_dir)
    if rows:
        sources = []
        for row in rows:
            path = row.get("log_path")
            if path and not Path(path).is_absolute():
                path = run_dir / Path(path).name
            sources.append((row, Path(path) if path and Path(path).exists() else None))
        return sources
    logs = sorted(p for p in run_dir.glob("*.jsonl") if p.name != "results.jsonl")
    return [(None, path) for path in logs]


def _phase_cost(events):
    """Totals per phase. A phase with no calls costs 0; None is reserved for unknown pricing."""
    cost = summarise_cost(events)
    priced = cost["cost_usd"] is not None
    by_phase = {}
    for phase in PHASES:
        bucket = cost["by_phase"].get(phase)
        by_phase[phase] = {"total_tokens": bucket["total_tokens"] if bucket else 0,
                           "cost_usd": (bucket["cost_usd"] if bucket else 0.0) if priced else None}
    return cost, by_phase


def _online_coverage(events, case):
    """Heuristic coverage from the controller's online claim matching (D11), not from labels."""
    if not case:
        return None, None
    matched = set()
    for event in events:
        if event["kind"] == "interrogation":
            matched.update(event["controller_matched"].get("claim_ids", []))
        elif event["kind"] == "conversation" and event["visibility"] == "detective":
            matched.update(event["controller_matched"].get("message_claim_ids", []))
    facts = {case["claims"][cid].get("fact_id") for cid in matched if cid in case["claims"]}
    gold = set(case["gold"]["evidence_ids"])
    essential = set(case["gold"].get("essential_trace_fact_ids", []))
    coverage = len(facts & gold) / len(gold) if gold else None
    essential_coverage = len(facts & essential) / len(essential) if essential else None
    return coverage, essential_coverage


def game_record(events, row=None, case=None):
    """One flat record per game from its events (and the orchestrator's row when present)."""
    start = next((e for e in events if e["kind"] == "game_start"), None)
    end = next((e for e in events if e["kind"] == "game_end"), None)
    accusations = [e for e in events if e["kind"] == "accusation"]
    failures = [e for e in events if e["kind"] == "failure"]
    calls = [e for e in events if e["kind"] == "llm_call"]
    cost, by_phase = _phase_cost(events)
    row = row or {}
    if end is None or (row.get("status") == "crashed"):
        status = "crashed"
    elif end["outcome"] == "failed_run":
        status = "failed_run"
    else:
        status = "completed"
    first = accusations[0] if accusations else None
    solved_at = next((a["attempt"] for a in accusations if a["correct"]), None)
    valid = [a for a in accusations if a.get("valid", True)]
    coverage, essential = _online_coverage(events, case)
    record = {
        "game_id": (start or {}).get("game_id") or row.get("game_id"),
        "condition": (start or {}).get("condition") or row.get("condition"),
        "seed": (start or {}).get("seed", row.get("seed")),
        "model_id": (start or {}).get("model_id"),
        "k": (start or {}).get("k"),
        "status": status,
        "outcome": end["outcome"] if end else row.get("outcome"),
        "failure_stage": failures[0]["stage"] if failures else None,
        "failure_reason": failures[0]["reason"] if failures else (row.get("error") if status == "crashed" else None),
        "first_accusation_made": first is not None,
        "first_accusation_correct": bool(first and first["correct"]),
        "component_correct": end["component_correct"] if end and end.get("component_correct") else None,
        "attempts_used": len(accusations),
        "solved_at": solved_at,
        "solved_within_2": solved_at is not None and solved_at <= 2,
        "solved_within_3": solved_at is not None and solved_at <= 3,
        "valid_accusations": len(valid),
        "wrong_valid_accusations": sum(1 for a in valid if not a["correct"]),
        "interrogations_before_first_accusation": first["interrogations_used"] if first else (end or {}).get("interrogations_used"),
        "interrogations_used": (end or {}).get("interrogations_used"),
        "exchanges_total": (end or {}).get("exchanges_total"),
        "checkpoints_reached": (end or {}).get("checkpoints_reached"),
        "invitations_sent": ((end or {}).get("invitations") or {}).get("sent"),
        "invitations_accepted": ((end or {}).get("invitations") or {}).get("accepted"),
        "invitations_rejected": ((end or {}).get("invitations") or {}).get("rejected"),
        "detective_conversations": ((end or {}).get("conversations") or {}).get("detective"),
        "private_conversations": ((end or {}).get("conversations") or {}).get("private"),
        "contradiction_pairs_reported": next((len(e["reported_pairs_raw"]) for e in events
                                             if e["kind"] == "contradiction_report"), None),
        "llm_calls": len(calls),
        "total_tokens": cost["total_tokens"],
        "cost_usd": cost["cost_usd"],
        "tokens_through_first_accusation": by_phase["through_first_accusation"]["total_tokens"],
        "cost_through_first_accusation": by_phase["through_first_accusation"]["cost_usd"],
        "cost_accusation_retries": by_phase["accusation_retries"]["cost_usd"],
        "cost_post_game_analysis": by_phase["post_game_analysis"]["cost_usd"],
        "transport_retries": sum(1 for c in calls for a in c["attempts"] if a["kind"] == "transport_retry"),
        "format_repairs": sum(1 for c in calls for a in c["attempts"] if a["kind"] == "format_repair"),
        "controller_repairs": cost["controller_repair_calls"]["calls"],
        "truncated_calls": sum(1 for c in calls if c.get("finish_reason") == "MAX_TOKENS"),
        "reasoning_tokens": sum(c["usage"]["reasoning_tokens"] for c in calls),
        "duration_s": (end or {}).get("duration_s", row.get("duration_s")),
        "online_evidence_coverage": coverage,
        "online_essential_coverage": essential,
        "validation_passed": validate_events(events)["passed"] if events else None,
    }
    return record


def crashed_record(row):
    """A results row whose game left no log (the controller crashed before game_start)."""
    return {"game_id": row.get("game_id"), "condition": row.get("condition"), "seed": row.get("seed"),
            "model_id": None, "k": None, "status": "crashed", "outcome": None,
            "failure_stage": "software_crash", "failure_reason": row.get("error"), "first_accusation_made": False,
            "first_accusation_correct": False, "component_correct": None, "attempts_used": 0, "solved_at": None,
            "solved_within_2": False, "solved_within_3": False, "valid_accusations": 0,
            "wrong_valid_accusations": 0, "interrogations_before_first_accusation": None,
            "interrogations_used": None, "exchanges_total": None, "checkpoints_reached": None,
            "invitations_sent": None, "invitations_accepted": None, "invitations_rejected": None,
            "detective_conversations": None, "private_conversations": None, "contradiction_pairs_reported": None,
            "llm_calls": 0, "total_tokens": 0, "cost_usd": None, "tokens_through_first_accusation": 0,
            "cost_through_first_accusation": None, "cost_accusation_retries": None, "cost_post_game_analysis": None,
            "transport_retries": 0, "format_repairs": 0, "controller_repairs": 0, "truncated_calls": 0,
            "reasoning_tokens": 0, "duration_s": row.get("duration_s"), "online_evidence_coverage": None,
            "online_essential_coverage": None, "validation_passed": None}


def collect_records(run_dir, case=None):
    """All game records of a run folder."""
    run = load_run(run_dir)
    records = []
    for row, path in game_sources(run_dir, run["rows"]):
        if path is None:
            records.append(crashed_record(row or {}))
            continue
        records.append(game_record(read_events(path), row, case))
    return records


# ----------------------------------------------------------------------------- statistics

def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def proportion(flags):
    flags = list(flags)
    return sum(1 for f in flags if f) / len(flags) if flags else None


def pooled_ratio(numerators, denominators):
    denominator = sum(denominators)
    return sum(numerators) / denominator if denominator else None


def percentile(sorted_values, q):
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * q
    low, high = int(position), min(int(position) + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def bootstrap_ci(records, statistic, replicates, rng, level=0.95):
    """Percentile bootstrap of statistic(records) by resampling records with replacement."""
    if not records:
        return [None, None]
    values = []
    n = len(records)
    for _ in range(replicates):
        sample = [records[rng.randrange(n)] for _ in range(n)]
        value = statistic(sample)
        if value is not None:
            values.append(value)
    if not values:
        return [None, None]
    values.sort()
    alpha = (1 - level) / 2
    return [percentile(values, alpha), percentile(values, 1 - alpha)]


def completed(records):
    return [r for r in records if r["status"] == "completed"]


def solved(records):
    return [r for r in records if r["solved_at"] is not None]


# Each metric: name -> (statistic over a list of records, description). Operational rates use every
# scheduled game; "_completed" variants and conditional means state their own denominator.
METRICS = {
    "first_accusation_accuracy": (lambda rs: proportion(r["first_accusation_correct"] for r in rs),
                                  "A1: first accusation exactly right; failed and crashed games count as wrong"),
    "first_accusation_accuracy_completed": (lambda rs: proportion(r["first_accusation_correct"] for r in completed(rs)),
                                            "A1 among games that reached a verdict (completed games only)"),
    "culprit_accuracy": (lambda rs: proportion(bool(r["component_correct"] and r["component_correct"]["culprit"]) for r in rs),
                         "First-attempt culprit right (operational)"),
    "weapon_accuracy": (lambda rs: proportion(bool(r["component_correct"] and r["component_correct"]["weapon"]) for r in rs),
                        "First-attempt weapon right (operational)"),
    "location_accuracy": (lambda rs: proportion(bool(r["component_correct"] and r["component_correct"]["location"]) for r in rs),
                          "First-attempt location right (operational)"),
    "solve_rate_within_2": (lambda rs: proportion(r["solved_within_2"] for r in rs), "A2: solved within two accusations"),
    "solve_rate_within_3": (lambda rs: proportion(r["solved_within_3"] for r in rs), "A3: eventual solve rate (three accusations)"),
    "wrong_valid_accusation_rate": (lambda rs: pooled_ratio([r["wrong_valid_accusations"] for r in rs],
                                                            [r["valid_accusations"] for r in rs]),
                                    "Wrong valid accusations over all valid accusations (pooled)"),
    "attempts_used_mean_solved": (lambda rs: mean(r["solved_at"] for r in solved(rs)),
                                  "Accusations used among solved games"),
    "failed_run_rate": (lambda rs: proportion(r["status"] == "failed_run" for r in rs), "Model-side failed runs"),
    "crash_rate": (lambda rs: proportion(r["status"] == "crashed" for r in rs), "Software crashes (orchestration accounting)"),
    "interrogations_before_first_accusation_mean": (lambda rs: mean(r["interrogations_before_first_accusation"] for r in rs),
                                                    "Information-gathering turns before the first accusation"),
    "exchanges_total_mean": (lambda rs: mean(r["exchanges_total"] for r in rs), "Detective-visible exchanges per game"),
    "checkpoints_reached_mean": (lambda rs: mean(r["checkpoints_reached"] for r in rs), "Checkpoints reached per game"),
    "invitations_sent_mean": (lambda rs: mean(r["invitations_sent"] for r in rs), "Invitations per game"),
    "invitation_acceptance_rate": (lambda rs: pooled_ratio([r["invitations_accepted"] or 0 for r in rs],
                                                           [r["invitations_sent"] or 0 for r in rs]),
                                   "Accepted over sent invitations (pooled)"),
    "detective_conversations_mean": (lambda rs: mean(r["detective_conversations"] for r in rs),
                                     "Suspect-initiated conversations with the detective per game"),
    "private_conversations_mean": (lambda rs: mean(r["private_conversations"] for r in rs),
                                   "Suspect-only conversations per game"),
    "contradiction_pairs_reported_mean": (lambda rs: mean(r["contradiction_pairs_reported"] for r in rs),
                                          "Pairs in the contradiction report (unscored until annotation)"),
    "llm_calls_mean": (lambda rs: mean(r["llm_calls"] for r in rs), "LLM calls per game"),
    "total_tokens_mean": (lambda rs: mean(r["total_tokens"] for r in rs), "Tokens per game, all roles"),
    "reasoning_tokens_mean": (lambda rs: mean(r["reasoning_tokens"] for r in rs), "Billed thinking tokens per game"),
    "cost_usd_mean": (lambda rs: mean(r["cost_usd"] for r in rs), "Cost per game, USD, all roles and phases"),
    "cost_through_first_accusation_mean": (lambda rs: mean(r["cost_through_first_accusation"] for r in rs),
                                           "Cost per game up to and including the first accusation"),
    "cost_accusation_retries_mean": (lambda rs: mean(r["cost_accusation_retries"] for r in rs),
                                     "Retry cost per game, zero for games without retries"),
    "transport_retries_mean": (lambda rs: mean(r["transport_retries"] for r in rs), "Transport retries per game"),
    "format_repairs_mean": (lambda rs: mean(r["format_repairs"] for r in rs), "Wrapper format repairs per game"),
    "controller_repairs_mean": (lambda rs: mean(r["controller_repairs"] for r in rs), "Controller-side detective repairs per game"),
    "truncated_calls_mean": (lambda rs: mean(r["truncated_calls"] for r in rs), "Calls cut off at the output cap per game"),
    "duration_s_mean": (lambda rs: mean(r["duration_s"] for r in rs), "Wall-clock seconds per game"),
    "online_evidence_coverage_mean": (lambda rs: mean(r["online_evidence_coverage"] for r in rs),
                                      "Heuristic: gold evidence matched online by the controller (D11), not label-based"),
    "online_essential_coverage_mean": (lambda rs: mean(r["online_essential_coverage"] for r in rs),
                                       "Heuristic: decisive trace clues matched online (D11)"),
}

DIFFERENCES = (("ledger", "raw"), ("ledger", "none"), ("raw", "none"))
DIFFERENCE_METRICS = ("first_accusation_accuracy", "solve_rate_within_3", "interrogations_before_first_accusation_mean",
                      "cost_usd_mean", "online_evidence_coverage_mean")


def aggregate(records, replicates=2000, seed=0, conditions=None):
    """Per-condition point estimates with 95% bootstrap intervals, plus pairwise differences."""
    rng = random.Random(seed)
    conditions = list(conditions or [c for c in CONDITIONS if any(r["condition"] == c for r in records)]
                      or sorted({r["condition"] for r in records if r["condition"]}))
    by_condition = {c: [r for r in records if r["condition"] == c] for c in conditions}
    result = {"bootstrap": {"replicates": replicates, "seed": seed, "method": "percentile, games resampled within condition"},
              "conditions": {}, "differences": {}}
    for condition, group in by_condition.items():
        entry = {"games": len(group), "completed": len(completed(group)),
                 "failed_runs": sum(1 for r in group if r["status"] == "failed_run"),
                 "crashed": sum(1 for r in group if r["status"] == "crashed"), "metrics": {}}
        for name, (statistic, description) in METRICS.items():
            value = statistic(group)
            entry["metrics"][name] = {"value": value, "ci95": bootstrap_ci(group, statistic, replicates, rng),
                                      "description": description}
        result["conditions"][condition] = entry
    for a, b in DIFFERENCES:
        if a not in by_condition or b not in by_condition:
            continue
        key = f"{a}_minus_{b}"
        result["differences"][key] = {}
        for name in DIFFERENCE_METRICS:
            statistic = METRICS[name][0]
            point_a, point_b = statistic(by_condition[a]), statistic(by_condition[b])
            point = None if point_a is None or point_b is None else point_a - point_b
            ga, gb = by_condition[a], by_condition[b]
            diffs = []
            for _ in range(replicates):
                sa = [ga[rng.randrange(len(ga))] for _ in range(len(ga))] if ga else []
                sb = [gb[rng.randrange(len(gb))] for _ in range(len(gb))] if gb else []
                va, vb = statistic(sa), statistic(sb)
                if va is not None and vb is not None:
                    diffs.append(va - vb)
            diffs.sort()
            result["differences"][key][name] = {"value": point,
                                                "ci95": [percentile(diffs, 0.025), percentile(diffs, 0.975)] if diffs else [None, None]}
    return result


# ----------------------------------------------------------------------------- outputs

def _fmt(value, digits=3):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _fmt_ci(ci, digits=3):
    return "n/a" if ci[0] is None else f"[{ci[0]:.{digits}f}, {ci[1]:.{digits}f}]"


HEADLINE = ("first_accusation_accuracy", "first_accusation_accuracy_completed", "culprit_accuracy", "weapon_accuracy",
            "location_accuracy", "solve_rate_within_2", "solve_rate_within_3", "wrong_valid_accusation_rate",
            "attempts_used_mean_solved", "failed_run_rate", "crash_rate")
EFFICIENCY = ("interrogations_before_first_accusation_mean", "exchanges_total_mean", "checkpoints_reached_mean",
              "invitations_sent_mean", "invitation_acceptance_rate", "detective_conversations_mean",
              "private_conversations_mean", "contradiction_pairs_reported_mean", "online_evidence_coverage_mean",
              "online_essential_coverage_mean")
COST = ("llm_calls_mean", "total_tokens_mean", "reasoning_tokens_mean", "cost_usd_mean",
        "cost_through_first_accusation_mean", "cost_accusation_retries_mean", "transport_retries_mean",
        "format_repairs_mean", "controller_repairs_mean", "truncated_calls_mean", "duration_s_mean")


def markdown_report(summary, title="Metrics"):
    conditions = list(summary["conditions"])
    lines = [f"# {title}", "",
             f"Bootstrap: {summary['bootstrap']['replicates']} replicates, seed {summary['bootstrap']['seed']}, "
             f"{summary['bootstrap']['method']}. Operational rates count failed and crashed games as unsuccessful.", ""]
    lines += ["| condition | games | completed | failed runs | crashed |", "|---|---|---|---|---|"]
    for c in conditions:
        e = summary["conditions"][c]
        lines.append(f"| {c} | {e['games']} | {e['completed']} | {e['failed_runs']} | {e['crashed']} |")
    for heading, names, digits in (("Outcomes", HEADLINE, 3), ("Interaction and efficiency", EFFICIENCY, 2),
                                   ("Cost and reliability", COST, 4)):
        lines += ["", f"## {heading}", "", "| metric | " + " | ".join(conditions) + " |",
                  "|---|" + "---|" * len(conditions)]
        for name in names:
            cells = []
            for c in conditions:
                m = summary["conditions"][c]["metrics"][name]
                cells.append(f"{_fmt(m['value'], digits)} {_fmt_ci(m['ci95'], digits)}")
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
    if summary["differences"]:
        lines += ["", "## Differences between conditions (point estimate, 95% bootstrap CI)", "",
                  "| metric | " + " | ".join(summary["differences"]) + " |", "|---|" + "---|" * len(summary["differences"])]
        for name in DIFFERENCE_METRICS:
            cells = [f"{_fmt(d[name]['value'])} {_fmt_ci(d[name]['ci95'])}" for d in summary["differences"].values()]
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "Metric descriptions are in metrics.json. Repeated questions, label-based evidence coverage and "
                  "contradiction precision/recall need the post-game annotation and are not in this table."]
    return "\n".join(lines) + "\n"


def write_outputs(summary, records, out_dir, title="Metrics"):
    """metrics.json, per_game.csv and metrics.md in out_dir; returns their paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"metrics_json": out_dir / "metrics.json", "per_game_csv": out_dir / "per_game.csv",
             "metrics_md": out_dir / "metrics.md"}
    paths["metrics_json"].write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if records:
        fields = list(records[0].keys())
        with paths["per_game_csv"].open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for record in records:
                writer.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in record.items()})
    paths["metrics_md"].write_text(markdown_report(summary, title), encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}

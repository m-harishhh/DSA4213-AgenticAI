"""Seeded run orchestration over controller.interfaces.run_game (or the mock game).

A run is a folder runs/<run_id>/ holding run.json (the plan, written once), one event
log per game, the per-game transcript and annotation export, and results.jsonl (one
row per finished game, appended as games finish). Re-running the same folder resumes:
finished plan entries are skipped. A cost ceiling stops new games from starting; games
already in flight finish. Crashes inside run_game are recorded as rows with status
"crashed" so operational accounting keeps every scheduled game.
"""

import concurrent.futures
import datetime
import hashlib
import json
import random
import threading
import time
from pathlib import Path

from .eventlog import read_events, validate_events, summarise_cost
from .replay import write_game_artifacts

CONDITIONS = ("none", "raw", "ledger")


def derive_seed(master_seed, condition, repeat):
    """Stable per-game seed from (master seed, condition, repeat index)."""
    digest = hashlib.sha256(f"{master_seed}:{condition}:{repeat}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def plan_games(master_seed, conditions, runs_per_condition):
    """All (condition, repeat) games in a condition order shuffled by the master seed (proposal 5.5)."""
    games = [{"plan_index": index, "condition": condition, "repeat": repeat,
              "seed": derive_seed(master_seed, condition, repeat)}
             for index, (condition, repeat) in enumerate((c, r) for c in conditions for r in range(runs_per_condition))]
    order = list(range(len(games)))
    random.Random(master_seed).shuffle(order)
    for position, index in enumerate(order):
        games[index]["order"] = position
    return sorted(games, key=lambda game: game["order"])


def case_sha256(case_path):
    case = json.loads(Path(case_path).read_text(encoding="utf-8"))
    return hashlib.sha256(json.dumps(case, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Run:
    """One run folder: manifest, results and the lock that serialises result writes."""

    def __init__(self, run_dir, manifest):
        self.dir = Path(run_dir)
        self.manifest = manifest
        self.results_path = self.dir / "results.jsonl"
        self._lock = threading.Lock()

    @classmethod
    def create(cls, run_dir, *, run_id, master_seed, case_path, config, conditions=CONDITIONS,
               runs_per_condition=None, backend="mock", game_impl="mock", max_cost_usd=None, code_version=None):
        run_dir = Path(run_dir)
        if (run_dir / "run.json").exists():
            raise FileExistsError(f"{run_dir} already holds a run; open it to resume")
        runs_per_condition = int(runs_per_condition or config.get("runs_per_condition") or 1)
        manifest = {"run_id": run_id, "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    "master_seed": master_seed, "case_path": str(case_path), "case_sha256": case_sha256(case_path),
                    "config": config, "conditions": list(conditions), "runs_per_condition": runs_per_condition,
                    "backend": backend, "game_impl": game_impl, "max_cost_usd": max_cost_usd,
                    "code_version": code_version, "plan": plan_games(master_seed, conditions, runs_per_condition)}
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return cls(run_dir, manifest)

    @classmethod
    def open(cls, run_dir):
        run_dir = Path(run_dir)
        return cls(run_dir, json.loads((run_dir / "run.json").read_text(encoding="utf-8")))

    @property
    def config(self):
        config = dict(self.manifest["config"])
        config.setdefault("run_id", self.manifest["run_id"])
        config.setdefault("case_sha256", self.manifest["case_sha256"])
        return config

    def results(self):
        if not self.results_path.exists():
            return []
        with self.results_path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def append_result(self, row):
        with self._lock:
            with self.results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
                handle.flush()

    def remaining(self):
        done = {row["plan_index"] for row in self.results()}
        return [game for game in self.manifest["plan"] if game["plan_index"] not in done]

    def cost_so_far(self):
        return sum(row["cost_usd"] or 0.0 for row in self.results())


def play_one(run, game, run_game, make_llm):
    """Run one planned game, write its artifacts and append its results row."""
    started = time.time()
    llm = make_llm(game)
    status, error, result = "ok", None, {}
    try:
        result = run_game(run.manifest["case_path"], run.config, game["seed"], game["condition"], llm,
                          log_dir=str(run.dir)) or {}
    except Exception as exc:  # a crashed game is a scheduled game that failed: keep it in the accounting
        status, error = "crashed", f"{type(exc).__name__}: {exc}"[:500]
    row = {"plan_index": game["plan_index"], "order": game["order"], "condition": game["condition"],
           "repeat": game["repeat"], "seed": game["seed"], "game_id": result.get("game_id"),
           "log_path": result.get("log_path"), "status": status, "error": error,
           "outcome": result.get("outcome"), "first_accusation_correct": result.get(
               "first_accusation_correct", result.get("first_accusations_correct")),
           "attempts_used": None, "interrogations_used": None, "exchanges_total": None,
           "total_tokens": None, "cost_usd": None, "validation_passed": None, "validation_errors": [],
           "transcript_path": None, "exchanges_path": None, "duration_s": None}
    log_path = result.get("log_path") or getattr(getattr(llm, "log", None), "path", None)
    if log_path and Path(log_path).exists():
        events = read_events(log_path)
        row["log_path"] = str(log_path)
        validation = validate_events(events)
        row["validation_passed"], row["validation_errors"] = validation["passed"], validation["errors"][:5]
        end = next((event for event in events if event["kind"] == "game_end"), None)
        cost = end["cost"] if end else summarise_cost(events)
        row["total_tokens"], row["cost_usd"] = cost["total_tokens"], cost["cost_usd"]
        if end:
            row.update({"outcome": end["outcome"], "first_accusation_correct": end["first_accusation_correct"],
                        "attempts_used": end["attempts_used"], "interrogations_used": end["interrogations_used"],
                        "exchanges_total": end["exchanges_total"]})
        elif status == "ok":
            row["status"] = "incomplete"
        try:
            artifacts = write_game_artifacts(log_path)
            row["transcript_path"], row["exchanges_path"] = artifacts["transcript"], artifacts["exchanges"]
        except Exception as exc:  # artifacts are a convenience; the log is the record
            row["validation_errors"].append(f"artifacts: {type(exc).__name__}: {exc}"[:200])
    row["duration_s"] = round(time.time() - started, 3)
    run.append_result(row)
    return row


def execute(run, run_game, make_llm, workers=1, limit=None, max_cost_usd=None, progress=None):
    """Play the remaining planned games, up to `limit`, with `workers` threads, under the cost ceiling."""
    pending = run.remaining()
    if limit is not None:
        pending = pending[:limit]
    ceiling = max_cost_usd if max_cost_usd is not None else run.manifest.get("max_cost_usd")
    rows, stopped_for_budget = [], False
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        in_flight = set()
        queue = list(pending)
        while queue or in_flight:
            while queue and len(in_flight) < max(1, workers):
                if ceiling is not None and run.cost_so_far() >= ceiling:
                    stopped_for_budget = True
                    queue.clear()
                    break
                game = queue.pop(0)
                in_flight.add(pool.submit(play_one, run, game, run_game, make_llm))
            if not in_flight:
                break
            done, in_flight = concurrent.futures.wait(in_flight, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                row = future.result()
                rows.append(row)
                if progress:
                    progress(row)
    summary = summarise(run.results())
    summary.update({"played_now": len(rows), "remaining": len(run.remaining()), "stopped_for_budget": stopped_for_budget,
                    "cost_ceiling_usd": ceiling})
    return summary


def summarise(rows):
    """Per-condition counts, operational first-accusation accuracy, cost and tokens."""
    by_condition = {}
    for row in rows:
        bucket = by_condition.setdefault(row["condition"], {"games": 0, "ok": 0, "failed_run": 0, "crashed": 0,
                                                             "first_correct": 0, "total_tokens": 0, "cost_usd": 0.0,
                                                             "validation_failures": 0})
        bucket["games"] += 1
        if row["status"] == "crashed":
            bucket["crashed"] += 1
        elif row["outcome"] == "failed_run":
            bucket["failed_run"] += 1
        else:
            bucket["ok"] += 1
        bucket["first_correct"] += 1 if row.get("first_accusation_correct") else 0
        bucket["total_tokens"] += row.get("total_tokens") or 0
        bucket["cost_usd"] += row.get("cost_usd") or 0.0
        bucket["validation_failures"] += 1 if row.get("validation_passed") is False else 0
    for bucket in by_condition.values():
        # Operational accuracy: failed and crashed games count as unsuccessful (proposal 5.4).
        bucket["first_accusation_accuracy"] = bucket["first_correct"] / bucket["games"] if bucket["games"] else None
    return {"games": len(rows), "by_condition": by_condition,
            "cost_usd": sum(b["cost_usd"] for b in by_condition.values()),
            "total_tokens": sum(b["total_tokens"] for b in by_condition.values())}

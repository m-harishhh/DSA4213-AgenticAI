"""Many-game annotation orchestration.

Deliberately mirrors infra.orchestrate's Run/execute shape: reads a completed run's
results.jsonl (infra.orchestrate.Run, normally from the same directory annotation
output is written to, but annotate_run's source_dir can point elsewhere), writes its
own resumable annotation_results.jsonl into the run directory, and never silently drops
a game from accounting -- a failed or crashed annotation call is still recorded as a
scheduled attempt (same operational-accounting principle as infra.orchestrate.play_one,
and ADH-2 in qa/blackbox_requirements.md). A missing results.jsonl raises loudly
(game_rows) rather than being treated as zero games to annotate.
"""

import json
import random
import threading
import time
from pathlib import Path

from casework import load_case
from infra import LLMCallError

from .pipeline import annotate_game

RESULTS_FILENAME = "annotation_results.jsonl"


def _read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def game_rows(source_dir):
    """Rows from the gameplay run's own results.jsonl (infra.orchestrate.Run), one per
    scheduled game. Raises FileNotFoundError with a specific message if results.jsonl
    is missing -- this is a REQUIRED input (the list of games to annotate), not an
    append-only output file that legitimately starts empty (contrast
    AnnotationRun.results, which correctly uses _read_jsonl's empty-list default for
    exactly that reason: its own output file not existing yet means "nothing annotated
    yet", a valid state).

    Confirmed as a real, silent bug: before this check, a missing results.jsonl (e.g.
    because annotation output lives in a different directory than where the games were
    actually played -- see annotate_run's source_dir parameter) made annotate_run()
    report planned=0, failed=0 with no error at all, indistinguishable from "already
    fully annotated, nothing to do."
    """
    path = Path(source_dir) / "results.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist. This is the gameplay run's required results.jsonl "
            f"(infra.orchestrate.Run's output, listing every game to annotate) -- if the "
            f"games were played into a different directory than where annotation output "
            f"should be written, pass source_dir to annotate_run separately from run_dir.")
    return _read_jsonl(path)


def select_sample(rows, sample_size=None, balanced=True, seed=0):
    """All rows with a log by default -- the proposal's stated annotation target (§5.4).

    With sample_size set and smaller than the available rows: a balanced sample draws
    sample_size // (number of conditions present) from each condition, topping up from
    the remainder if it doesn't divide evenly, so no condition is silently dropped to
    zero. balanced=False draws uniformly at random across all rows instead.
    """
    rows = [row for row in rows if row.get("log_path")]
    if sample_size is None or sample_size >= len(rows):
        return rows
    rng = random.Random(seed)
    if not balanced:
        return rng.sample(rows, sample_size)
    by_condition = {}
    for row in rows:
        by_condition.setdefault(row["condition"], []).append(row)
    conditions = sorted(by_condition)
    per_condition = sample_size // len(conditions)
    if per_condition == 0:
        return rng.sample(rows, sample_size)
    chosen = []
    for condition in conditions:
        pool = by_condition[condition]
        chosen += rng.sample(pool, min(per_condition, len(pool)))
    remaining = sample_size - len(chosen)
    if remaining > 0:
        chosen_ids = {(row["game_id"]) for row in chosen}
        leftover = [row for row in rows if row["game_id"] not in chosen_ids]
        chosen += rng.sample(leftover, min(remaining, len(leftover)))
    return chosen


class AnnotationRun:
    """The annotation-side counterpart to infra.orchestrate.Run: one results file per
    run directory, resumable, append-only."""

    def __init__(self, run_dir):
        self.dir = Path(run_dir)
        self.results_path = self.dir / RESULTS_FILENAME
        self._lock = threading.Lock()

    def results(self):
        return _read_jsonl(self.results_path)

    def append_result(self, row):
        with self._lock:
            with self.results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
                handle.flush()

    def already_done(self):
        return {row["game_id"] for row in self.results() if row.get("status") == "ok"}


def annotate_one(run, row, case, llm):
    """Annotate one game row; returns the results row. Never raises -- failures and
    crashes are caught and recorded, same principle as infra.orchestrate.play_one."""
    started = time.time()
    game_id = row["game_id"]
    status, error, cost_usd = "ok", None, None
    exchanges_labelled = missed = coordination = None
    try:
        record, stored = annotate_game(row["log_path"], case, llm, game_id=game_id)
        cost_usd = record["cost_usd"]
        exchanges_labelled = len(stored["exchange_behaviour"])
        missed = len(stored["missed_required_disclosures"])
        coordination = len(stored["voluntary_interaction"]["coordination_instances"])
    except LLMCallError as exc:
        status, error, cost_usd = exc.record["status"], str(exc), exc.record.get("cost_usd")
    except Exception as exc:  # a crashed annotation call is still a scheduled one: keep it in the accounting
        status, error = "crashed", f"{type(exc).__name__}: {exc}"[:500]

    result = {"game_id": game_id, "condition": row.get("condition"), "log_path": row["log_path"],
              "status": status, "error": error, "exchanges_labelled": exchanges_labelled,
              "missed_required_disclosures": missed, "coordination_instances": coordination,
              "cost_usd": cost_usd, "duration_s": round(time.time() - started, 3)}
    run.append_result(result)
    return result


def annotate_run(run_dir, case_path, make_llm, sample_size=None, balanced=True, seed=0, progress=None,
                 source_dir=None):
    """Annotate every (or a sample of) games already played in source_dir, writing
    annotation output into run_dir.

    make_llm: a callable (row) -> a fresh infra.llm.LLMClient, mirroring
              infra.orchestrate's make_llm_factory convention.
    source_dir: where the gameplay run's results.jsonl lives, if different from
                run_dir. Defaults to run_dir -- the historical assumption that gameplay
                and annotation output share one directory, still the common case, but
                no longer required (e.g. annotation/main_01 holds this run's own
                annotation output but not the gameplay run's results.jsonl, which was
                never kept in this directory in the first place).
    Resumable: a game already recorded with status "ok" is skipped; a prior
    crashed/failed attempt is retried.
    """
    run = AnnotationRun(run_dir)
    case = load_case(case_path)
    rows = select_sample(game_rows(source_dir or run_dir), sample_size=sample_size, balanced=balanced, seed=seed)
    done = run.already_done()
    pending = [row for row in rows if row["game_id"] not in done]

    results = []
    for row in pending:
        result = annotate_one(run, row, case, make_llm(row))
        results.append(result)
        if progress:
            progress(result)
    return {"planned": len(rows), "already_done": len(rows) - len(pending), "annotated_now": len(results),
           "failed": sum(1 for r in results if r["status"] != "ok"),
           "cost_usd": sum(r["cost_usd"] or 0.0 for r in results)}

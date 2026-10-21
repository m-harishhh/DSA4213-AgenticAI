"""Orchestration commands (see infra/README.md):

    python -m infra run --config configs/experiment_config.final.json --case cases/evaluation.json \\
        --run-id pilot_01 --seed 1 --games-per-condition 3 --workers 3 --backend gemini --game controller
    python -m infra run --config configs/experiment_config.example.json --case cases/pilot.json \\
        --run-dir runs/mock_demo --games-per-condition 2 --workers 2            # offline mock game
    python -m infra replay runs/mock_demo/<game>.jsonl [--write] [--no-private] [--check]
    python -m infra summary runs/mock_demo
"""

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

from .eventlog import read_events, validate_events
from .llm import LLMClient, MockBackend, GeminiBackend
from .orchestrate import Run, execute, summarise, CONDITIONS
from .pricing import pricing_for, PRICES
from .replay import render_transcript, write_game_artifacts


def code_version():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def make_llm_factory(backend_name, config):
    """One fresh LLMClient per game; run_game attaches the game's EventLog to it."""
    model_id = config.get("model_id") or "mock-model"
    decoding = config.get("decoding_settings") or {}
    pricing = pricing_for(model_id) if model_id in PRICES else None
    if backend_name == "mock":
        return lambda game: LLMClient(MockBackend(), model_id, decoding, log=None, pricing=pricing,
                                      sleep=lambda seconds: None)
    if backend_name == "gemini":
        timeout_s = float(config.get("timeout_s") or 120)
        return lambda game: LLMClient(GeminiBackend(timeout_s=timeout_s), model_id, decoding, log=None,
                                      pricing=pricing)
    raise SystemExit(f"Unknown backend {backend_name!r}")


def game_runner(name):
    if name == "mock":
        from .mockgame import run_game
        return run_game
    try:
        from controller.interfaces import run_game
    except ImportError as exc:
        raise SystemExit(f"controller.interfaces could not be imported ({exc}); use --game mock or install requirements")
    return run_game


def cmd_run(args):
    run_dir = Path(args.run_dir) if args.run_dir else Path("runs") / args.run_id
    if (run_dir / "run.json").exists():
        run = Run.open(run_dir)
        print(f"Resuming run {run.manifest['run_id']} in {run_dir} ({len(run.remaining())} games remaining)",
              file=sys.stderr)
    else:
        if not args.config or not args.case:
            raise SystemExit("--config and --case are required to create a run")
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        max_cost = args.max_cost_usd if args.max_cost_usd is not None else config.get("budget_usd")
        run = Run.create(run_dir, run_id=args.run_id, master_seed=args.seed, case_path=args.case, config=config,
                         conditions=tuple(args.conditions.split(",")), runs_per_condition=args.games_per_condition,
                         backend=args.backend, game_impl=args.game, max_cost_usd=max_cost, code_version=code_version())
        print(f"Created run {args.run_id} in {run_dir}: {len(run.manifest['plan'])} games planned", file=sys.stderr)
    if args.dry_run:
        print(json.dumps({"run_id": run.manifest["run_id"], "remaining": run.remaining()}, indent=2))
        return 0
    if args.backend == "gemini" and args.game == "mock":
        print("Note: the mock game scripts its own MockBackend and ignores --backend gemini", file=sys.stderr)

    def progress(row):
        print(f"[{row['order']:>4}] {row['condition']:6} seed {row['seed']:>10}: {row['status']:8} "
              f"{row['outcome'] or '-':13} tokens={row['total_tokens']} cost={row['cost_usd']}", file=sys.stderr)

    summary = execute(run, game_runner(args.game), make_llm_factory(args.backend, run.config), workers=args.workers,
                      limit_blocks=args.limit_blocks, max_cost_usd=args.max_cost_usd, progress=progress)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_replay(args):
    events = read_events(args.log)
    status = 0
    if args.check:
        report = validate_events(events)
        print(json.dumps(report, indent=2), file=sys.stderr)
        status = 0 if report["passed"] else 1
    if args.write:
        print(json.dumps(write_game_artifacts(args.log, include_private=not args.no_private), indent=2))
    else:
        print(render_transcript(events, include_private=not args.no_private))
    return status


def cmd_summary(args):
    run = Run.open(args.run_dir)
    summary = summarise(run.results())
    summary["remaining"] = len(run.remaining())
    print(json.dumps(summary, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m infra", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Create or resume a seeded run and play its remaining games")
    run.add_argument("--config", help="Experiment config JSON (required when creating a run)")
    run.add_argument("--case", help="Case JSON path (required when creating a run)")
    run.add_argument("--run-id", default=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    run.add_argument("--run-dir", help="Run folder; default runs/<run-id>")
    run.add_argument("--seed", type=int, default=0, help="Master seed; per-game seeds and the order derive from it")
    run.add_argument("--conditions", default=",".join(CONDITIONS))
    run.add_argument("--games-per-condition", type=int, default=None, help="Default: config runs_per_condition")
    run.add_argument("--workers", type=int, default=1, help="Parallel games (threads)")
    run.add_argument("--backend", choices=("mock", "gemini"), default="mock")
    run.add_argument("--game", choices=("mock", "controller"), default="mock",
                     help="mock: infra.mockgame.run_game; controller: controller.interfaces.run_game")
    run.add_argument("--max-cost-usd", type=float, default=None, help="Stop scheduling games past this; default config budget_usd")
    run.add_argument("--limit-blocks", type=int, default=None,
                     help="Play at most this many blocks (one game per condition) now")
    run.add_argument("--dry-run", action="store_true", help="Create or open the run and print the remaining plan")
    run.set_defaults(func=cmd_run)
    replay = sub.add_parser("replay", help="Print a game's transcript, or write its transcript and annotation export")
    replay.add_argument("log")
    replay.add_argument("--write", action="store_true")
    replay.add_argument("--no-private", action="store_true", help="Omit suspect-only conversations")
    replay.add_argument("--check", action="store_true", help="Also validate the event log")
    replay.set_defaults(func=cmd_replay)
    summary = sub.add_parser("summary", help="Per-condition summary of a run's results")
    summary.add_argument("run_dir")
    summary.set_defaults(func=cmd_summary)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

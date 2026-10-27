"""Annotation CLI (see annotation/README.md):

    python -m annotation run --run-dir runs/pilot_01 --case cases/evaluation.json \\
        --backend gemini                                   # annotate every game (proposal's 300-game target)
    python -m annotation run --run-dir runs/pilot_01 --case cases/evaluation.json \\
        --backend gemini --sample-size 30                   # a smaller balanced sample instead
    python -m annotation run --run-dir runs/pilot_01 --case cases/evaluation.json \\
        --backend openrouter                                # same model, routed via OpenRouter instead
                                                              # (needs OPENROUTER_API_KEY, not GEMINI_API_KEY)
    python -m annotation run --run-dir runs/mock_demo --case cases/pilot.json      # offline mock annotation
    python -m annotation summary runs/pilot_01
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from infra.llm import LLMClient, MockBackend, GeminiBackend, OpenRouterBackend
from infra.pricing import pricing_for, PRICES

from .orchestrate import AnnotationRun, annotate_run


def make_llm_factory(backend_name, model_id, decoding):
    if backend_name == "openrouter" and "/" not in model_id:
        # configs/*.json store the bare Google SDK-style id (e.g. "gemini-3.1-flash-lite");
        # OpenRouter's catalog uses "<provider>/<model>" (confirmed:
        # https://openrouter.ai/google/gemini-3.1-flash-lite, checked 26 Oct 2026). Added
        # 2026-10-26 as a fallback when the direct Google API key ran out of prepaid
        # credit mid-run -- same nominal model, routed differently, so the --config flag
        # doesn't need a separate OpenRouter-specific copy.
        model_id = f"google/{model_id}"
    pricing = pricing_for(model_id) if model_id in PRICES else None
    if backend_name == "mock":
        return lambda row: LLMClient(MockBackend(), model_id, decoding, log=None, pricing=pricing,
                                     sleep=lambda seconds: None)
    if backend_name == "gemini":
        return lambda row: LLMClient(GeminiBackend(), model_id, decoding, log=None, pricing=pricing)
    if backend_name == "openrouter":
        return lambda row: LLMClient(OpenRouterBackend(), model_id, decoding, log=None, pricing=pricing)
    raise SystemExit(f"Unknown backend {backend_name!r}")


def cmd_run(args):
    config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
    model_id = args.model_id or config.get("model_id") or "mock-model"
    decoding = config.get("decoding_settings") or {}

    def progress(row):
        print(f"[{row['game_id']}] {row['condition'] or '-':7} {row['status']:8} "
             f"exchanges={row['exchanges_labelled']} missed={row['missed_required_disclosures']} "
             f"coordination={row['coordination_instances']} cost={row['cost_usd']}", file=sys.stderr)

    summary = annotate_run(args.run_dir, args.case, make_llm_factory(args.backend, model_id, decoding),
                           sample_size=args.sample_size, balanced=not args.unbalanced, seed=args.seed,
                           progress=progress)
    print(json.dumps(summary, indent=2))
    return 0 if summary["failed"] == 0 else 1


def cmd_summary(args):
    run = AnnotationRun(args.run_dir)
    rows = run.results()
    by_status = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    print(json.dumps({"games": len(rows), "by_status": by_status,
                      "total_exchanges_labelled": sum(r["exchanges_labelled"] or 0 for r in rows),
                      "total_missed_required_disclosures": sum(r["missed_required_disclosures"] or 0 for r in rows),
                      "total_coordination_instances": sum(r["coordination_instances"] or 0 for r in rows),
                      "cost_usd": sum(r["cost_usd"] or 0.0 for r in rows)}, indent=2))
    return 0


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m annotation", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Annotate every (or a sample of) games already played in a run directory")
    run.add_argument("--run-dir", required=True, help="The infra.orchestrate run directory to read results.jsonl from")
    run.add_argument("--case", required=True, help="Case JSON path (must match the one the run was played against)")
    run.add_argument("--config", help="Experiment config JSON for model_id/decoding_settings (optional)")
    run.add_argument("--model-id", help="Overrides --config's model_id")
    run.add_argument("--backend", choices=("mock", "gemini", "openrouter"), default="mock")
    run.add_argument("--sample-size", type=int, default=None, help="Default: all games (proposal's 300-game target)")
    run.add_argument("--unbalanced", action="store_true", help="Sample uniformly at random instead of per-condition")
    run.add_argument("--seed", type=int, default=0)
    run.set_defaults(func=cmd_run)

    summary = sub.add_parser("summary", help="Summary of a run directory's annotation_results.jsonl")
    summary.add_argument("run_dir")
    summary.set_defaults(func=cmd_summary)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

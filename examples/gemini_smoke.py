"""Live smoke test of the Gemini backend: two tiny paid calls. Run from the repo root:

    GEMINI_API_KEY=... python examples/gemini_smoke.py --model gemini-2.5-flash-lite

It refuses to run without GEMINI_API_KEY. It writes runs/smoke/<timestamp>.jsonl (git-ignored)
and prints measured usage, latency, served model version and cost for each call. Expected
cost is well under one tenth of a US cent. Pass --thinking-budget 0 on 2.5 models to keep
thinking off, or --thinking-level minimal on 3.x models.
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from infra import LLMClient, GeminiBackend, EventLog, LLMCallError, pricing_for, summarise_cost  # noqa: E402

ACCEPT = {"type": "object", "required": ["accept"], "properties": {"accept": {"type": "boolean"}},
          "additionalProperties": False}


def load_dotenv(path):
    """Load KEY=VALUE lines from a git-ignored .env into the environment without overriding it."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def hint_for(message):
    if "API_KEY_INVALID" in message:
        return ("Hint: the key was rejected. Use a Google AI Studio key (aistudio.google.com) in .env; "
                "keys from other providers (for example sk-...) will not work.")
    if "NOT_FOUND" in message or "404" in message[:12]:
        return ("Hint: the model is unknown or not available to this key. Stable IDs are gemini-3.1-flash-lite "
                "and gemini-3.5-flash-lite; gemini-2.5-flash-lite is refused for new keys (checked 20 Oct 2026).")
    return "Hint: not a transient error; fix the request before retrying."


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL"),
                        help="Model ID, e.g. gemini-2.5-flash-lite (or set GEMINI_MODEL)")
    parser.add_argument("--thinking-budget", type=int, default=None, help="2.5 models: 0 disables thinking")
    parser.add_argument("--thinking-level", default=None, help="3.x models: minimal, low, medium or high")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout in seconds")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if not os.environ.get("GEMINI_API_KEY"):
        print("Refusing to run: set GEMINI_API_KEY in the environment or in a git-ignored .env at the repo root; never commit it.")
        return 2
    if not args.model:
        print("Pass --model or set GEMINI_MODEL.")
        return 2
    decoding = {"temperature": 0.0, "top_p": 0.95, "seed": 7, "max_output_tokens": 48}
    if args.thinking_budget is not None:
        decoding["thinking_budget"] = args.thinking_budget
    if args.thinking_level is not None:
        decoding["thinking_level"] = args.thinking_level
    try:
        pricing = pricing_for(args.model)
    except KeyError as exc:
        print(exc)
        pricing = None

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = ROOT / "runs" / "smoke" / f"{stamp}.jsonl"
    with EventLog(log_path, f"smoke_{stamp}") as log:
        llm = LLMClient(GeminiBackend(timeout_s=args.timeout), args.model, decoding, log=log, pricing=pricing)
        calls = [
            dict(role="detective", purpose="question", phase="through_first_accusation",
                 messages=[{"role": "system", "content": "You are a detective in a text game. Be terse."},
                           {"role": "user", "content": "Reply with the single word OK."}]),
            dict(role="suspect", agent_id="suspect_1", purpose="acceptance", phase="through_first_accusation",
                 messages=[{"role": "user", "content": "A detective invites you to talk. Answer as JSON {\"accept\": true}."}],
                 response_schema=ACCEPT),
        ]
        for spec in calls:
            try:
                record = llm.call(game_id=log.game_id, **spec)
            except LLMCallError as exc:
                record = exc.record
            except Exception as exc:  # non-retryable API errors: bad request, key, unknown model
                message = str(exc).replace(os.environ["GEMINI_API_KEY"], "[key]")[:300]
                print(f"Call failed and was not retried: {type(exc).__name__}: {message}")
                print(hint_for(message))
                return 1
            print(json.dumps({"call_id": record["call_id"], "purpose": record["purpose"], "status": record["status"],
                              "text": record["text"][:120], "parsed": record["parsed"], "usage": record["usage"],
                              "latency_ms": record["latency_ms"], "finish_reason": record["finish_reason"],
                              "model_version": record["model_version"], "cost_usd": record["cost_usd"],
                              "attempts": [(a["kind"], a["status"]) for a in record["attempts"]]}, indent=2))
        summary = summarise_cost(log.events)
    print(f"Total: {summary['total_tokens']} tokens, cost_usd={summary['cost_usd']}; log at {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline commands: python -m casework validate cases/pilot.json."""

import argparse
import hashlib
import json
from pathlib import Path
from .model import load_case, public_view
from .validation import validate_case
from .generate import write_examples


def main():
    parser = argparse.ArgumentParser(description="Offline case validation; no API calls")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="Explicitly regenerate authored examples; existing files are overwritten")
    generate.add_argument("directory")
    for command in ("validate", "public", "manifest"):
        child = sub.add_parser(command)
        child.add_argument("case")
        child.add_argument("--output")
        if command in ("validate", "manifest"):
            child.add_argument("--turn-budget", type=int, default=20)
        if command == "manifest":
            child.add_argument("--config", required=True,
                               help="Final experiment settings agreed after pilot")
    args = parser.parse_args()
    if args.command == "generate":
        write_examples(args.directory)
        print("Wrote pilot.json and evaluation.json. Validate before use.")
        return 0
    case = load_case(args.case)
    status = 0
    if args.command == "public":
        report = public_view(case)
    else:
        validation = validate_case(case, args.turn_budget)
        report = validation
        status = 0 if validation["passed"] else 1
        if args.command == "manifest" and validation["passed"]:
            config = json.loads(Path(args.config).read_text(encoding="utf-8"))
            required = ("model_id", "decoding_settings", "k", "turn_budget", "runs_per_condition",
                        "budget_usd", "annotation_sample_size", "prompt_versions")
            if any(key not in config or config[key] is None for key in required):
                parser.error("Fill every final setting in the experiment config before creating a manifest")
            if config["turn_budget"] != args.turn_budget:
                parser.error("Config turn_budget must match --turn-budget")
            for key in ("k", "turn_budget", "runs_per_condition"):
                if not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] <= 0:
                    parser.error(f"{key} must be a positive integer")
            if not isinstance(config["budget_usd"], (int, float)) or isinstance(config["budget_usd"], bool) or config["budget_usd"] <= 0:
                parser.error("budget_usd must be an agreed positive amount; USD15 is not enforced")
            if (not isinstance(config["annotation_sample_size"], int)
                    or isinstance(config["annotation_sample_size"], bool)
                    or config["annotation_sample_size"] < 0
                    or config["annotation_sample_size"] > 3*config["runs_per_condition"]):
                parser.error("annotation_sample_size must be between 0 and the number of scheduled games")
            if not config["model_id"] or not config["prompt_versions"]:
                parser.error("A model version and prompt versions are required")
            canonical = json.dumps(case, sort_keys=True, separators=(",", ":")).encode()
            module_dir = Path(__file__).parent
            hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted(module_dir.glob("*.py"))}
            report = {"case_id": case["case_id"], "case_sha256": hashlib.sha256(canonical).hexdigest(),
                      "casework_source_sha256": hashes, "config": config,
                      "validation": validation,
                      "note": "Reproducibility record, not a filesystem lock. Preserve these inputs; changes require a new manifest. No paid experiment was run."}
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path}")
    else:
        print(text)
    return status


if __name__ == "__main__":
    raise SystemExit(main())

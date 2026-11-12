"""
Figures for the final report (Appendix C). Local only: this folder is excluded through .git/info/exclude.

Run from the repository root, after merging main (it needs infra/metrics.py) and with the raw logs in runs/:

    python -m pip install matplotlib==3.11.2
    python -m infra metrics runs/main_01 --case cases/evaluation.json --bootstrap 2000 \
        --out report_figures/out/metrics --no-figures
    python report_figures/make_figures.py                 # all figures
    python report_figures/make_figures.py --only 2 3      # selected figures
    python report_figures/make_figures.py --metrics path/to/metrics.json

Figures 2 to 5 read the percentile-bootstrap summary written by `python -m infra metrics`.
Figures 6 and 7 are computed directly from the event logs (runs/main_01 and runs/pilot_*).
Output: report_figures/out/figureN.png (300 dpi) and figureN.pdf.
"""
import argparse
import glob
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from infra.eventlog import read_events  # noqa: E402

OUT = ROOT / "report_figures" / "out"
CONDITIONS = ("none", "raw", "ledger")
LABELS = {"none": "No memory", "raw": "Raw history", "ledger": "Ledger"}
COLOURS = {"none": "#999999", "raw": "#0072B2", "ledger": "#D55E00"}   # colour-blind-safe

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Times", "DejaVu Serif"],
    "font.size": 10,
    "axes.titlesize": 10,
    "axes.labelsize": 10,
    "legend.fontsize": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
})


def save(fig, number):
    OUT.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(OUT / f"figure{number}.{ext}")
    plt.close(fig)
    print(f"figure{number} written to {OUT}")


def load_metrics(path):
    path = Path(path)
    if not path.exists():
        sys.exit(f"{path} not found. Run `python -m infra metrics runs/main_01 --case cases/evaluation.json "
                 f"--out {path.parent} --no-figures` first (needs infra/metrics.py from main).")
    return json.loads(path.read_text())


def metric(m, condition, name):
    entry = m["conditions"][condition]["metrics"][name]
    return entry["value"], entry["ci95"]


def error_bars(values_cis):
    lo = [v - ci[0] for v, ci in values_cis]
    hi = [ci[1] - v for v, ci in values_cis]
    return [lo, hi]


def annotate(ax, bars, fmt="{:.0f}%", scale=100, dy=0.8):
    for bar in bars:
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + dy, fmt.format(bar.get_height() * scale
                if scale != 100 else bar.get_height()), ha="center", va="bottom", fontsize=8)


# ----------------------------------------------------------------------------------------------
def figure1():
    """System diagram: one decision cycle plus the checkpoint branch."""
    fig, ax = plt.subplots(figsize=(7.4, 4.1))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 56)
    ax.axis("off")

    def box(x, y, w, h, text, fc="#FFFFFF", ec="#333333", bold=False, size=9):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.25,rounding_size=1.2",
                                    fc=fc, ec=ec, lw=1.0))
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size,
                fontweight="bold" if bold else "normal", linespacing=1.2)

    def arrow(p, q, style="-|>", rad=0.0, ls="-", colour="#333333"):
        ax.add_patch(FancyArrowPatch(p, q, arrowstyle=style, mutation_scale=9, lw=1.0, color=colour,
                                     connectionstyle=f"arc3,rad={rad}", linestyle=ls))

    # top row: the interrogation cycle
    box(2, 38, 17, 12, "Detective\n(LLM agent)", fc="#DCEBF7", bold=True)
    box(27, 38, 20, 12, "Controller\nvalidates action;\none repair allowed", fc="#F2F2F2", bold=True)
    box(55, 38, 17, 12, "Interpreter\n(LLM)\nquestion to intent")
    box(80, 38, 17, 12, "Suspect (LLM)\npicks a permitted\nanswer")
    arrow((19, 44), (27, 44))
    arrow((47, 44), (55, 44))
    arrow((72, 44), (80, 44))
    ax.text(23, 46.2, "question", fontsize=7.5, ha="center")
    ax.text(51, 46.2, "intent", fontsize=7.5, ha="center")
    ax.text(76, 46.2, "intent", fontsize=7.5, ha="center", alpha=0)

    # middle row: bookkeeping and memory
    box(80, 20, 17, 11, "Claim matching\n(updates\ndisclosure state)")
    box(55, 20, 17, 11, "Exchange\nhistory\n(e1, e2, ...)")
    box(27, 20, 20, 11, "Memory module\nnone | raw k = 8 |\nledger k = 8", fc="#FDEBD3", bold=True)
    box(27, 4, 20, 9, "Extractor (LLM)\nledger only", fc="#FDEBD3")
    arrow((88.5, 38), (88.5, 31))
    arrow((80, 25.5), (72, 25.5))
    arrow((55, 25.5), (47, 25.5))
    arrow((37, 13), (37, 20), style="<|-|>")
    arrow((37, 31), (14, 38), rad=0.15)
    ax.text(18.5, 32.5, "memory block", fontsize=7.5, ha="center", rotation=0)

    # checkpoint branch
    box(55, 2, 42, 12, "Checkpoint after interrogations 5, 10, 15\nsuspects invite; detective accepts or rejects;\n"
                      "one message, one reply;\nprivate chats never enter memory",
        fc="#E8F3E8", size=8)
    arrow((63.5, 20), (63.5, 14), style="<|-|>", ls="--")
    arrow((9, 38), (9, 8), rad=0.0, ls="--")
    ax.text(10.5, 24, "accusation\n(up to 3)", fontsize=7.5, ha="left", va="center")
    box(2, 3, 17, 6, "Scored by the\ncontroller", fc="#F2F2F2", size=8.5)
    save(fig, 1)


def figure2(m):
    """Primary outcomes with 95% bootstrap intervals."""
    names = [("first_accusation_accuracy", "First accusation correct"),
             ("solve_rate_within_3", "Solved within three accusations")]
    fig, ax = plt.subplots(figsize=(6.2, 3.4))
    width = 0.24
    for j, cond in enumerate(CONDITIONS):
        vals = [metric(m, cond, n) for n, _ in names]
        xs = [i + (j - 1) * width for i in range(len(names))]
        bars = ax.bar(xs, [v * 100 for v, _ in vals], width, color=COLOURS[cond], label=LABELS[cond],
                      yerr=[[e * 100 for e in err] for err in error_bars(vals)], capsize=3,
                      error_kw={"lw": 0.9})
        for x, (v, ci) in zip(xs, vals):
            ax.text(x, ci[1] * 100 + 1.5, f"{v * 100:.0f}%", ha="center", fontsize=8)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([label for _, label in names])
    ax.set_ylabel("Games (%)")
    ax.set_ylim(0, 85)
    ax.legend(frameon=False, loc="upper left")
    ax.set_title("n = 100 games per condition; bars show 95% bootstrap intervals", fontsize=9)
    save(fig, 2)


def figure3(m):
    """First-accusation accuracy by component."""
    names = [("culprit_accuracy", "Culprit"), ("weapon_accuracy", "Weapon"), ("location_accuracy", "Room")]
    fig, ax = plt.subplots(figsize=(6.2, 3.4))
    width = 0.24
    for j, cond in enumerate(CONDITIONS):
        vals = [metric(m, cond, n) for n, _ in names]
        xs = [i + (j - 1) * width for i in range(len(names))]
        ax.bar(xs, [v * 100 for v, _ in vals], width, color=COLOURS[cond], label=LABELS[cond],
               yerr=[[e * 100 for e in err] for err in error_bars(vals)], capsize=3, error_kw={"lw": 0.9})
        for x, (v, ci) in zip(xs, vals):
            ax.text(x, ci[1] * 100 + 1.5, f"{v * 100:.0f}%", ha="center", fontsize=8)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([label for _, label in names])
    ax.set_ylabel("First accusation correct (%)")
    ax.set_ylim(0, 100)
    ax.legend(frameon=False, loc="upper left")
    ax.set_title("Component accuracy of the first accusation; 95% bootstrap intervals", fontsize=9)
    save(fig, 3)


def figure4(m):
    """Differences between conditions with 95% intervals."""
    pairs = [("ledger_minus_raw", "Ledger − raw"), ("raw_minus_none", "Raw − none"),
             ("ledger_minus_none", "Ledger − none")]
    panels = [
        ("first_accusation_accuracy", "First accusation correct (percentage points)", 100),
        ("solve_rate_within_3", "Solved within three accusations (percentage points)", 100),
        ("interrogations_before_first_accusation_mean", "Interrogations before first accusation", 1),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(7.6, 2.8), sharey=True)
    for ax, (name, title, scale) in zip(axes, panels):
        for i, (key, label) in enumerate(pairs):
            entry = m["differences"][key][name]
            v, (lo, hi) = entry["value"] * scale, [c * scale for c in entry["ci95"]]
            ax.errorbar(v, i, xerr=[[v - lo], [hi - v]], fmt="o", color="#222222", capsize=3, lw=1.0, ms=4)
        ax.axvline(0, color="#888888", lw=0.8, ls="--")
        ax.set_yticks(range(len(pairs)))
        ax.set_yticklabels([label for _, label in pairs])
        ax.set_ylim(len(pairs) - 0.5, -0.5)
        ax.set_title(title, fontsize=8)
    fig.suptitle("Differences between conditions (point estimate and 95% bootstrap interval)", fontsize=9, y=1.02)
    fig.tight_layout()
    save(fig, 4)


def figure5(m):
    """Cost and tokens per game."""
    fig, axes = plt.subplots(1, 3, figsize=(7.6, 2.9))
    spec = [("cost_usd_mean", "Cost per game (US$)", 1, "{:.3f}"),
            ("total_tokens_mean", "Tokens per game (thousands)", 1 / 1000, "{:.0f}"),
            ("duration_s_mean", "Time per game (seconds)", 1, "{:.0f}")]
    for ax, (name, title, scale, fmt) in zip(axes, spec):
        vals = [metric(m, c, name) for c in CONDITIONS]
        ax.bar(range(3), [v * scale for v, _ in vals], color=[COLOURS[c] for c in CONDITIONS],
               yerr=[[e * scale for e in err] for err in error_bars(vals)], capsize=3, error_kw={"lw": 0.9})
        for i, (v, ci) in enumerate(vals):
            ax.text(i, ci[1] * scale * 1.02, fmt.format(v * scale), ha="center", fontsize=8)
        ax.set_xticks(range(3))
        ax.set_xticklabels([LABELS[c] for c in CONDITIONS], fontsize=8)
        ax.set_title(title, fontsize=9)
        ax.set_ylim(0, max(ci[1] for _, ci in vals) * scale * 1.18)
    fig.tight_layout()
    save(fig, 5)


# ----------------------------------------------------------------------------------------------
def game_logs(run_dir):
    for path in sorted(glob.glob(str(ROOT / run_dir / "*.jsonl"))):
        if not path.endswith("results.jsonl"):
            yield path


def figure6():
    """Failure categories per condition, counted from the main-run event logs (out of 100 games each)."""
    counts = {c: Counter() for c in CONDITIONS}
    for path in game_logs("runs/main_01"):
        events = read_events(path)
        cond = next(e for e in events if e["kind"] == "game_start")["condition"]
        end = next(e for e in events if e["kind"] == "game_end")
        counts[cond]["games"] += 1
        if not end["first_accusation_correct"]:
            counts[cond]["no correct first accusation"] += 1
        if end["interrogations_used"] >= 20:
            counts[cond]["used all 20 interrogations"] += 1
        if end["outcome"] == "failed_run":
            counts[cond]["failed run"] += 1
        if end["outcome"] == "unsolved":
            counts[cond]["unsolved after 3 accusations"] += 1
    cats = ["no correct first accusation", "used all 20 interrogations", "unsolved after 3 accusations", "failed run"]
    fig, ax = plt.subplots(figsize=(6.6, 3.5))
    width = 0.24
    for j, cond in enumerate(CONDITIONS):
        xs = [i + (j - 1) * width for i in range(len(cats))]
        ys = [counts[cond][c] for c in cats]
        ax.bar(xs, ys, width, color=COLOURS[cond], label=LABELS[cond])
        for x, y in zip(xs, ys):
            ax.text(x, y + 1.2, str(y), ha="center", fontsize=8)
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels(["No correct\nfirst accusation", "Used all 20\ninterrogations",
                        "Unsolved after\nthree accusations", "Failed run"], fontsize=8.5)
    ax.set_ylabel("Games (out of 100)")
    ax.set_ylim(0, 112)
    ax.legend(frameon=False, loc="upper right")
    save(fig, 6)
    print({c: dict(v) for c, v in counts.items()})


def pilot_facts(run_dir, case):
    """Mean number of the case's gold evidence facts that the controller recorded as delivered, per game."""
    gold = set(case["gold"]["evidence_ids"])
    per_game = []
    for path in game_logs(run_dir):
        matched = set()
        for e in read_events(path):
            if e["kind"] == "interrogation":
                matched.update(e["controller_matched"].get("claim_ids", []))
            elif e["kind"] == "conversation" and e["visibility"] == "detective":
                matched.update(e["controller_matched"].get("message_claim_ids", []))
        facts = {case["claims"][c].get("fact_id") for c in matched if c in case["claims"]}
        per_game.append(len(facts & gold))
    return per_game


def figure7():
    """Evidence facts delivered per game across the pilots that played three games per condition."""
    case = json.loads((ROOT / "cases" / "pilot.json").read_text())
    pilots = ["04a", "04b", "04c", "05a", "05b", "06", "07", "07b"]
    means, meta = [], []
    for p in pilots:
        run_dir = f"runs/pilot_{p}"
        games = pilot_facts(run_dir, case)
        start = next(e for e in read_events(next(game_logs(run_dir))) if e["kind"] == "game_start")
        means.append(sum(games) / len(games))
        meta.append((start["model_id"], start["prompt_versions"]["detective"], start["decoding"].get("thinking_level")))
    fig, ax = plt.subplots(figsize=(6.8, 3.6))
    palette = {"detective-v2": "#B0B0B0", "detective-v3": "#7FA9CF", "detective-v4": "#0072B2"}
    for i, (mean, (model, det, thinking)) in enumerate(zip(means, meta)):
        hatch = "//" if "3.5" in model else ("xx" if thinking == "low" else None)
        ax.bar(i, mean, color=palette[det], hatch=hatch, edgecolor="#333333", lw=0.6)
        ax.text(i, mean + 0.15, f"{mean:.1f}", ha="center", fontsize=8)
    ax.set_xticks(range(len(pilots)))
    ax.set_xticklabels([f"pilot_{p}" for p in pilots], rotation=30, ha="right", fontsize=8.5)
    ax.set_ylabel("Evidence facts delivered per game (of 10)")
    ax.set_ylim(0, 10)
    handles = [plt.Rectangle((0, 0), 1, 1, fc=c, ec="#333333", lw=0.6) for c in palette.values()]
    handles += [plt.Rectangle((0, 0), 1, 1, fc="white", ec="#333333", hatch="//", lw=0.6),
                plt.Rectangle((0, 0), 1, 1, fc="white", ec="#333333", hatch="xx", lw=0.6)]
    ax.legend(handles, ["detective-v2", "detective-v3", "detective-v4", "gemini-3.5-flash-lite",
                        "thinking level low"], frameon=False, fontsize=8, loc="upper left", ncol=2)
    ax.set_title("Pilot case, 9 games per pilot (3 per condition), controller's online claim matching", fontsize=9)
    save(fig, 7)
    print(dict(zip(pilots, [round(x, 1) for x in means])))


# ----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", default=str(OUT / "metrics" / "metrics.json"))
    ap.add_argument("--only", nargs="*", type=int, default=[1, 2, 3, 4, 5, 6, 7])
    args = ap.parse_args()
    needs_metrics = {2, 3, 4, 5} & set(args.only)
    m = load_metrics(args.metrics) if needs_metrics else None
    jobs = {1: figure1, 2: lambda: figure2(m), 3: lambda: figure3(m), 4: lambda: figure4(m),
            5: lambda: figure5(m), 6: figure6, 7: figure7}
    for n in args.only:
        jobs[n]()


if __name__ == "__main__":
    main()

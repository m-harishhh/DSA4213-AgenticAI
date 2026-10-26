"""Figures for the slides from a metrics summary (infra.metrics.aggregate output).

matplotlib is imported only here, so the rest of infra stays standard-library. Every
figure is written as PNG and SVG. Conditions keep fixed colours in a fixed order (none,
raw, ledger); values and labels use neutral ink; intervals are the 95% bootstrap CIs.
"""

from pathlib import Path

CONDITION_ORDER = ("none", "raw", "ledger")
CONDITION_LABEL = {"none": "no memory", "raw": "raw history", "ledger": "ledger"}
# Validated categorical slots 1 to 3 (light surface): blue, orange, aqua.
SERIES = {"none": "#2a78d6", "raw": "#eb6834", "ledger": "#1baf7a"}
INK, INK_SOFT, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e1", "#fcfcfb"


def _style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_SOFT, labelsize=9)
    ax.set_facecolor(SURFACE)
    ax.grid(True, axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _conditions(summary):
    return [c for c in CONDITION_ORDER if c in summary["conditions"]]


def _metric(summary, condition, name):
    return summary["conditions"][condition]["metrics"][name]


def _dot_interval(ax, summary, name, fmt="{:.0%}", xlim=(0, 1)):
    """Horizontal dot-and-interval plot of one metric per condition, direct-labelled."""
    conditions = _conditions(summary)
    for index, condition in enumerate(conditions):
        metric = _metric(summary, condition, name)
        y = len(conditions) - 1 - index
        value, (low, high) = metric["value"], metric["ci95"]
        if value is None:
            ax.text(0, y, "n/a", color=INK_SOFT, va="center", fontsize=9)
            continue
        if low is not None:
            ax.plot([low, high], [y, y], color=SERIES[condition], linewidth=2, solid_capstyle="round")
        ax.plot([value], [y], "o", color=SERIES[condition], markersize=8, markeredgecolor=SURFACE, markeredgewidth=1.5)
        label = fmt.format(value) + (f"  n={summary['conditions'][condition]['games']}")
        ax.text((high if high is not None else value) + (xlim[1] - xlim[0]) * 0.02, y, label,
                color=INK, va="center", fontsize=9)
    ax.set_yticks(range(len(conditions)))
    ax.set_yticklabels([CONDITION_LABEL[c] for c in reversed(conditions)], color=INK)
    ax.set_xlim(*xlim)
    ax.set_ylim(-0.7, len(conditions) - 0.3)
    _style(ax)


def _save(fig, out_dir, name):
    paths = []
    for ext in ("png", "svg"):
        path = Path(out_dir) / f"{name}.{ext}"
        fig.savefig(path, dpi=200, bbox_inches="tight", facecolor=SURFACE)
        paths.append(str(path))
    return paths


def make_figures(summary, out_dir, title_prefix=""):
    """Write the slide figures; returns the list of paths (empty if matplotlib is missing)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 10, "text.color": INK,
                         "axes.labelcolor": INK_SOFT, "figure.facecolor": SURFACE})
    conditions = _conditions(summary)
    written = []

    # 1. First-accusation accuracy and eventual solve rate.
    fig, axes = plt.subplots(1, 2, figsize=(9, 2.6), sharey=False)
    _dot_interval(axes[0], summary, "first_accusation_accuracy")
    axes[0].set_title(f"{title_prefix}First-accusation accuracy (A1)", loc="left", fontsize=11, color=INK)
    _dot_interval(axes[1], summary, "solve_rate_within_3")
    axes[1].set_title("Eventual solve rate (A3)", loc="left", fontsize=11, color=INK)
    axes[0].set_xlabel("share of scheduled games; dot = point estimate, bar = 95% bootstrap CI", fontsize=8, color=INK_SOFT)
    axes[1].set_xlabel("share of scheduled games", fontsize=8, color=INK_SOFT)
    fig.tight_layout()
    written += _save(fig, out_dir, "accuracy_by_condition")
    plt.close(fig)

    # 2. Cumulative solve rate by attempt, one line per condition.
    fig, ax = plt.subplots(figsize=(5.2, 3.2))
    for condition in conditions:
        ys = [_metric(summary, condition, name)["value"] or 0
              for name in ("first_accusation_accuracy", "solve_rate_within_2", "solve_rate_within_3")]
        ax.plot([1, 2, 3], ys, color=SERIES[condition], linewidth=2, marker="o", markersize=7,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=CONDITION_LABEL[condition])
        ax.text(3.08, ys[-1], f"{CONDITION_LABEL[condition]} {ys[-1]:.0%}", color=INK, va="center", fontsize=9)
    ax.set_xticks([1, 2, 3])
    ax.set_xticklabels(["1st accusation", "within 2", "within 3"], color=INK)
    ax.set_xlim(0.8, 3.9)
    ax.set_ylim(0, 1)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1])
    ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.set_title(f"{title_prefix}Cumulative solve rate by accusation attempt", loc="left", fontsize=11, color=INK)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    _style(ax)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.grid(False, axis="x")
    fig.tight_layout()
    written += _save(fig, out_dir, "solve_rate_by_attempt")
    plt.close(fig)

    # 3. Cost per game, stacked by phase, with the CI on the total.
    fig, ax = plt.subplots(figsize=(6, 2.6))
    phases = (("cost_through_first_accusation_mean", "through 1st accusation", 1.0),
              ("cost_accusation_retries_mean", "retries", 0.55))
    max_total = 0
    for index, condition in enumerate(conditions):
        y = len(conditions) - 1 - index
        left = 0
        for name, label, alpha in phases:
            value = _metric(summary, condition, name)["value"] or 0
            ax.barh(y, value, left=left, height=0.5, color=SERIES[condition], alpha=alpha, edgecolor=SURFACE, linewidth=2)
            left += value
        total = _metric(summary, condition, "cost_usd_mean")
        if total["value"] is not None:
            low, high = total["ci95"]
            if low is not None:
                ax.plot([low, high], [y, y], color=INK, linewidth=1.2)
            ax.text(max(left, high or 0) * 1.02 + 1e-9, y, f"${total['value']:.4f}", color=INK, va="center", fontsize=9)
            max_total = max(max_total, high or total["value"])
    ax.set_yticks(range(len(conditions)))
    ax.set_yticklabels([CONDITION_LABEL[c] for c in reversed(conditions)], color=INK)
    ax.set_xlim(0, max_total * 1.3 if max_total else 1)
    ax.set_ylim(-0.7, len(conditions) - 0.3)
    ax.set_xlabel("USD per game, mean: solid = through the first accusation, pale = accusation retries; "
                  "black bar = 95% CI of the total", fontsize=8, color=INK_SOFT)
    ax.set_title(f"{title_prefix}Cost per game by phase", loc="left", fontsize=11, color=INK)
    _style(ax)
    fig.tight_layout()
    written += _save(fig, out_dir, "cost_per_game")
    plt.close(fig)

    # 4. Interrogations before the first accusation and online evidence coverage.
    fig, axes = plt.subplots(1, 2, figsize=(9, 2.6))
    turns_max = max((_metric(summary, c, "interrogations_before_first_accusation_mean")["ci95"][1] or 0)
                    for c in conditions) or 20
    _dot_interval(axes[0], summary, "interrogations_before_first_accusation_mean", fmt="{:.1f}", xlim=(0, turns_max * 1.3))
    axes[0].set_title(f"{title_prefix}Interrogations before first accusation", loc="left", fontsize=11, color=INK)
    axes[0].set_xlabel("interrogation turns (of 20); dot = mean, bar = 95% bootstrap CI", fontsize=8, color=INK_SOFT)
    _dot_interval(axes[1], summary, "online_evidence_coverage_mean")
    axes[1].set_title("Evidence matched online (heuristic, D11)", loc="left", fontsize=11, color=INK)
    axes[1].set_xlabel("share of the case's gold evidence items", fontsize=8, color=INK_SOFT)
    fig.tight_layout()
    written += _save(fig, out_dir, "efficiency_by_condition")
    plt.close(fig)

    # 5. Ledger minus raw, the primary comparison, centred on zero.
    if "ledger_minus_raw" in summary["differences"]:
        diffs = summary["differences"]["ledger_minus_raw"]
        names = [("first_accusation_accuracy", "A1 accuracy"), ("solve_rate_within_3", "A3 solve rate"),
                 ("online_evidence_coverage_mean", "evidence matched (heuristic)")]
        fig, ax = plt.subplots(figsize=(5.6, 2.4))
        extent = 0.25
        for name, _ in names:
            low, high = diffs[name]["ci95"]
            extent = max(extent, abs(low or 0), abs(high or 0), abs(diffs[name]["value"] or 0))
        extent = min(1.0, extent * 1.35)
        for index, (name, label) in enumerate(names):
            y = len(names) - 1 - index
            value, (low, high) = diffs[name]["value"], diffs[name]["ci95"]
            if value is None:
                continue
            if low is not None:
                ax.plot([low, high], [y, y], color=SERIES["ledger"], linewidth=2, solid_capstyle="round")
            ax.plot([value], [y], "o", color=SERIES["ledger"], markersize=8, markeredgecolor=SURFACE, markeredgewidth=1.5)
            ax.text((high if high is not None else value) + extent * 0.04, y, f"{value:+.0%}",
                    color=INK, va="center", fontsize=9)
        ax.axvline(0, color=INK_SOFT, linewidth=1)
        ax.set_yticks(range(len(names)))
        ax.set_yticklabels([label for _, label in reversed(names)], color=INK)
        ax.set_xlim(-extent, extent * 1.25)
        ticks = [t for t in (-1, -0.5, -0.25, 0, 0.25, 0.5, 1) if -extent <= t <= extent]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t:+.0%}" if t else "0" for t in ticks])
        ax.set_xlabel("percentage points, ledger minus raw; positive favours the ledger", fontsize=8, color=INK_SOFT)
        ax.set_title(f"{title_prefix}Ledger minus raw history (95% bootstrap CI)", loc="left", fontsize=11, color=INK)
        _style(ax)
        fig.tight_layout()
        written += _save(fig, out_dir, "ledger_minus_raw")
        plt.close(fig)
    return written

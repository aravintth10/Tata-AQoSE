"""Does deciding by expected damage protect calls better than taking the
top label when the classifier gets worse? (Chunk 8B.)

  python -m aqos_ml.ablation

The classifier is weakened by multiplying every number it sees by a random
factor exp(N(0, noise)) (aqos_ml.serve --feature-noise). For each noise
level: how accurate the weakened classifier is on the validation flows,
and, from live runs of S5 and S6, what the calls went through under each
rule. Noise 0 is the frozen system's own runs (results-final, seeds 0-2).
Writes reports/ablation.md and reports/charts/ablation.png.
"""

import argparse
from pathlib import Path

import numpy as np
import yaml

from . import charts, damage, decide, evaluate, model, report, serve
from .dataset import build as dataset

REPO = Path(__file__).resolve().parents[2]
LEVELS = [0.0, 0.5, 1.0, 1.5]
SCENARIOS = ["S5", "S6"]
SEEDS = [0, 1, 2]


def offline(version, levels=LEVELS, every=3):
    """{noise: {accuracy, macro_f1, damage by rule}} on validation flows."""
    m = model.load(version)
    rows = list(dataset.load(REPO / "data" / "dataset" / "v2.jsonl.gz", splits={"validation"}))[::every]
    table = evaluate.load_damage_table()
    rules = {"O1": decide.Engine(table, mode="top_label").tin, "O2": decide.Engine(table).tin}
    out = {}
    for noise in levels:
        service = serve.Service.__new__(serve.Service)
        service.noise, service._rng = noise, None
        blurred = [service.blur(r) for r in rows]

        class Weak:
            model_version = m.model_version

            def classify_many(self, records):
                return m.classify_many(blurred)

        scores = {name: evaluate.score(Weak(), rows, table, decide=rule) for name, rule in rules.items()}
        out[noise] = {"accuracy": scores["O2"]["accuracy"], "macro_f1": scores["O2"]["macro_f1"],
                      **{f"damage_{name}": s["expected_damage_balanced"] for name, s in scores.items()}}
    return out


def live(levels=LEVELS):
    """{noise: {scenario: {config: [call late-frames % per seed]}}}."""
    scoring = yaml.safe_load(Path(damage.SCORING).read_text())
    out = {}
    for noise in levels:
        where = REPO / ("results-final" if noise == 0 else f"results-ablation-{noise}")
        if not where.exists():
            continue
        runs, _ = report.reduce(where, scoring)
        out[noise] = {s: {c: [runs[s][c][k].get("call: late frames %") for k in SEEDS if k in runs.get(s, {}).get(c, {})]
                          for c in ("O1", "O2")} for s in SCENARIOS if s in runs}
    return out


def markdown(off, on):
    lines = ["# Expected damage against top label, with a weaker classifier", "",
             "Every number the classifier sees is multiplied by exp(N(0, noise)). Offline: validation flows of "
             "dataset v2, every class counting the same. Live: the call in S5 and S6, median late frames over "
             f"seeds {SEEDS[0]} to {SEEDS[-1]}.", "",
             "| Noise | Accuracy | Macro-F1 | Damage, top label | Damage, expected damage | "
             + " | ".join(f"{s} call late %, top label | {s}, expected damage" for s in SCENARIOS) + " |",
             "| --- |" + " --- |" * (4 + 2 * len(SCENARIOS))]
    for noise in LEVELS:
        o = off.get(noise, {})
        cells = [f"{noise:g}", report._num(o.get("accuracy"), 3), report._num(o.get("macro_f1"), 3),
                 report._num(o.get("damage_O1"), 3), report._num(o.get("damage_O2"), 3)]
        for s in SCENARIOS:
            for c in ("O1", "O2"):
                values = [v for v in on.get(noise, {}).get(s, {}).get(c, []) if v is not None]
                cells.append(report._num(float(np.median(values)), 1) if values else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def chart(off, on, out):
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, len(SCENARIOS), figsize=(10, 3.8), sharey=True)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.72, bottom=0.17, wspace=0.12)
    for ax, s in zip(axes, SCENARIOS):
        for c in ("O1", "O2"):
            xs, ys = [], []
            for noise in LEVELS:
                values = [v for v in on.get(noise, {}).get(s, {}).get(c, []) if v is not None]
                if values and noise in off:
                    xs.append(100 * off[noise]["accuracy"])
                    ys.append(float(np.median(values)))
            if xs:
                ax.plot(xs, ys, "o-", color=charts.COLOR[c], linewidth=2, markersize=7,
                        markeredgecolor=charts.SURFACE, markeredgewidth=1.5, label=charts.NAME[c], zorder=3)
        ax.set_title(f"{s}: the call", fontsize=10)
        ax.invert_xaxis()
        ax.set_xlabel("classifier accuracy, %  (worse to the right)")
        ax.yaxis.grid(True, color=charts.GRID, linewidth=0.8, zorder=0)
        ax.spines["bottom"].set_color(charts.AXIS)
        ax.tick_params(length=0, labelsize=8.5)
    axes[0].set_ylabel("frames late, %")
    axes[-1].legend(loc="lower right", bbox_to_anchor=(1.0, 1.12), ncols=2, fontsize=9, borderaxespad=0)
    charts._title(fig, "When the classifier gets worse", "Late frames of the call, median of three seeds, as the classifier is weakened")
    return charts._save(fig, out, "ablation.png")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.ablation", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", default=None)
    parser.add_argument("--out", default=str(REPO / "reports" / "ablation.md"))
    args = parser.parse_args(argv)
    version = args.version or (model.MODELS / "chosen-v2.txt").read_text().strip()
    off, on = offline(version), live()
    Path(args.out).write_text(markdown(off, on))
    print(markdown(off, on))
    if on:
        print(chart(off, on, REPO / "reports" / "charts"))


if __name__ == "__main__":
    main()

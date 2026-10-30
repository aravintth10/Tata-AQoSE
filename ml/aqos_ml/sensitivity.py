"""How much the decisions depend on the exact numbers in the damage table.

  python -m aqos_ml.sensitivity --dataset data/dataset/v1.jsonl.gz

The table has two seeds per cell and is rounded to half a point, so any
cell could be off by a quarter. Each cell is moved by -25% and +25% in
turn, then every cell at once by a random factor in that range, 200
times. A factor leaves a zero cell at zero, and much of the table is
zero, so every cell is also moved by up to a whole point either way
(twice the table's rounding step), 200 times. For each perturbed table the decision engine places the
validation flows again. Reported: the share of flows whose tin changes
(every class counting the same), and the balanced expected damage of
the new placements judged by the unperturbed table.
"""

import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import decide, evaluate, model
from .dataset import build as dataset

REPO = Path(__file__).resolve().parents[2]


def placements(classifications, rows, table):
    engine = decide.Engine(table)
    return [engine.tin(c, row) for c, row in zip(classifications, rows)]


def flow_weights(rows):
    per_flow = defaultdict(int)
    for row in rows:
        per_flow[row.get("session"), row["flow_id"]] += 1
    return np.array([1 / per_flow[row.get("session"), row["flow_id"]] for row in rows])


def balanced(values, labels, weights):
    by_class = defaultdict(lambda: [0.0, 0.0])
    for v, label, w in zip(values, labels, weights):
        by_class[label][0] += v * w
        by_class[label][1] += w
    return float(np.mean([s / n for s, n in by_class.values()]))


def run(dataset_path, version=None, draws=200, seed=1):
    version = version or (model.MODELS / "chosen-v2.txt").read_text().strip()
    classifier = model.load(version)
    rows = list(dataset.load(dataset_path, splits={"validation"}))
    classifications = classifier.classify_many(rows)
    table = evaluate.load_damage_table()
    labels = [row["label"] for row in rows]
    weights = flow_weights(rows)
    base = placements(classifications, rows, table)
    true_damage = lambda tins: balanced([table[l][t] for l, t in zip(labels, tins)], labels, weights)  # noqa: E731
    changed = lambda tins: balanced([float(a != b) for a, b in zip(base, tins)], labels, weights)  # noqa: E731
    out = {"base_damage": true_damage(base), "single": [], "joint": [], "additive": []}
    for cls in evaluate.CLASSES:
        for tin in table[cls]:
            for factor in (0.75, 1.25):
                moved = {c: dict(row) for c, row in table.items()}
                moved[cls][tin] = table[cls][tin] * factor
                tins = placements(classifications, rows, moved)
                out["single"].append({"class": cls, "tin": tin, "factor": factor,
                                      "changed": changed(tins), "damage": true_damage(tins)})
    rng = np.random.default_rng(seed)
    for _ in range(draws):
        moved = {c: {t: v * rng.uniform(0.75, 1.25) for t, v in row.items()} for c, row in table.items()}
        tins = placements(classifications, rows, moved)
        out["joint"].append({"changed": changed(tins), "damage": true_damage(tins)})
    for _ in range(draws):
        moved = {c: {t: max(v + rng.uniform(-1.0, 1.0), 0.0) for t, v in row.items()} for c, row in table.items()}
        tins = placements(classifications, rows, moved)
        out["additive"].append({"changed": changed(tins), "damage": true_damage(tins)})
    return out


def markdown(out):
    single = sorted(out["single"], key=lambda r: -r["changed"])
    joint_changed = np.array([r["changed"] for r in out["joint"]])
    joint_damage = np.array([r["damage"] for r in out["joint"]])
    add_changed = np.array([r["changed"] for r in out["additive"]])
    add_damage = np.array([r["damage"] for r in out["additive"]])
    lines = ["# Damage table sensitivity", "",
             "Validation split of dataset v1. Every class counts the same. The balanced expected damage of the "
             f"unperturbed table's placements is {out['base_damage']:.3f}.", "",
             "## Every cell at once, each by a random factor from 0.75 to 1.25 (200 tables)", "",
             "| | Median | 95th percentile | Worst |", "| --- | --- | --- | --- |",
             f"| Flows whose tin changes | {np.median(joint_changed):.1%} | {np.percentile(joint_changed, 95):.1%} "
             f"| {joint_changed.max():.1%} |",
             f"| Balanced expected damage, judged by the unperturbed table | {np.median(joint_damage):.3f} | "
             f"{np.percentile(joint_damage, 95):.3f} | {joint_damage.max():.3f} |", "",
             "## Every cell at once, each moved by up to one point either way (200 tables)", "",
             "| | Median | 95th percentile | Worst |", "| --- | --- | --- | --- |",
             f"| Flows whose tin changes | {np.median(add_changed):.1%} | {np.percentile(add_changed, 95):.1%} "
             f"| {add_changed.max():.1%} |",
             f"| Balanced expected damage, judged by the unperturbed table | {np.median(add_damage):.3f} | "
             f"{np.percentile(add_damage, 95):.3f} | {add_damage.max():.3f} |", "",
             "## One cell at a time, by 25% down or up: the ten that move most", "",
             "| Class | Tin | Factor | Flows whose tin changes | Balanced expected damage |",
             "| --- | --- | --- | --- | --- |"]
    for r in single[:10]:
        lines.append(f"| {r['class']} | {r['tin']} | {r['factor']} | {r['changed']:.1%} | {r['damage']:.3f} |")
    untouched = sum(r["changed"] < 0.005 for r in single)
    lines += ["", f"{untouched} of {len(single)} single-cell changes move under 0.5% of flows.", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.sensitivity", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", default=str(REPO / "reports" / "sensitivity-v1.md"))
    args = parser.parse_args(argv)
    text = markdown(run(args.dataset))
    Path(args.out).write_text(text)
    print(text)


if __name__ == "__main__":
    main()

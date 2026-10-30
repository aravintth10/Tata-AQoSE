"""The offline head-to-head (chunk 6B): do the trained models beat the
threshold rules, by how much, and on which traffic?

  python -m aqos_ml.compare --dataset data/dataset/v1.jsonl.gz
  python -m aqos_ml.compare --dataset data/dataset/v1.jsonl.gz --final   # the test set, once

Every contestant is scored by the same code on the same flows:

  rules, qosify's defaults        what the tools ship with
  rules, tuned                    thresholds tuned on our training data
  each model, by top label        O1
  each model, by expected damage  O2

Besides the headline (class-balanced expected damage), the report shows
where each class's flows were sent, the two kinds of traffic the project
is about (HD video calls and cloud gaming), a confidence interval on the
difference from the tuned rules, and what happens to each source of data
when the model is trained without it.

The validation split is the default. The test split is read only with
--final, and that is meant to happen once.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from . import decide, evaluate, model, rules, train
from .dataset import build as dataset

REPORTS = evaluate.REPO / "reports"
TIN_ORDER = ["voice", "video", "best_effort", "bulk"]
# The activities scenarios S6 and S7 are built on.
FOCUS = {"video_call": "Video calls", "cloud_gaming": "Cloud gaming"}


def placements(classifier, rows, damage, decide_tin):
    """One entry per flow: (session, class, activity, source, mean damage,
    {tin: share of the flow's records sent there})."""
    many = getattr(classifier, "classify_many", None)
    classifications = many(rows) if many else [classifier.classify(r) for r in rows]
    flows = {}
    for row, classification in zip(rows, classifications):
        tin = decide_tin(classification, row)
        entry = flows.setdefault((row["session"], row["flow_id"]),
                                 [row["session"], row["label"], row.get("activity"), row["source"], [], Counter()])
        entry[4].append(damage[row["label"]][tin])
        entry[5][tin] += 1
    return [(session, label, activity, source, float(np.mean(costs)), {t: n / len(costs) for t, n in tins.items()})
            for session, label, activity, source, costs, tins in flows.values()]


def balanced(flows):
    """Mean over classes of the mean damage of the class's flows."""
    by_class = defaultdict(list)
    for _, label, _, _, cost, _ in flows:
        by_class[label].append(cost)
    return float(np.mean([np.mean(v) for v in by_class.values()])) if by_class else float("nan")


def interval(a, b, draws=2000, seed=1):
    """A 95% interval for balanced(a) - balanced(b). a and b are two
    contestants' placements of the same flows.

    Whole sessions are resampled, because flows of one session are not
    independent, and within each class separately, because the figure is a
    mean over classes. A class with a single session cannot vary, so with
    few sessions the interval is narrower than the truth.
    """
    # class -> session -> [damage of a, damage of b, flows]
    cells = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0]))
    for which, flows in enumerate((a, b)):
        for session, label, _, _, cost, _ in flows:
            cell = cells[label][session]
            cell[which] += cost
            cell[2] += which == 0
    rng = np.random.default_rng(seed)
    per_class = []
    for label in sorted(cells):
        table = np.array([cells[label][s] for s in sorted(cells[label])], dtype=float)
        pick = rng.integers(0, len(table), (draws, len(table)))
        sums = table[pick].sum(axis=1)  # draws x [a, b, flows]
        per_class.append((sums[:, 0] - sums[:, 1]) / sums[:, 2])
    if not per_class:
        return None
    low, high = np.percentile(np.mean(per_class, axis=0), [2.5, 97.5])
    return float(low), float(high)


def share(flows, tin, where):
    """Share of the flows matching `where` that were sent to `tin`."""
    picked = [f for f in flows if where(f)]
    return (sum(f[5].get(tin, 0.0) for f in picked) / len(picked), len(picked)) if picked else (None, 0)


def contestants(name, digest, damage, train_rows, models_dir):
    """[(label, classifier, decide function)], rules first."""
    # The rules stand in for existing tools and keep their own behaviour:
    # no guard on Bulk. The models' decision rules have it, as live.
    top = decide.Engine(damage, mode="top_label")
    plain = decide.Engine(damage, mode="top_label", bulk_guard=False)
    tuned, _, _ = rules.tune(train_rows, damage)
    out = [("Rules, qosify defaults", rules.Rules(), plain.tin),
           (f"Rules, tuned ({tuned.bulk_pps:g} pkt/s, {tuned.priority_bytes:g} B)", tuned, plain.tin)]
    for kind, title in (("rf", "Random forest (tuned)"), ("lgbm", "LightGBM")):
        version = f"{kind}-{name}-{digest[:8]}"
        if not (Path(models_dir) / version / "meta.json").exists():
            continue
        loaded = model.load(version, models_dir)
        out.append((f"{title}, top label (O1)", loaded, top.tin))
        out.append((f"{title}, expected damage (O2)", loaded, decide.Engine(damage).tin))
    return out


def holdout(rows, eval_rows, damage, params, seed, jobs):
    """For each source: train without it, then score its flows. A model
    that has learned which lab a flow came from, and not what the flow is,
    does badly here."""
    engine = decide.Engine(damage)
    out = {}
    for source in sorted({r["source"] for r in eval_rows}):
        kept = {s: [r for r in rows[s] if r["source"] != source] for s in ("train", "calibration", "validation")}
        target = [r for r in eval_rows if r["source"] == source]
        if any(set(evaluate.CLASSES) - {r["label"] for r in kept[s]} for s in kept):
            out[source] = {"skipped": "a class would have no data left without this source"}
            continue
        splits = {s: train.Split(r) for s, r in kept.items()}
        settings = {k: v for k, v in params.items() if k != "trees"}
        est = train.boosted(splits["train"], splits["validation"], settings, seed, jobs)
        est = train.calibrated(est, splits["calibration"], "isotonic")
        without = model.Model(est, f"without-{source}")
        flows = placements(without, target, damage, engine.tin)
        by_class = defaultdict(list)
        for _, label, _, _, cost, _ in flows:
            by_class[label].append(cost)
        out[source] = {"flows": len(flows), "damage_by_class": {c: float(np.mean(v)) for c, v in by_class.items()},
                       "flows_by_class": {c: len(v) for c, v in by_class.items()}}
    return out


def compare(dataset_path, split="validation", models_dir=model.MODELS, damage_path=None, with_holdout=True,
            seed=1, jobs=4, report=print):
    dataset_path = Path(dataset_path)
    name = dataset_path.name.removesuffix(".jsonl.gz")
    digest = train.sha256(dataset_path)
    damage_path = Path(damage_path or evaluate.REPO / "configs" / "damage_table.yaml")
    damage = evaluate.load_damage_table(damage_path)
    wanted = {"train", "calibration", "validation"}
    rows = {s: [] for s in wanted}
    for row in dataset.load(dataset_path, splits=wanted):
        rows[row["split"]].append(row)
    if split == "test":
        test_path = dataset_path.with_name(f"{name}.test.jsonl.gz")
        eval_rows = list(dataset.load(test_path))
    else:
        eval_rows = rows[split]

    results, placed = [], {}
    for label, classifier, decide_tin in contestants(name, digest, damage, rows["train"], models_dir):
        report(f"scoring {label}")
        score = evaluate.score(classifier, eval_rows, damage, decide=decide_tin)
        flows = placements(classifier, eval_rows, damage, decide_tin)
        placed[label] = flows
        focus = {}
        for activity in FOCUS:
            to_bulk, n = share(flows, "bulk", lambda f, a=activity: f[2] == a)
            costs = [f[4] for f in flows if f[2] == activity]
            focus[activity] = {"flows": n, "to_bulk": to_bulk, "damage": float(np.mean(costs)) if costs else None}
        results.append({
            "contestant": label, "model_version": score["model_version"],
            "expected_damage_balanced": score["expected_damage_balanced"], "damage_by_class": score["damage_by_class"],
            "accuracy": score["accuracy"], "macro_f1": score["macro_f1"],
            "ece": None if label.startswith("Rules") else score["ece"],
            "tins": score["tins"], "recall": score["recall"], "focus": focus,
        })
    reference = next(r["contestant"] for r in results if r["contestant"].startswith("Rules, tuned"))
    for result in results:
        result["vs_tuned_rules"] = None
        if result["contestant"] != reference:
            ci = interval(placed[result["contestant"]], placed[reference], seed=seed)
            result["vs_tuned_rules"] = {
                "difference": result["expected_damage_balanced"] - balanced(placed[reference]), "interval_95": ci}

    held = None
    chosen = Path(models_dir) / f"chosen-{name}.txt"
    lgbm_meta = Path(models_dir) / f"lgbm-{name}-{digest[:8]}" / "meta.json"
    if with_holdout and lgbm_meta.exists():
        report("training without each source in turn")
        held = holdout(rows, eval_rows, damage, json.loads(lgbm_meta.read_text())["params"], seed, jobs)

    counts = Counter()
    for f in next(iter(placed.values())):
        counts[f[1]] += 1
    return {
        "dataset": {"name": name, "sha256": digest}, "split": split,
        "damage_table": {"file": damage_path.name, "status": _status(damage_path)},
        "chosen_model": chosen.read_text().strip() if chosen.exists() else None,
        "flows_by_class": dict(counts), "flows": sum(counts.values()),
        "sessions": len({f[0] for f in next(iter(placed.values()))}),
        "results": results, "holdout": held,
    }


def _status(path):
    import yaml
    return yaml.safe_load(Path(path).read_text()).get("status", "unknown")


def verdict(out):
    """The go/no-go, as (go, [reasons])."""
    tuned = next(r for r in out["results"] if r["contestant"].startswith("Rules, tuned"))
    models = [r for r in out["results"] if "(O2)" in r["contestant"]]
    if not models:
        return False, ["No trained model was found for this dataset."]
    best = min(models, key=lambda r: r["expected_damage_balanced"])
    reasons, go = [], True
    diff = best["vs_tuned_rules"]
    ci = diff["interval_95"]
    if ci and ci[1] < 0:
        reasons.append(f"{best['contestant']} has lower balanced expected damage than the tuned rules: "
                       f"{best['expected_damage_balanced']:.3f} against {tuned['expected_damage_balanced']:.3f} "
                       f"(difference {diff['difference']:+.3f}, 95% interval {ci[0]:+.3f} to {ci[1]:+.3f}).")
    else:
        go = False
        shown = f"{ci[0]:+.3f} to {ci[1]:+.3f}" if ci else "not computable"
        reasons.append(f"{best['contestant']} is not clearly better than the tuned rules: difference "
                       f"{diff['difference']:+.3f}, 95% interval {shown}.")
    for activity, title in FOCUS.items():
        mine, theirs = best["focus"][activity], tuned["focus"][activity]
        if not mine["flows"]:
            go = False
            reasons.append(f"{title}: no flows in this split, so the central claim cannot be checked.")
        elif mine["damage"] <= theirs["damage"]:
            reasons.append(f"{title} ({mine['flows']} flows): damage {mine['damage']:.2f} against the rules' "
                           f"{theirs['damage']:.2f}; sent to Bulk {mine['to_bulk']:.0%} against {theirs['to_bulk']:.0%}.")
        else:
            go = False
            reasons.append(f"{title} ({mine['flows']} flows): the model does worse than the rules, "
                           f"{mine['damage']:.2f} against {theirs['damage']:.2f}.")
    return go, reasons


def markdown(out):
    go, reasons = verdict(out)
    num = lambda v, spec=".3f": "" if v is None else format(v, spec)  # noqa: E731
    lines = [
        f"# Offline head-to-head: {out['dataset']['name']}, {out['split']} split", "",
        f"Dataset `{out['dataset']['name']}` (sha256 `{out['dataset']['sha256'][:12]}`), "
        f"{out['flows']} flows from {out['sessions']} sessions. "
        f"Damage table: `{out['damage_table']['file']}` ({out['damage_table']['status']}). "
        "Every flow counts once and every class counts the same.", "",
        f"## Verdict: {'go' if go else 'no-go'}", "",
        *[f"- {r}" for r in reasons], "",
    ]
    if out["damage_table"]["status"] != "measured":
        lines += ["**The damage table is not the measured one.** Every damage figure below depends on it.", ""]
    lines += [
        "## Scores", "",
        "| Contestant | Balanced expected damage | Against tuned rules (95% interval) | Accuracy | Macro-F1 | "
        "Calibration error |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in out["results"]:
        versus = ""
        if r["vs_tuned_rules"]:
            ci = r["vs_tuned_rules"]["interval_95"]
            versus = f"{r['vs_tuned_rules']['difference']:+.3f}" + (f" ({ci[0]:+.3f} to {ci[1]:+.3f})" if ci else "")
        lines.append(f"| {r['contestant']} | {num(r['expected_damage_balanced'])} | {versus} | {num(r['accuracy'])} | "
                     f"{num(r['macro_f1'])} | {num(r['ece'])} |")
    lines += ["", "Lower damage is better. The interval comes from resampling whole sessions 2,000 times; "
              "one that stays below zero means the contestant beats the tuned rules.", "",
              "## Expected damage by true class", "",
              "| Contestant | " + " | ".join(evaluate.CLASSES) + " |", "| --- |" + " --- |" * len(evaluate.CLASSES),
              "| *flows* | " + " | ".join(str(out["flows_by_class"].get(c, 0)) for c in evaluate.CLASSES) + " |"]
    for r in out["results"]:
        lines.append(f"| {r['contestant']} | " + " | ".join(num(r["damage_by_class"].get(c), ".2f")
                                                           for c in evaluate.CLASSES) + " |")
    lines += ["", "## The traffic the project is about", "",
              "| Contestant | " + " | ".join(f"{t}: sent to Bulk | {t}: damage" for t in FOCUS.values()) + " |",
              "| --- |" + " --- | --- |" * len(FOCUS)]
    for r in out["results"]:
        cells = []
        for activity in FOCUS:
            f = r["focus"][activity]
            cells += [("" if f["to_bulk"] is None else f"{f['to_bulk']:.0%}") + f" of {f['flows']}", num(f["damage"], ".2f")]
        lines.append(f"| {r['contestant']} | " + " | ".join(cells) + " |")
    lines += ["", "## Where each class's flows were sent", ""]
    for r in out["results"]:
        lines += [f"**{r['contestant']}**", "", "| True class | " + " | ".join(TIN_ORDER) + " | Recall |",
                  "| --- |" + " --- |" * (len(TIN_ORDER) + 1)]
        for cls in evaluate.CLASSES:
            tins = r["tins"].get(cls)
            if not tins:
                continue
            total = sum(tins.values())
            lines.append(f"| {cls} | " + " | ".join(f"{tins.get(t, 0) / total:.0%}" for t in TIN_ORDER)
                         + f" | {num(r['recall'].get(cls), '.2f')} |")
        lines.append("")
    if out["holdout"]:
        lines += ["## Has the model learned the lab?", "",
                  "LightGBM trained without each source in turn, then scored on that source's flows by expected "
                  "damage. A class the model can only place when it has seen that source's own recordings shows up "
                  "here as high damage.", "",
                  "| Source left out | Flows | Damage by class on that source |", "| --- | --- | --- |"]
        for source, h in out["holdout"].items():
            if "skipped" in h:
                lines.append(f"| {source} | | Skipped: {h['skipped']} |")
                continue
            detail = ", ".join(f"{c} {v:.2f} ({h['flows_by_class'][c]})" for c, v in sorted(h["damage_by_class"].items()))
            lines.append(f"| {source} | {h['flows']} | {detail} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.compare", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=evaluate.REPO / "data" / "dataset" / "v1.jsonl.gz")
    parser.add_argument("--models", default=model.MODELS)
    parser.add_argument("--damage", help="damage table (default configs/damage_table.yaml)")
    parser.add_argument("--final", action="store_true", help="score the test split. Meant to happen once")
    parser.add_argument("--no-holdout", action="store_true", help="skip training without each source")
    parser.add_argument("--out", default=REPORTS)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)
    split = "test" if args.final else "validation"
    out = compare(args.dataset, split, args.models, args.damage, not args.no_holdout, jobs=args.jobs)
    directory = Path(args.out)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"head-to-head-{out['dataset']['name']}-{split}"
    (directory / f"{stem}.json").write_text(json.dumps(out, indent=2) + "\n")
    (directory / f"{stem}.md").write_text(markdown(out))
    go, reasons = verdict(out)
    print(f"\n{'GO' if go else 'NO-GO'}")
    for reason in reasons:
        print(" -", reason)
    print(f"report: {directory / (stem + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

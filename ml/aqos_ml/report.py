"""The statistics of a matrix of runs (chunk 9A): does the full system
beat plain CAKE, the threshold rules and its own simpler decision rule,
scenario by scenario, and by more than chance?

  python -m aqos_ml.report --results results-core --aa results-aa --out reports/core.md

Each run is reduced to a few numbers, the same ones the damage table is
built from: every traffic entry's cost from 0 (as good as it gets) to 1
(unusable), their sum, and for calls and games the share of frames that
arrived late and the 95th-percentile latency. Per scenario and
configuration these are given as the median over seeds with a 95%
bootstrap interval. Configurations are compared seed against seed, since
the same seed gives the same traffic: the median of the paired
differences with its interval, the Wilcoxon signed-rank test and the
matched-pairs rank-biserial effect size.

Two comparisons are named in advance and are not corrected: O2 against B4
(does a model beat the rules) and O2 against O1 (does deciding by expected
damage beat taking the top label). The others are corrected with Holm's
method. "No worse than CAKE" is judged against the noise between two B2
runs of the same seed (--aa), when those runs exist.

A scenario where the methods do the same is reported as a tie.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml
from scipy import stats

from . import damage
from .run import table

KEY = [("O2", "B4"), ("O2", "O1")]
OTHERS = [("O2", "B2"), ("O2", "B3"), ("O1", "B2"), ("B4", "B2"), ("B3", "B2"), ("B2", "B0")]
RESAMPLES = 10_000
COST = "summed cost"


def reduce_run(entries, scoring, usual):
    """{measure: value} for one run. Lower is better for every measure."""
    out = {COST: 0.0}
    for tid, entry in entries.items():
        cost = damage.cost(entry, scoring, (usual or {}).get(tid))
        out[COST] += cost
        out[f"{tid}: cost"] = cost
        if entry["profile"] not in damage.INTERACTIVE:
            continue
        sides = [entry[d] for d in ("up", "down")]
        late = [s["frame_miss_ratio"] for s in sides if s.get("frame_miss_ratio") is not None]
        latency = [s["latency_p95_ms"] for s in sides if s.get("latency_p95_ms") is not None]
        if late:
            out[f"{tid}: late frames %"] = 100 * max(late)
        if latency:
            out[f"{tid}: latency p95 ms"] = max(latency)
    return out


def reduce(results, scoring):
    """{scenario: {config: {seed: {measure: value}}}}, and the scenarios' names."""
    out, names = defaultdict(lambda: defaultdict(dict)), {}
    for (scenario, seed), by_config in table.collect(results).items():
        usual = by_config.get(table.USUAL, {}).get("entries")
        for config, run in by_config.items():
            out[scenario][config][seed] = reduce_run(run["entries"], scoring, usual)
            out[scenario][config][seed]["_controller"] = run.get("controller")
            out[scenario][config][seed]["_cpu"] = run.get("cpu")
            names[scenario] = run["name"]
    return out, names


def interval(values, rng, fn=np.median):
    """The statistic and its 95% bootstrap interval, percentile method."""
    values = np.asarray(values, float)
    if len(values) < 2:
        return float(fn(values)), None, None
    picks = rng.integers(0, len(values), (RESAMPLES, len(values)))
    low, high = np.percentile(fn(values[picks], axis=1), [2.5, 97.5])
    return float(fn(values)), float(low), float(high)


def compare(a, b, rng, noise=0.0):
    """a against b, paired by seed; negative means a is better. Returns
    None when they share no seed."""
    seeds = sorted(set(a) & set(b))
    if not seeds:
        return None
    diff = np.array([a[s] - b[s] for s in seeds])
    median, low, high = interval(diff, rng)
    out = {"n": len(seeds), "median": median, "low": low, "high": high, "p": None, "effect": None}
    nonzero = diff[diff != 0]
    if len(nonzero):
        ranks = stats.rankdata(np.abs(nonzero))
        plus, minus = ranks[nonzero > 0].sum(), ranks[nonzero < 0].sum()
        out["effect"] = float((plus - minus) / (plus + minus))
        if len(seeds) >= 6:
            out["p"] = float(stats.wilcoxon(diff, zero_method="wilcox").pvalue)
    # A difference counts when its whole interval is beyond the noise
    # between identical runs. With too few seeds for an interval, nothing
    # is claimed either way.
    if low is None or len(seeds) < 6:
        out["verdict"] = "too few seeds"
    elif high < -noise:
        out["verdict"] = "better"
    elif low > noise:
        out["verdict"] = "worse"
    else:
        out["verdict"] = "tie"
    return out


def holm(results):
    """Holm's correction over the comparisons that have a p-value."""
    tested = sorted((r for r in results if r["p"] is not None), key=lambda r: r["p"])
    worst = 0.0
    for i, r in enumerate(tested):
        worst = max(worst, min((len(tested) - i) * r["p"], 1.0))
        r["p_holm"] = worst


def noise_bands(aa, core):
    """{scenario: {measure: band}}: the 95th percentile of the difference
    between two B2 runs of the same seed."""
    bands = {}
    for scenario, by_config in aa.items():
        again, first = by_config.get("B2", {}), core.get(scenario, {}).get("B2", {})
        seeds = sorted(set(again) & set(first))
        if len(seeds) < 3:
            continue
        bands[scenario] = {
            measure: float(np.percentile([abs(again[s][measure] - first[s][measure]) for s in seeds], 95))
            for measure in first[seeds[0]] if not measure.startswith("_") and all(measure in again[s] for s in seeds)
        }
    return bands


def _num(v, digits=2):
    return "" if v is None else f"{v:,.{digits}f}"


def _cell(c, digits=2):
    if c is None:
        return ""
    text = f"{c['verdict']}: {c['median']:+,.{digits}f}"
    if c["low"] is not None:
        text += f" ({c['low']:+,.{digits}f} to {c['high']:+,.{digits}f})"
    if c["p"] is not None:
        text += f", p {c.get('p_holm', c['p']):.3f}"
    if c["effect"] is not None:
        text += f", r {c['effect']:+.2f}"
    return text


def cpu_table(runs, order, configs, names):
    """CPU per component: the share of one core each part used over the
    run, mean over seeds. Runs from before CPU was recorded are left out."""
    rows = []
    for s in order:
        for c in configs:
            per = defaultdict(list)
            for m in runs[s].get(c, {}).values():
                seconds = m.get("_seconds") or 200
                for part, cpu in (m.get("_cpu") or {}).items():
                    per[part].append(100 * cpu / seconds)
                ctl = m.get("_controller") or {}
                if "cpu_s" in ctl:
                    per["controller"].append(100 * ctl["cpu_s"] / seconds)
            if per:
                rows.append((s, c, {p: float(np.mean(v)) for p, v in per.items()}))
    if not rows:
        return []
    parts = sorted({p for _, _, d in rows for p in d})
    lines = ["## CPU per component, % of one core over the run", "",
             "| Scenario | Configuration | " + " | ".join(parts) + " |", "| --- | --- |" + " --- |" * len(parts)]
    for s, c, d in rows:
        lines.append(f"| {s} | {c} | " + " | ".join(_num(d.get(p), 2) for p in parts) + " |")
    return lines + ["", "Measured on the reference laptop (12th-gen Intel i5, 16 threads). Router CPU use must be measured "
                         "separately. Runtime and classifier selections are recorded in each run's environment.json; "
                         "B4 uses the Python rules reference.", ""]


def markdown(results, aa=None, scoring_path=None, seed=1):
    scoring = yaml.safe_load(Path(scoring_path or damage.SCORING).read_text())
    rng = np.random.default_rng(seed)
    runs, names = reduce(results, scoring)
    bands = noise_bands(reduce(aa, scoring)[0], runs) if aa else {}
    order = sorted(runs, key=lambda s: int(s[1:]))
    configs = [c for c in table.ORDER if any(c in runs[s] for s in order)]

    lines = [f"# Matrix results: `{Path(results).name}/`", "",
             "Lower is better everywhere. Cost is 0 for as good as it gets and 1 for unusable, per traffic entry, "
             "summed over a scenario's entries. Intervals are 95% bootstrap intervals of the median over seeds. "
             "A comparison reads \"verdict: median paired difference (interval), p, r\"; negative means the first "
             "configuration is better; r is the rank-biserial effect size, -1 when it was better on every seed.", ""]
    lines += ["## Runs", "", "| Scenario | " + " | ".join(configs) + " |", "| --- |" + " --- |" * len(configs)]
    for s in order:
        lines.append(f"| {s} {names[s]} | " + " | ".join(str(len(runs[s].get(c, {}))) for c in configs) + " |")
    lines += ["", "Valid runs per scenario and configuration. "
              + ("Noise bands from A/A runs are applied." if bands else "No A/A runs: differences are judged against zero."), ""]

    comparisons, pairs = {}, [p for p in KEY + OTHERS if all(c in configs for c in p)]
    for s in order:
        for a, b in pairs:
            if a in runs[s] and b in runs[s]:
                va = {seed_: m[COST] for seed_, m in runs[s][a].items()}
                vb = {seed_: m[COST] for seed_, m in runs[s][b].items()}
                comparisons[s, a, b] = compare(va, vb, rng, bands.get(s, {}).get(COST, 0.0))
    holm([c for (s, a, b), c in comparisons.items() if c and (a, b) not in KEY])

    lines += ["## Summed cost, configuration against configuration", "",
              "| Scenario | " + " | ".join(f"{a} against {b}" for a, b in pairs) + " |", "| --- |" + " --- |" * len(pairs)]
    for s in order:
        lines.append(f"| {s} | " + " | ".join(_cell(comparisons.get((s, a, b))) for a, b in pairs) + " |")
    lines += ["", "O2 against B4 and O2 against O1 were named in advance; the p-values of the others are "
              "Holm-corrected.", ""]

    lines += ["## Summed cost, median over seeds", "", "| Scenario | " + " | ".join(configs) + " |",
              "| --- |" + " --- |" * len(configs)]
    for s in order:
        cells = []
        for c in configs:
            values = [m[COST] for m in runs[s].get(c, {}).values()]
            if not values:
                cells.append("")
                continue
            median, low, high = interval(values, rng)
            cells.append(_num(median) + (f" ({_num(low)} to {_num(high)})" if low is not None else ""))
        lines.append(f"| {s} | " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## Calls and games", "", "| Scenario | Measure | " + " | ".join(configs) + " |",
              "| --- | --- |" + " --- |" * len(configs)]
    for s in order:
        measures = sorted({m for c in runs[s].values() for run in c.values() for m in run
                           if m.endswith(("late frames %", "latency p95 ms"))})
        for measure in measures:
            cells = []
            for c in configs:
                values = [m[measure] for m in runs[s].get(c, {}).values() if measure in m]
                if not values:
                    cells.append("")
                    continue
                median, low, high = interval(values, rng)
                cells.append(_num(median, 1) + (f" ({_num(low, 1)} to {_num(high, 1)})" if low is not None else ""))
            lines.append(f"| {s} | {measure} | " + " | ".join(cells) + " |")
    lines.append("")

    counted = ["labelled", "relabelled", "triggers", "rechecks", "rate_changes", "watched", "regressed", "rolled_back",
               "rejected"]
    lines += ["## What the controller did, mean per run", "", "| Scenario | Configuration | " + " | ".join(counted) + " |",
              "| --- | --- |" + " --- |" * len(counted)]
    for s in order:
        for c in configs:
            reports = [m["_controller"]["flows"] for m in runs[s].get(c, {}).values() if m.get("_controller")]
            if reports:
                lines.append(f"| {s} | {c} | " + " | ".join(_num(np.mean([r.get(k, 0) for r in reports]), 1)
                                                               for k in counted) + " |")
    lines.append("")
    lines += cpu_table(runs, order, configs, names)
    if bands:
        lines += ["## Noise between identical runs", "", "| Scenario | Summed cost, 95th percentile of the difference |",
                  "| --- | --- |", *[f"| {s} | {_num(bands[s].get(COST), 3)} |" for s in order if s in bands], ""]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.report", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", required=True)
    parser.add_argument("--aa", help="results of a second set of B2 runs with the same seeds")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    text = markdown(args.results, args.aa)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()

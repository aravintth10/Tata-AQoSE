"""Puts runs of the same scenarios under different configurations side by
side: what each traffic entry experienced, as numbers and as one cost.

  python -m aqos_ml.run table --results results-live

The numbers and the cost are the ones the damage table is built from
(aqos_ml.damage), so a scenario's result and a damage cell mean the same
thing. Data transfers and web pages are judged against the same entry in
the B2 run of the same scenario and seed: plain CAKE is the usual.
"""

import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml

ORDER = ["B0", "B2", "B4", "O1", "O2", "B3"]
USUAL = "B2"


def _numbers(entry):
    """The few numbers worth a column for one traffic entry."""
    if "down" in entry and isinstance(entry["down"], dict):
        worst = lambda key: max((entry[d].get(key) for d in ("up", "down") if entry[d].get(key) is not None),  # noqa: E731
                                default=None)
        out = {"latency p95 ms": worst("latency_p95_ms"), "loss %": None if worst("loss_ratio") is None else 100 * worst("loss_ratio")}
        if worst("frame_miss_ratio") is not None:
            out["late frames %"] = 100 * worst("frame_miss_ratio")
        return out
    if "stall_fraction" in entry:
        return {"stalled %": 100 * entry["stall_fraction"], "bitrate kbit/s": entry["bitrate_kbit"]}
    if "page_load_median_ms" in entry:
        return {"page load ms": entry["page_load_median_ms"], "pages": entry["pages"]}
    return {"throughput Mbit/s": entry["throughput_kbit"] / 1000}


def collect(results):
    """{(scenario, seed): {config: measured run}} from every valid run."""
    from .. import damage
    runs = defaultdict(dict)
    for manifest_path in sorted(Path(results).glob("*/manifest.json")):
        if manifest_path.parent.is_symlink():
            continue
        manifest = json.loads(manifest_path.read_text())
        if not manifest["valid"]:
            continue
        run_dir = manifest_path.parent
        doc = yaml.safe_load((run_dir / "scenario.yaml").read_text())
        # Reducing a run means reading every packet's latency sample. A
        # finished run never changes, so the reduction is kept beside it.
        kept = run_dir / "measured.json"
        if kept.exists():
            measured = {"entries": json.loads(kept.read_text())}
        else:
            measured = {"entries": damage.measure_entries(run_dir)}
            kept.write_text(json.dumps(measured["entries"]) + "\n")
        report = run_dir / "controller.json"
        measured["controller"] = json.loads(report.read_text()) if report.exists() else None
        cpu = run_dir / "cpu.json"
        measured["cpu"] = json.loads(cpu.read_text()) if cpu.exists() else None
        measured["name"] = doc["name"]
        runs[manifest["scenario_id"], manifest["seed"]][manifest["config"]] = measured
    return runs


def markdown(results, scoring_path=None):
    from .. import damage
    scoring = yaml.safe_load(Path(scoring_path or damage.SCORING).read_text())
    runs = collect(results)
    lines = ["# Live runs, side by side", "",
             f"From `{Path(results).name}/`. Cost is 0 for as good as it gets and 1 for unusable, by the thresholds "
             f"in `{Path(scoring_path or damage.SCORING).name}`; a download or a web page is judged against the "
             f"same one under {USUAL}.", ""]
    totals = defaultdict(list)
    for (scenario, seed), by_config in sorted(runs.items(), key=lambda kv: (int(kv[0][0][1:]), kv[0][1])):
        configs = [c for c in ORDER if c in by_config] + sorted(set(by_config) - set(ORDER))
        name = next(iter(by_config.values()))["name"]
        lines += [f"## {scenario}: {name} (seed {seed})", "",
                  "| Entry | Measure | " + " | ".join(configs) + " |", "| --- | --- |" + " --- |" * len(configs)]
        usual = by_config.get(USUAL, {}).get("entries", {})
        entry_ids = list(next(iter(by_config.values()))["entries"])
        summed = {c: 0.0 for c in configs}
        for tid in entry_ids:
            rows = defaultdict(dict)
            for config in configs:
                entry = by_config[config]["entries"].get(tid)
                if entry is None:
                    continue
                for measure, value in _numbers(entry).items():
                    rows[measure][config] = value
                cost = damage.cost(entry, scoring, usual.get(tid))
                rows["**cost**"][config] = cost
                summed[config] += cost
            profile = by_config[configs[0]]["entries"][tid]["profile"]
            for measure, values in rows.items():
                cells = []
                for config in configs:
                    v = values.get(config)
                    cells.append("" if v is None else f"{v:.2f}" if measure == "**cost**" else f"{v:,.1f}")
                lines.append(f"| {tid} ({profile}) | {measure} | " + " | ".join(cells) + " |")
        lines.append("| *all entries* | **summed cost** | " + " | ".join(f"**{summed[c]:.2f}**" for c in configs) + " |")
        for config in configs:
            totals[config].append(summed[config])
        notes = []
        for config in configs:
            report = by_config[config].get("controller")
            if report:
                flows = report["flows"]
                notes.append(f"{config}: {flows['labelled']} labels written, {flows.get('rate_changes', 0)} rate changes, "
                             f"{flows['rejected']} proposals rejected by the shield")
        lines += ["", *[f"- {n}" for n in notes], ""]
    if totals:
        configs = [c for c in ORDER if c in totals] + sorted(set(totals) - set(ORDER))
        lines += ["## Summed cost over the scenarios run", "",
                  "| | " + " | ".join(configs) + " |", "| --- |" + " --- |" * len(configs),
                  "| Scenarios | " + " | ".join(str(len(totals[c])) for c in configs) + " |",
                  "| Mean summed cost | " + " | ".join(f"{np.mean(totals[c]):.2f}" for c in configs) + " |", ""]
    return "\n".join(lines) + "\n"

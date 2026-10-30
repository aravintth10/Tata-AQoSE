"""Every number on the Round 1 one-slider, read from the results.

  PYTHONPATH=ml .venv/bin/python reports/round1/slide_numbers.py

Writes reports/round1/numbers.json: each figure with the file it came from,
so the slide (build_slide.py) never holds a number of its own. Live results
use only the seeds that every scenario and configuration has finished, so a
matrix still running gives a consistent interim slide.
"""

import json
import re
from pathlib import Path

import numpy as np
import yaml

from aqos_ml import charts, damage, report

REPO = Path(__file__).resolve().parents[2]
RESULTS = REPO / "results-core"
OFFLINE = REPO / "reports" / "head-to-head-v1-validation.json"
CONFIGS = ["B0", "B2", "B4", "O1", "O2"]
SEEDS_PLANNED = 10


def _matrix():
    scoring = yaml.safe_load(Path(damage.SCORING).read_text())
    runs, names = report.reduce(str(RESULTS), scoring)
    order = sorted(runs, key=lambda s: int(s[1:]))
    seeds = sorted(set.intersection(*[set(runs[s][c]) for s in order for c in CONFIGS]))
    return runs, names, order, seeds


def _median(runs, scenario, config, measure, seeds):
    return float(np.median([runs[scenario][config][k][measure] for k in seeds]))


def live(runs, names, order, seeds):
    src = f"{RESULTS.name}/, summed cost per seed over all scenarios, median over seeds {seeds[0]}-{seeds[-1]}"
    totals = {c: float(np.median([sum(runs[s][c][k][report.COST] for s in order) for k in seeds])) for c in CONFIGS}

    # O2 against plain CAKE and against the rules, run by run (same seed, same traffic).
    wins = ties = losses = worse_than_rules = 0
    for s in order:
        for k in seeds:
            d = runs[s]["O2"][k][report.COST] - runs[s]["B2"][k][report.COST]
            wins, ties, losses = wins + (d < -0.01), ties + (abs(d) <= 0.01), losses + (d > 0.01)
            worse_than_rules += runs[s]["O2"][k][report.COST] - runs[s]["B4"][k][report.COST] > 0.01

    # Per scenario: the report's verdict on O2 against plain CAKE.
    rng = np.random.default_rng(1)
    verdicts = {}
    for s in order:
        a = {k: runs[s]["O2"][k][report.COST] for k in seeds}
        b = {k: runs[s]["B2"][k][report.COST] for k in seeds}
        verdicts[s] = {"verdict": report.compare(a, b, rng)["verdict"], "name": names[s],
                       "cake": _median(runs, s, "B2", report.COST, seeds),
                       "ours": _median(runs, s, "O2", report.COST, seeds),
                       "cost": {c: _median(runs, s, c, report.COST, seeds) for c in CONFIGS}}

    late = lambda s, tid, c: _median(runs, s, c, f"{tid}: late frames %", seeds)  # noqa: E731
    s6 = {c: late("S6", "call", c) for c in CONFIGS}
    s7 = {c: late("S7", "game", c) for c in CONFIGS}

    # S6, one seed, second by second: the call's late frames under plain CAKE and ours.
    seed = seeds[0]
    timeline = {c: charts._per_second(RESULTS / f"S6-{c}-s{seed:04d}", "call") for c in ("B2", "O2")}
    s6doc = yaml.safe_load((RESULTS / f"S6-B2-s{seed:04d}" / "scenario.yaml").read_text())
    sharing_starts = next(t["start_s"] for t in s6doc["traffic"] if t["id"] == "sharing")

    # S2: time from the line dropping to the estimator's first cut.
    reactions = []
    for c in ("O1", "O2"):
        for k in seeds:
            run = RESULTS / f"S2-{c}-s{k:04d}"
            doc = yaml.safe_load((run / "scenario.yaml").read_text())
            drop = next(e["at_s"] for e in doc["events"] if e["type"] == "link_rate")
            for line in (run / "actions.jsonl").open():
                a = json.loads(line)
                if a["proposal"]["kind"] == "shaper_rate" and a["t_s"] >= drop and a.get("outcome") == "applied":
                    reactions.append((a["t_s"] - drop) * 1000)
                    break

    # S10: the watchdog, every configuration that had a controller to kill.
    failsafe = [json.loads(p.read_text())["seconds_to_safe_state"] for p in sorted(RESULTS.glob("S10-*/failsafe.json"))
                if int(p.parent.name.rsplit("-s", 1)[1]) in seeds]

    manifests = [json.loads(p.read_text()) for p in RESULTS.glob("S*-*-s*/manifest.json")
                 if int(p.parent.name.rsplit("-s", 1)[1]) in seeds]

    return {
        "seeds": {"value": len(seeds), "planned": SEEDS_PLANNED, "source": f"{RESULTS.name}/*/manifest.json"},
        "scenarios": {"value": len(order), "source": f"{RESULTS.name}/"},
        "total_cost": {"value": totals, "source": src},
        "less_damage_than_cake": {"value": 1 - totals["O2"] / totals["B2"], "source": src},
        "runs_vs_cake": {"wins": int(wins), "ties": int(ties), "losses": int(losses), "runs": len(order) * len(seeds),
                         "source": f"{RESULTS.name}/, O2 against B2 run by run, summed cost, tie within 0.01"},
        "runs_worse_than_rules": {"value": int(worse_than_rules),
                                  "source": f"{RESULTS.name}/, O2 against B4 run by run, summed cost"},
        "scenario_verdicts": {"value": verdicts, "source": f"{RESULTS.name}/, report.compare O2 against B2 (as reports/core.md)"},
        "s6_late_frames_pct": {"value": s6, "source": f"{RESULTS.name}/S6-*, call late frames %, median over seeds"},
        "s7_late_frames_pct": {"value": s7, "source": f"{RESULTS.name}/S7-*, game late frames %, median over seeds"},
        "s6_timeline": {"value": {c: {str(t): v for t, v in pts.items()} for c, pts in timeline.items()},
                        "seed": seed, "sharing_starts_s": sharing_starts,
                        "source": f"{RESULTS.name}/S6-{{B2,O2}}-s{seed:04d}/metrics.jsonl.gz, call frame_miss per second"},
        "s2_reaction_ms": {"value": float(np.median(reactions)), "min": min(reactions), "max": max(reactions),
                           "source": f"{RESULTS.name}/S2-{{O1,O2}}-*/actions.jsonl, first applied rate cut after the drop"},
        "watchdog_s": {"min": min(failsafe), "max": max(failsafe), "n": len(failsafe), "limit": 3.0,
                       "source": f"{RESULTS.name}/S10-*/failsafe.json"},
        "runs_valid": {"valid": sum(bool(m.get("valid")) for m in manifests), "runs": len(manifests),
                       "source": f"{RESULTS.name}/*/manifest.json"},
    }


def _labels(run):
    """The flow_label actions of one run, in order: (t_s, flow_label, reason, outcome)."""
    out = []
    for line in (run / "actions.jsonl").open():
        a = json.loads(line)
        if a["proposal"]["kind"] == "flow_label":
            out.append((a["t_s"], a["proposal"]["flow_label"], a["proposal"]["reason"], a.get("outcome")))
    return out


def _confidence(reason):
    m = re.search(r"\((\d+)% confident\)", reason)
    return int(m.group(1)) if m else None


def mechanism(seeds):
    """Real examples of the three steps: a first label, a relabel, and the
    damage rows the placement reads."""
    seed = seeds[0]
    # Classify: the call's first label in S6.
    s6 = RESULTS / f"S6-O2-s{seed:04d}"
    t, fl, reason, _ = next(x for x in _labels(s6) if x[1]["top_class"] == "multimedia_conferencing"
                            and x[1]["previous_tin"] is None)
    first = {"t_s": t, "class": fl["top_class"], "tin": fl["tin"], "confidence": _confidence(reason), "reason": reason}
    # Re-classify: in S8, the first flow sent to Voice or Video that a recheck moved to Bulk as a background transfer.
    s8 = RESULTS / f"S8-O2-s{seed:04d}"
    labels = _labels(s8)
    firsts = {x[1]["flow_id"]: x for x in labels if x[1]["previous_tin"] is None}
    moved = next(x for x in labels if x[1]["previous_tin"] in ("voice", "video") and x[1]["tin"] == "bulk"
                 and x[1]["top_class"] == "low_priority_data" and x[3] == "applied")
    began = firsts[moved[1]["flow_id"]]
    doc = yaml.safe_load((s8 / "scenario.yaml").read_text())
    sharing = next(e for e in doc["traffic"] if e["profile"] == "p2p")
    relabel = {"first_t_s": began[0], "first_class": began[1]["top_class"], "first_tin": began[1]["tin"],
               "first_confidence": _confidence(began[2]), "after_s": moved[0] - began[0],
               "class": moved[1]["top_class"], "tin": moved[1]["tin"], "confidence": _confidence(moved[2]),
               "why": moved[2].split(". Looks like")[0], "flow_host_is_sharing_host": True,
               "sharing_starts_s": sharing["start_s"], "reason": moved[2]}
    per_run = []
    for k in seeds:
        labels = _labels(RESULTS / f"S8-O2-s{k:04d}")
        per_run.append(sum(1 for x in labels if x[1]["previous_tin"] is not None and x[3] == "applied"))
    table = yaml.safe_load((REPO / "configs" / "damage_table.yaml").read_text())["damage"]
    return {"mechanism": {
        "first_label": {**first, "source": f"{s6.relative_to(REPO)}/actions.jsonl"},
        "relabel": {**relabel, "source": f"{s8.relative_to(REPO)}/actions.jsonl (flow on the laptop, opened with the "
                    "file sharing)"},
        "relabels_per_run_s8": {"value": float(np.mean(per_run)), "source": f"{RESULTS.name}/S8-O2-*/actions.jsonl"},
        "damage": {"value": {c: table[c] for c in ("multimedia_conferencing", "low_priority_data")},
                   "source": "configs/damage_table.yaml"},
    }}


def offline():
    doc = json.loads(OFFLINE.read_text())
    rows = {r["contestant"]: r for r in doc["results"]}
    qosify = rows["Rules, qosify defaults"]
    ours = rows["LightGBM, expected damage (O2)"]
    chosen = (REPO / "models" / f"chosen-{doc['dataset']['name']}.txt").read_text().strip()
    meta = json.loads((REPO / "models" / chosen / "meta.json").read_text())
    src = str(OFFLINE.relative_to(REPO))
    return {
        "held_out": {"flows": doc["flows"], "sessions": doc["sessions"], "split": doc["split"], "source": src},
        "accuracy": {"value": ours["accuracy"], "source": src},
        "macro_f1": {"value": ours["macro_f1"], "source": src},
        "recall": {"value": ours["recall"], "flows": doc["flows_by_class"], "source": src},
        "rules_accuracy": {"value": qosify["accuracy"], "macro_f1": qosify["macro_f1"], "source": src},
        "cloud_gaming_to_bulk": {"qosify": qosify["focus"]["cloud_gaming"]["to_bulk"],
                                 "ours": ours["focus"]["cloud_gaming"]["to_bulk"],
                                 "flows": ours["focus"]["cloud_gaming"]["flows"], "source": src},
        "classify_ms": {"value": meta["single_record_us"] / 1000, "model": chosen,
                        "source": f"models/{chosen}/meta.json single_record_us"},
        "classes": {"value": len(meta["classes"]), "source": f"models/{chosen}/meta.json"},
    }


def tests():
    py = sum(len(re.findall(r"^\s*def test_", p.read_text(), re.M)) for p in (REPO / "ml").rglob("test_*.py"))
    go = sum(len(re.findall(r"^func Test", p.read_text(), re.M)) for p in REPO.rglob("*_test.go")
             if ".venv" not in p.parts)
    return {"unit_tests": {"value": py + go, "python": py, "go": go, "source": "ml/**/test_*.py, **/*_test.go"}}


def main():
    runs, names, order, seeds = _matrix()
    out = {**live(runs, names, order, seeds), **mechanism(seeds), **offline(), **tests()}
    path = Path(__file__).with_name("numbers.json")
    path.write_text(json.dumps(out, indent=1) + "\n")
    print(path.relative_to(REPO))


if __name__ == "__main__":
    main()

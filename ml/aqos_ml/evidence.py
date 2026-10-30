"""Evidence the problem statement asks for by name, computed from finished
runs (no new runs):

  python -m aqos_ml.evidence --results results-final --fresh results-fresh

- Bulk progress and starvation: every bulk entry's throughput under AQoSE
  against the same entry under plain CAKE, same seed; seconds in which it
  got almost nothing.
- Fairness between devices: Jain's index over the devices' throughput.
- Jitter, loss and throughput of the calls and games.
- Link estimation in S2: the shaper rate against the true capacity, time to
  detect the drop and to get under it, steady-state error.
- A dashboard of one run: latency, jitter, loss, throughput, queue delay and
  backlog per tin, per-device throughput and fairness.

Writes reports/evidence.md and charts in reports/charts/.
"""

import argparse
import functools
import gzip
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml

from . import charts
from .charts import COLOR, INK, MUTED, SECONDARY, plt

REPO = Path(__file__).resolve().parents[2]
BULK = {"bulk_download", "game_update", "backup_upload", "p2p"}
INTERACTIVE = {"voice_call", "video_call", "gaming", "cloud_gaming"}
STARVED_KBIT = 100  # a second in which a bulk transfer got less than this


def _metrics(run, only=None):
    """The run's metric samples; with `only`, just the lines naming one of
    those metrics (the per-packet latency lines are most of a file)."""
    path = Path(run) / "metrics.jsonl"
    with (open(path) if path.exists() else gzip.open(str(path) + ".gz", "rt")) as f:
        for line in f:
            if only is None or any(f'"{m}"' in line for m in only):
                yield json.loads(line)


def _runs(results):
    """{(scenario, config, seed): run dir} for valid runs."""
    out = {}
    for manifest in Path(results).glob("*/manifest.json"):
        m = json.loads(manifest.read_text())
        if m.get("valid"):
            out[m["scenario_id"], m["config"], m["seed"]] = manifest.parent
    return out


@functools.lru_cache(maxsize=None)
def _summary(run):
    return summarise(Path(run))


def summarise(run):
    """Per traffic entry and direction: mean throughput, starved seconds,
    median jitter, mean loss; per device and direction: mean throughput.
    Over the measured time only."""
    doc = yaml.safe_load((run / "scenario.yaml").read_text())
    lo, hi = doc["warmup_s"], doc["warmup_s"] + doc["duration_s"]
    entries = {t["id"]: t for t in doc["traffic"]}
    tput = defaultdict(list)
    jitter, loss = defaultdict(list), defaultdict(list)
    for s in _metrics(run, ("throughput_kbit", "jitter_ms", "loss_ratio")):
        if s.get("source") != "generator" or not lo <= s["t_s"] < hi:
            continue
        key = (s["traffic_id"], s.get("direction"))
        if s["metric"] == "throughput_kbit":
            tput[key].append(s["value"])
        elif s["metric"] == "jitter_ms":
            jitter[key].append(s["value"])
        elif s["metric"] == "loss_ratio":
            loss[key].append(s["value"])
    flows, hosts = {}, defaultdict(float)
    for (tid, d), values in tput.items():
        entry = entries[tid]
        start = max(float(entry["start_s"]), lo)
        stop = hi if entry.get("stop_s") is None else min(float(entry["stop_s"]), hi)
        active = max(stop - start, 1.0)
        mean = float(np.sum(values)) / active  # seconds with no sample carried nothing
        flows[tid, d] = {
            "profile": entry["profile"], "host": entry["host"], "kbit": mean,
            "starved_s": int(max(active - len(values), 0) + sum(v < STARVED_KBIT for v in values)),
            "active_s": active,
            "jitter_ms": float(np.median(jitter[tid, d])) if jitter[tid, d] else None,
            "loss": float(np.mean(loss[tid, d])) if loss[tid, d] else None,
        }
        hosts[entry["host"], d] += mean
    return flows, dict(hosts)


def jain(values):
    x = np.asarray([v for v in values if v > 0], float)
    return float(x.sum() ** 2 / (len(x) * (x ** 2).sum())) if len(x) > 1 else None


def _med(values):
    values = [v for v in values if v is not None]
    return float(np.median(values)) if values else None


def bulk_and_fairness(runs, scenarios):
    rows, fair = [], []
    for s in scenarios:
        seeds = sorted({seed for (sc, c, seed) in runs if sc == s and c == "O2"}
                       & {seed for (sc, c, seed) in runs if sc == s and c == "B2"})
        if not seeds:
            continue
        per = {c: {seed: _summary(str(runs[s, c, seed])) for seed in seeds if (s, c, seed) in runs}
               for c in ("B2", "B4", "O2")}
        keys = sorted({k for f, _ in per["O2"].values() for k in f if f[k]["profile"] in BULK})
        for key in keys:
            ratio = [per["O2"][sd][0][key]["kbit"] / per["B2"][sd][0][key]["kbit"]
                     for sd in seeds if per["B2"][sd][0].get(key, {}).get("kbit")]
            rows.append({
                "scenario": s, "entry": f"{key[0]} ({key[1]})", "profile": per["O2"][seeds[0]][0][key]["profile"],
                "cake_mbit": _med([per["B2"][sd][0][key]["kbit"] / 1000 for sd in seeds]),
                "rules_mbit": _med([per["B4"][sd][0][key]["kbit"] / 1000 for sd in per["B4"]
                                    if key in per["B4"][sd][0]]),
                "aqose_mbit": _med([per["O2"][sd][0][key]["kbit"] / 1000 for sd in seeds]),
                "ratio": _med(ratio), "ratio_min": min(ratio) if ratio else None,
                "starved_max_pct": max(100 * per["O2"][sd][0][key]["starved_s"] / per["O2"][sd][0][key]["active_s"]
                                       for sd in seeds),
                "starved_cake_max_pct": max(100 * per["B2"][sd][0][key]["starved_s"] / per["B2"][sd][0][key]["active_s"]
                                            for sd in seeds),
            })
        for d in ("down", "up"):
            j = {c: _med([jain([v for (h, dd), v in hosts.items() if dd == d]) for _, hosts in per[c].values()])
                 for c in ("B2", "O2")}
            n = len({h for _, hosts in per["O2"].values() for (h, dd) in hosts if dd == d})
            if n > 1:
                fair.append({"scenario": s, "direction": d, "devices": n, "cake": j["B2"], "aqose": j["O2"]})
    return rows, fair


def interactive(runs, scenarios):
    rows = []
    for s in scenarios:
        seeds = sorted({seed for (sc, c, seed) in runs if sc == s and c == "O2"})
        stats = defaultdict(lambda: defaultdict(list))
        for c in ("B2", "B4", "O2", "B3"):
            for seed in seeds:
                if (s, c, seed) not in runs:
                    continue
                flows, _ = _summary(str(runs[s, c, seed]))
                for (tid, d), f in flows.items():
                    if f["profile"] in INTERACTIVE:
                        stats[tid, d][c, "jitter"].append(f["jitter_ms"])
                        stats[tid, d][c, "loss"].append(f["loss"])
                        stats[tid, d][c, "kbit"].append(f["kbit"])
        for (tid, d), v in sorted(stats.items()):
            row = {"scenario": s, "flow": f"{tid} {d}"}
            for c in ("B2", "B4", "O2", "B3"):
                row[c] = {m: _med(v[c, m]) for m in ("jitter", "loss", "kbit")} if v[c, "kbit"] else None
            rows.append(row)
    return rows


def estimator(runs, out):
    """S2: the download shaper against the true capacity, for AQoSE and
    cake-autorate; plain CAKE stays at its fixed rate."""
    result, traces = {}, {}
    for c in ("O2", "B3"):
        det, conv, err, over = [], [], [], []
        for (s, cfg, seed), run in sorted(runs.items()):
            if s != "S2" or cfg != c:
                continue
            true, shaper, drop_at = [], [], None
            for m in _metrics(run, ("capacity_true_kbit", "shaper_rate_kbit")):
                if m.get("metric") == "capacity_true_kbit" and m.get("direction") == "down":
                    true.append((m["t_s"], m["value"]))
                elif m.get("metric") == "shaper_rate_kbit" and m.get("direction") == "down":
                    shaper.append((m["t_s"], m["value"]))
            drop_at = max(t for t, _ in true)
            low = min(v for _, v in true)
            before = [v for t, v in shaper if t < drop_at]
            after = [(t, v) for t, v in shaper if t >= drop_at]
            first_cut = next((t for t, v in after if v < before[-1]), None)
            under = next((t for t, v in after if v <= low), None)
            steady = [v for t, v in after if t >= drop_at + 10]
            det.append(first_cut - drop_at if first_cut is not None else None)
            conv.append(under - drop_at if under is not None else None)
            err.append(100 * float(np.mean([abs(v - low) for v in steady])) / low)
            over.append(100 * float(np.mean([v > low for v in steady])))
            if seed == 3:
                traces[c] = (shaper, true)
        result[c] = {"runs": len(err), "detect_s": _med(det), "under_s": _med(conv), "abs_error_pct": _med(err),
                     "time_above_pct": _med(over)}
    if traces:
        fig, ax = plt.subplots(figsize=(9, 3.6))
        fig.subplots_adjust(left=0.09, right=0.98, top=0.78, bottom=0.15)
        shaper, true = traces["O2"]
        tt = [t for t, _ in true] + [200]
        tv = [v for _, v in true] + [true[-1][1]]
        ax.step([t for t in tt], [v / 1000 for v in tv], where="post", color=INK, linewidth=2.2, label="True capacity")
        ax.plot([0, 200], [95, 95], color=COLOR["B2"], linewidth=1.6, linestyle="--", label="Plain CAKE (fixed)")
        if "B3" in traces:
            ax.plot([t for t, _ in traces["B3"][0]], [v / 1000 for _, v in traces["B3"][0]], color=COLOR["B3"],
                    linewidth=1.6, label="cake-autorate")
        ax.plot([t for t, _ in shaper], [v / 1000 for _, v in shaper], color=COLOR["O2"], linewidth=2,
                label="AQoSE estimator")
        ax.set_xlim(60, 200)
        ax.set_ylim(0, 110)
        ax.set_xlabel("seconds into the run (the line drops at 100 s)")
        ax.set_ylabel("download rate, Mbit/s")
        ax.yaxis.grid(True, color=charts.GRID)
        ax.legend(loc="center right", bbox_to_anchor=(1.0, 0.62), frameon=False, fontsize=8.5)
        charts._title(fig, "Link estimation in S2: shaper rate against the true capacity",
                      "Seed 3; the shaper has to sit at or just below the line for CAKE to control the queue")
        charts._save(fig, out, "estimator-S2.png")
    return result


def dashboard(runs, out, scenario="S5", seed=3):
    """One run of plain CAKE and one of AQoSE, same seed, as a dashboard."""
    panels = {}
    for c in ("B2", "O2"):
        run = runs.get((scenario, c, seed))
        if run is None:
            return None
        series = defaultdict(lambda: defaultdict(list))
        for m in _metrics(run):
            t = m["t_s"]
            if m.get("source") == "generator" and m.get("traffic_id") == "call":
                if m["metric"] in ("jitter_ms", "loss_ratio") and m.get("direction") == "up":
                    series[m["metric"]][int(t)].append(m["value"])
                elif m["metric"] == "latency_ms" and m.get("direction") == "up":
                    series["latency"][int(t)].append(m["value"])
            if m.get("source") == "generator" and m["metric"] == "throughput_kbit" and m.get("direction") == "up":
                series["up:" + m["traffic_id"]][int(t)].append(m["value"])
            if m.get("source") == "cake" and m.get("direction") == "up" and m["metric"] in ("queue_delay_ms", "queue_backlog_bytes"):
                series[m["metric"] + ":" + m["tin"]][int(t)].append(m["value"])
        panels[c] = {k: (sorted(v), [float(np.max(v[t])) if k == "latency" else float(np.mean(v[t])) for t in sorted(v)])
                     for k, v in series.items()}
    fig, axes = plt.subplots(3, 2, figsize=(12, 8.8), sharex=True)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.88, bottom=0.06, hspace=0.5, wspace=0.18)
    name = {"B2": "Plain CAKE", "O2": "AQoSE"}
    line = {"B2": "--", "O2": "-"}

    def single(ax, key, title):
        for c in ("B2", "O2"):
            if key in panels[c]:
                t, v = panels[c][key]
                ax.plot(t, v, line[c], color=COLOR[c], linewidth=1.4, label=name[c])
        ax.set_title(title, fontsize=9.5, loc="left", color=INK)

    single(axes[0, 0], "latency", "Call latency, worst packet each second (ms)")
    single(axes[0, 1], "jitter_ms", "Call jitter (ms)")
    single(axes[1, 0], "loss_ratio", "Call packet loss (share of packets)")
    ax = axes[1, 1]
    for c in ("B2", "O2"):
        for flow, color in (("call", COLOR[c]), ("backup", "#52514e" if c == "O2" else "#b9b8b0")):
            if "up:" + flow in panels[c]:
                t, v = panels[c]["up:" + flow]
                ax.plot(t, [x / 1000 for x in v], line[c], color=color, linewidth=1.3, label=f"{flow}, {name[c]}")
    ax.set_title("Upload throughput: the call and the backup (Mbit/s)", fontsize=9.5, loc="left", color=INK)
    tin_color = {"voice": "#1d3557", "video": "#457b9d", "best_effort": "#8fb3c9", "bulk": MUTED}
    for ax, key, title, scale in ((axes[2, 0], "queue_delay_ms", "AQoSE: CAKE queue delay per tin, upload (ms)", 1),
                                  (axes[2, 1], "queue_backlog_bytes", "AQoSE: CAKE backlog per tin, upload (kB)", 1000)):
        for tin in ("voice", "video", "best_effort", "bulk"):
            if f"{key}:{tin}" in panels["O2"]:
                t, v = panels["O2"][f"{key}:{tin}"]
                ax.plot(t, [x / scale for x in v], color=tin_color[tin], linewidth=1.3, label=tin.replace("_", " ").title())
        ax.set_title(title, fontsize=9.5, loc="left", color=INK)
    for ax in axes.flat:
        top = ax.get_ylim()[1]
        ax.set_ylim(bottom=0, top=top * 1.45)
        ax.yaxis.grid(True, color=charts.GRID)
        ax.legend(fontsize=7.5, frameon=False, loc="upper right", ncol=2)
        ax.tick_params(labelsize=8)
    for ax in axes[-1]:
        ax.set_xlabel("seconds into the run", fontsize=8.5)
    charts._title(fig, f"Run dashboard: {scenario}, seed {seed}",
                  "A work call on a laptop that is also backing up; the backup starts at 30 s. Dashed: plain CAKE; solid: AQoSE")
    return charts._save(fig, out, f"dashboard-{scenario}.png")


def _f(v, d=1):
    return "" if v is None else f"{v:,.{d}f}"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m aqos_ml.evidence", description=__doc__.split("\n\n")[0])
    parser.add_argument("--results", default=REPO / "results-final")
    parser.add_argument("--fresh", default=REPO / "results-fresh")
    parser.add_argument("--out", default=REPO / "reports")
    args = parser.parse_args(argv)
    out = Path(args.out)
    runs = _runs(args.results)
    scenarios = sorted({s for s, _, _ in runs}, key=lambda s: int(s[1:]))
    bulk, fair = bulk_and_fairness(runs, scenarios)
    fresh = _runs(args.fresh) if Path(args.fresh).exists() else {}
    fbulk, ffair = bulk_and_fairness(fresh, sorted({s for s, _, _ in fresh}, key=lambda s: int(s[1:]))) if fresh else ([], [])
    inter = interactive(runs, scenarios)
    est = estimator(runs, out / "charts")
    dash = dashboard(runs, out / "charts")

    lines = ["# Evidence for the problem statement's metrics", "",
             f"From `{Path(args.results).name}` (frozen system, seeds 0 to 9) and `{Path(args.fresh).name}` (fresh seeds 10 to 14). "
             "Medians over seeds. No new runs: everything is computed from the runs' per-second metrics.", "",
             "## Bulk progress and starvation", "",
             f"Throughput of every bulk entry, and its throughput under AQoSE as a share of the same entry under plain CAKE "
             f"on the same seed. A starved second is one in which the transfer got under {STARVED_KBIT} kbit/s.", "",
             "| Scenario | Bulk entry | CAKE, Mbit/s | Rules, Mbit/s | AQoSE, Mbit/s | AQoSE / CAKE, median (lowest) | Starved seconds, worst run: AQoSE / CAKE |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in bulk:
        lines.append(f"| {r['scenario']} | {r['entry']} | {_f(r['cake_mbit'],2)} | {_f(r['rules_mbit'],2)} | {_f(r['aqose_mbit'],2)} | "
                     f"{_f(100*r['ratio'],0)}% ({_f(100*r['ratio_min'],0)}%) | {_f(r['starved_max_pct'],1)}% / {_f(r['starved_cake_max_pct'],1)}% |")
    if fbulk:
        lines += ["", "Fresh seeds:", "", "| Scenario | Bulk entry | CAKE, Mbit/s | AQoSE, Mbit/s | AQoSE / CAKE, median (lowest) | Starved seconds, worst run: AQoSE / CAKE |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for r in fbulk:
            lines.append(f"| {r['scenario']} | {r['entry']} | {_f(r['cake_mbit'],2)} | {_f(r['aqose_mbit'],2)} | "
                         f"{_f(100*r['ratio'],0)}% ({_f(100*r['ratio_min'],0)}%) | {_f(r['starved_max_pct'],1)}% / {_f(r['starved_cake_max_pct'],1)}% |")
    lines += ["", "## Fairness between devices", "",
              "Jain's index over the devices' mean throughput in one direction (1 is perfectly even). Devices with "
              "light traffic lower it under any scheduler, so the comparison that matters is CAKE against AQoSE.", "",
              "| Scenario | Direction | Devices | Plain CAKE | AQoSE |", "| --- | --- | --- | --- | --- |"]
    for r in fair + [dict(x, scenario=x["scenario"] + " (fresh)") for x in ffair]:
        lines.append(f"| {r['scenario']} | {r['direction']} | {r['devices']} | {_f(r['cake'],3)} | {_f(r['aqose'],3)} |")
    lines += ["", "## Calls and games: jitter, loss, throughput", "",
              "| Scenario | Flow | Jitter ms: CAKE / rules / AQoSE | Loss %: CAKE / rules / AQoSE | Throughput kbit/s: CAKE / AQoSE |",
              "| --- | --- | --- | --- | --- |"]
    for r in inter:
        if not r["O2"]:
            continue
        j = " / ".join(_f(r[c]["jitter"], 1) if r[c] else "-" for c in ("B2", "B4", "O2"))
        l = " / ".join(_f(100 * r[c]["loss"], 2) if r[c] else "-" for c in ("B2", "B4", "O2"))
        t = " / ".join(_f(r[c]["kbit"], 0) if r[c] else "-" for c in ("B2", "O2"))
        lines.append(f"| {r['scenario']} | {r['flow']} | {j} | {l} | {t} |")
    lines += ["", "## Link estimation (S2)", "",
              "The line drops from 100 to 20 Mbit/s at 100 s. Detection: from the drop to the first cut. Under the line: "
              "from the drop to the first second the shaper is at or below the new capacity. Error: mean absolute "
              "difference from the true capacity from 10 s after the drop, as a share of it.", "",
              "| | Runs | Detection, s | Under the line after, s | Steady-state error | Time above the line |",
              "| --- | --- | --- | --- | --- | --- |"]
    for c, name in (("O2", "AQoSE"), ("B3", "cake-autorate")):
        if c in est and est[c]["runs"]:
            e = est[c]
            lines.append(f"| {name} | {e['runs']} | {_f(e['detect_s'],1)} | {_f(e['under_s'],1)} | {_f(e['abs_error_pct'],1)}% | {_f(e['time_above_pct'],0)}% |")
    lines += ["", "Plain CAKE stays at its fixed 95 Mbit/s, 375% above the line after the drop.", ""]
    if dash:
        lines += ["## Dashboard", "", f"`{Path(dash).name}`: one run of S5, plain CAKE against AQoSE.", ""]
    (out / "evidence.md").write_text("\n".join(lines) + "\n")
    (out / "evidence.json").write_text(json.dumps({"bulk": bulk, "bulk_fresh": fbulk, "fairness": fair,
                                                   "fairness_fresh": ffair, "interactive": inter, "estimator": est},
                                                  indent=1, default=str))
    print(out / "evidence.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

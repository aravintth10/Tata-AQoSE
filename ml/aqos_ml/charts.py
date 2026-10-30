"""Charts for the case study, from the same numbers as the report's tables.

  python -m aqos_ml.charts --results results-core --out reports/charts

Writes PNG files. Every chart has one job and one axis; values are written
on the marks, so nothing has to be read off a colour or a gridline.

  total-cost.png        the headline: damage summed over all scenarios, per configuration
  cost-by-scenario.png  the same, scenario by scenario
  late-frames.png       what a person on the call or in the game saw
  against-cake.png      the full system against each rival, seed by seed
  timeline-<S>.png      one run second by second: plain CAKE and the full system
  offline.png           the classifier against the rules on recorded flows
  damage-table.png      the measured damage table
"""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import yaml  # noqa: E402

from . import damage, report  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
# One colour per configuration, the same in every chart. No QoS is grey:
# it is the backdrop, not a rival.
COLOR = {"B0": "#898781", "B2": "#2a78d6", "B4": "#eb6834", "O1": "#1baf7a", "O2": "#6250d6",
         # Last in every order: green beside orange fails for colour-blind readers.
         "B3": "#008300"}
NAME = {"B0": "No QoS", "B2": "Plain CAKE", "B4": "CAKE + threshold rules",
        "O1": "Classifier, top label", "O2": "Classifier, expected damage", "B3": "CAKE + cake-autorate"}
SHORT = {"B0": "No QoS", "B2": "CAKE", "B4": "Rules", "O1": "Top label", "O2": "Full system", "B3": "Autorate"}
SURFACE, INK, SECONDARY, MUTED, AXIS, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#c3c2b7", "#ebeae5"
BLUES = ["#fcfcfb", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#104281"]

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "DejaVu Sans", "font.size": 10, "text.color": INK,
    "axes.edgecolor": AXIS, "axes.labelcolor": SECONDARY, "xtick.color": MUTED, "ytick.color": MUTED,
    "xtick.labelcolor": SECONDARY, "ytick.labelcolor": SECONDARY,
    "axes.spines.top": False, "axes.spines.right": False, "axes.spines.left": False,
    "axes.grid": False, "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "legend.frameon": False,
})


def _title(fig, title, subtitle):
    fig.text(0.02, 0.985, title, fontsize=14, fontweight="bold", va="top", ha="left")
    fig.text(0.02, 0.985 - 0.36 / fig.get_figheight(), subtitle, fontsize=10, color=SECONDARY, va="top", ha="left")


def _save(fig, out, name):
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def _median(runs, scenario, config, measure):
    values = [m[measure] for m in runs[scenario].get(config, {}).values() if measure in m]
    return values


def _bars(ax, labels, values, colors, lows=None, highs=None, digits=2, limit=None):
    """Thin horizontal bars from a zero baseline, each with its value."""
    y = np.arange(len(labels))[::-1]
    highs = [value if high is None else high for value, high in zip(values, highs or values)]
    top = limit or max([*values, *highs] + [1e-9])
    ax.barh(y, values, height=0.56, color=colors, edgecolor=SURFACE, linewidth=1.5, zorder=3)
    if lows is not None:
        for yi, low, high in zip(y, lows, highs):
            if low is not None:
                ax.plot([low, high], [yi, yi], color=INK, linewidth=1.2, zorder=4, solid_capstyle="butt")
    for yi, value, high in zip(y, values, highs):
        ax.text(max(value, high) + top * 0.025, yi, f"{value:,.{digits}f}", va="center", ha="left",
                fontsize=9.5, color=INK)
    ax.set_yticks(y, labels)
    ax.set_xlim(0, top * 1.2)
    ax.set_ylim(-0.6, len(labels) - 0.4)
    ax.tick_params(axis="y", length=0)
    ax.tick_params(axis="x", length=0, labelsize=8.5)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.axvline(0, color=AXIS, linewidth=1)


def total_cost(runs, configs, order, out, rng):
    """Damage per seed summed over the scenarios, then the median over seeds.
    Only configurations run on every scenario: a total over fewer would
    not compare."""
    configs = [c for c in configs if all(c in runs[s] for s in order)]
    seeds = sorted(set.intersection(*[set(runs[s][c]) for s in order for c in configs if c in runs[s]]))
    values, lows, highs = [], [], []
    for c in configs:
        per_seed = [sum(runs[s][c][seed][report.COST] for s in order if c in runs[s]) for seed in seeds]
        median, low, high = report.interval(per_seed, rng)
        values.append(median), lows.append(low), highs.append(high)
    fig, ax = plt.subplots(figsize=(8, 3.6))
    fig.subplots_adjust(left=0.27, right=0.96, top=0.74, bottom=0.14)
    _bars(ax, [NAME[c] for c in configs], values, [COLOR[c] for c in configs], lows, highs)
    ax.set_xlabel("flows left unusable (summed cost, lower is better)")
    seeds_text = f"median of {len(seeds)} seeds, 95% interval" if len(seeds) > 1 else "one seed"
    _title(fig, f"Damage over all {len(order)} scenarios",
           f"Each flow costs 0 when it is fine and 1 when it is unusable; {seeds_text}")
    return _save(fig, out, "total-cost.png")


def cost_by_scenario(runs, names, configs, order, out, rng):
    cols = 3
    rows = -(-len(order) // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(11, 2.5 * rows + 1.0), squeeze=False)
    fig.subplots_adjust(left=0.09, right=0.98, top=1 - 1.0 / (2.5 * rows + 1.0), bottom=0.05, hspace=0.55, wspace=0.45)
    top = max(max(_median(runs, s, c, report.COST) or [0]) for s in order for c in configs)
    for ax, s in zip(axes.flat, order):
        present = [c for c in configs if c in runs[s]]
        stats = [report.interval(_median(runs, s, c, report.COST), rng) for c in present]
        _bars(ax, [SHORT[c] for c in present], [m for m, _, _ in stats], [COLOR[c] for c in present],
              [low for _, low, _ in stats], [high for _, _, high in stats], limit=top)
        name = names[s] if len(names[s]) <= 36 else names[s][:34].rstrip() + "…"
        ax.set_title(f"{s}  {name}", fontsize=9)
        ax.tick_params(axis="y", labelsize=8.5)
    for ax in axes.flat[len(order):]:
        ax.set_visible(False)
    _title(fig, "Damage by scenario", "Summed cost of the scenario's flows, lower is better; the same scale in every panel")
    return _save(fig, out, "cost-by-scenario.png")


def late_frames(runs, names, configs, order, out, rng):
    """The share of video or game frames that arrived too late to show."""
    panels = []
    for s in order:
        measures = sorted({m for c in runs[s].values() for run in c.values() for m in run if m.endswith("late frames %")})
        for measure in measures:
            worst = max(max(_median(runs, s, c, measure) or [0]) for c in configs if c != "B0")
            if worst >= 5:  # panels where every QoS method is fine say nothing
                panels.append((s, measure))
    if not panels:
        return None
    cols = min(3, len(panels))
    rows = -(-len(panels) // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(3.7 * cols, 2.5 * rows + 1.0), squeeze=False)
    fig.subplots_adjust(left=0.27 / cols + 0.02, right=0.98, top=1 - 1.0 / (2.5 * rows + 1.0), bottom=0.07,
                        hspace=0.55, wspace=0.5)
    for ax, (s, measure) in zip(axes.flat, panels):
        present = [c for c in configs if c in runs[s]]
        stats = [report.interval(_median(runs, s, c, measure), rng) for c in present]
        _bars(ax, [SHORT[c] for c in present], [m for m, _, _ in stats], [COLOR[c] for c in present],
              [low for _, low, _ in stats], [high for _, _, high in stats], digits=1, limit=100)
        ax.set_xlim(0, 122)
        ax.set_xticks([0, 50, 100])
        who = measure.split(":")[0]
        ax.set_title(f"{s}  the {who}", fontsize=9.5)
        ax.tick_params(axis="y", labelsize=8.5)
    for ax in axes.flat[len(panels):]:
        ax.set_visible(False)
    _title(fig, "Frames that arrived too late, %",
           "Calls and games in the scenarios where some method fails them; lower is better")
    return _save(fig, out, "late-frames.png")


def against(runs, configs, order, out, rng, bands):
    """The full system minus each rival, paired by seed: left of zero is better."""
    rivals = [c for c in ("B2", "B4", "O1", "B3") if c in configs]
    if "O2" not in configs or not rivals:
        return None
    fig, axes = plt.subplots(1, len(rivals), figsize=(3.9 * len(rivals), 0.42 * len(order) + 1.9), sharey=True,
                             squeeze=False)
    fig.subplots_adjust(left=0.07, right=0.98, top=1 - 1.0 / (0.42 * len(order) + 1.9), bottom=0.16, wspace=0.12)
    y = np.arange(len(order))[::-1]
    compared = {}
    for rival in rivals:
        for s in order:
            if "O2" in runs[s] and rival in runs[s]:
                a = {seed: m[report.COST] for seed, m in runs[s]["O2"].items()}
                b = {seed: m[report.COST] for seed, m in runs[s][rival].items()}
                compared[rival, s] = report.compare(a, b, rng, bands.get(s, {}).get(report.COST, 0.0))
    reach = max([abs(v) for c in compared.values() if c for v in (c["median"], c["low"], c["high"]) if v is not None]
                + [1.0]) + 0.45
    for ax, rival in zip(axes.flat, rivals):
        for yi, s in zip(y, order):
            c = compared.get((rival, s))
            if c is None:
                continue
            if c["low"] is not None:
                ax.plot([c["low"], c["high"]], [yi, yi], color=COLOR["O2"], linewidth=2, zorder=3, solid_capstyle="round")
            ax.plot(c["median"], yi, "o", markersize=7.5, color=COLOR["O2"], markeredgecolor=SURFACE,
                    markeredgewidth=1.5, zorder=4)
            side = "left" if c["median"] > -0.3 * reach else "right"
            edge = c["median"] if c["low"] is None else (c["high"] if side == "left" else c["low"])
            ax.text(edge + (0.06 if side == "left" else -0.06) * reach, yi + 0.02, f"{c['median']:+.2f}", va="center",
                    ha=side, fontsize=8.5, color=INK)
        ax.axvline(0, color=SECONDARY, linewidth=1, zorder=2)
        ax.set_xlim(-reach, reach)
        ax.set_ylim(-0.6, len(order) - 0.4)
        ax.xaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
        ax.tick_params(axis="both", length=0, labelsize=8.5)
        ax.set_yticks(y, order)
        ax.set_title(f"against {NAME[rival]}", fontsize=10)
        ax.set_xlabel("← full system better     worse →", fontsize=8.5)
    _title(fig, "The full system against each rival, same traffic",
           "Difference in summed cost, paired by seed: median and 95% interval. Zero is a tie")
    return _save(fig, out, "against-cake.png")


def _per_second(run_dir, traffic_id):
    """{second: share of the flow's frames that were late in that second,
    in percent}, for the direction that suffered more."""
    params = json.loads((Path(run_dir) / "gen" / f"{traffic_id}.client.json").read_text())
    by_direction = {}
    with damage._open(run_dir) as f:
        for line in f:
            if f'"{traffic_id}"' not in line or '"frame_miss"' not in line:
                continue
            try:
                s = json.loads(line)
            except ValueError:
                continue
            if s.get("traffic_id") == traffic_id and s["metric"] == "frame_miss":
                by_direction.setdefault(s["direction"], {})[int(s["t_s"])] = s["value"]
    if not by_direction:
        return {}
    direction = max(by_direction, key=lambda d: sum(by_direction[d].values()))
    rate = damage.frame_rate(params["profile"], params["params"], direction) or 1
    points = sorted(by_direction[direction].items())[:-1]  # the last second is cut short
    return {t: min(100.0 * v / rate, 100.0) for t, v in points}


def _event(event):
    """An event in a few plain words."""
    args = event.get("args") or {}
    if event["type"] == "traffic_param":
        said = {("camera", "off"): "camera off", ("camera", "on"): "camera on", ("phase", "match"): "match begins",
                ("phase", "lobby"): "back to lobby", ("paused", True): "paused", ("paused", False): "resumed"}
        return f"{args.get('traffic')}: {said.get((args.get('param'), args.get('value')), args.get('value'))}"
    if event["type"] == "link_rate":
        return f"link {args.get('direction')} to {int(args.get('rate_kbit', 0)) // 1000} Mbit/s"
    if event["type"] == "intent_lease":
        return f"priority asked for {args.get('host')}"
    return event["type"].replace("_", " ")


def timeline(results, scenario, traffic_id, out, configs=("B2", "O2")):
    """One run, second by second: what the call or the game went through."""
    results = Path(results)
    seeds = sorted(int(p.name.rsplit("-s", 1)[1]) for p in results.glob(f"{scenario}-{configs[0]}-s????")
                   if (p / "manifest.json").exists())
    if not seeds:
        return None
    seed = seeds[0]
    series = {}
    for config in configs:
        run_dir = results / f"{scenario}-{config}-s{seed:04d}"
        if (run_dir / "manifest.json").exists():
            series[config] = _per_second(run_dir, traffic_id)
    if len(series) < 2 or not all(series.values()):
        return None
    doc = yaml.safe_load((results / f"{scenario}-{configs[0]}-s{seed:04d}" / "scenario.yaml").read_text())
    fig, ax = plt.subplots(figsize=(9, 3.9))
    fig.subplots_adjust(left=0.08, right=0.97, top=0.62, bottom=0.14)
    for config, points in series.items():
        ax.plot(list(points), list(points.values()), color=COLOR[config], linewidth=2, zorder=3, label=NAME[config])
    # When each other flow starts, and each event: the cause of what the lines do.
    marks = [(e["start_s"], f"{e['id']} starts") for e in doc["traffic"] if e["start_s"] > 0 and e["id"] != traffic_id]
    marks += [(e["at_s"], _event(e)) for e in doc.get("events") or []]
    for i, (at, text) in enumerate(sorted(marks)):
        ax.axvline(at, color=AXIS, linewidth=1, linestyle=(0, (3, 3)), zorder=1)
        ax.text(at + 1, 124 - 8 * (i % 3), text, fontsize=8, color=SECONDARY, va="top", clip_on=False)
        ax.plot([at, at], [100, 124 - 8 * (i % 3)], color=AXIS, linewidth=1, linestyle=(0, (3, 3)), clip_on=False)
    ax.set_ylim(0, 100)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_xlim(0, doc["warmup_s"] + doc["duration_s"])
    ax.yaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(axis="both", length=0, labelsize=8.5)
    ax.set_xlabel("seconds into the run")
    ax.set_ylabel("frames late, %")
    ax.legend(loc="lower right", bbox_to_anchor=(1.0, 1.3), ncols=2, fontsize=9, handlelength=1.6, borderaxespad=0)
    _title(fig, f"{scenario}: the {traffic_id}'s frames arriving too late, second by second",
           f"{doc['name']}. Same traffic under both, seed {seed}")
    return _save(fig, out, f"timeline-{scenario}.png")


def offline(out, path=REPO / "reports" / "head-to-head-v1-validation.json"):
    if not path.exists():
        return None
    doc = json.loads(path.read_text())
    picked = []
    for row in doc["results"]:
        name = row["contestant"]
        if "Random forest" in name:
            continue  # the model that was not chosen; it is in the report's table
        color = COLOR["B4"] if name.startswith("Rules") else COLOR["O1"] if "top label" in name else COLOR["O2"]
        picked.append((name.replace(" (O1)", "").replace(" (O2)", ""), float(row["expected_damage_balanced"]), color))
    fig, ax = plt.subplots(figsize=(8, 3.3))
    fig.subplots_adjust(left=0.38, right=0.96, top=0.72, bottom=0.16)
    _bars(ax, [n for n, _, _ in picked], [v for _, v, _ in picked], [c for _, _, c in picked])
    ax.set_xlabel("balanced expected damage (lower is better)")
    _title(fig, "On recorded flows: the classifier against the rules",
           f"Validation split of dataset v1, {doc['flows']:,} flows none of them trained on; every class counts the same")
    return _save(fig, out, "offline.png")


def damage_table(out, path=REPO / "configs" / "damage_table.yaml"):
    from matplotlib.colors import LinearSegmentedColormap
    table = yaml.safe_load(path.read_text())["damage"]
    tins = ["voice", "video", "best_effort", "bulk"]
    labels = {"telephony": "Voice call", "realtime_interactive": "Game, cloud game",
              "multimedia_conferencing": "Video call", "multimedia_streaming": "Streaming", "standard": "Web",
              "high_throughput_data": "Download", "low_priority_data": "Background transfer"}
    grid = np.array([[table[c][t] for t in tins] for c in labels])
    fig, ax = plt.subplots(figsize=(6.6, 4.6))
    fig.subplots_adjust(left=0.3, right=0.97, top=0.74, bottom=0.04)
    ax.imshow(grid, cmap=LinearSegmentedColormap.from_list("blues", BLUES), vmin=0, vmax=10, aspect="auto")
    for i in range(grid.shape[0]):
        for j in range(grid.shape[1]):
            ax.text(j, i, f"{grid[i, j]:g}", ha="center", va="center", fontsize=10.5,
                    color="#ffffff" if grid[i, j] >= 5 else INK)
    ax.set_xticks(range(len(tins)), ["Voice", "Video", "Best Effort", "Bulk"])
    ax.set_yticks(range(len(labels)), list(labels.values()))
    ax.xaxis.tick_top()
    ax.tick_params(axis="both", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xticks(np.arange(-0.5, len(tins)), minor=True)
    ax.set_yticks(np.arange(-0.5, len(labels)), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=2)
    ax.tick_params(which="minor", length=0)
    _title(fig, "What a wrong tin costs",
           "Measured damage of putting each kind of flow in each tin; 0 is its best tin, 10 the worst cell")
    return _save(fig, out, "damage-table.png")


# The flow each scenario is about, for the timelines.
SUBJECTS = {"S2": "call", "S5": "call", "S6": "call", "S7": "game", "S8": "call"}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.charts", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", required=True)
    parser.add_argument("--aa")
    parser.add_argument("--out", default=str(REPO / "reports" / "charts"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    scoring = yaml.safe_load(Path(damage.SCORING).read_text())
    rng = np.random.default_rng(1)
    runs, names = report.reduce(args.results, scoring)
    bands = report.noise_bands(report.reduce(args.aa, scoring)[0], runs) if args.aa else {}
    order = sorted(runs, key=lambda s: int(s[1:]))
    configs = [c for c in ("B0", "B2", "B4", "O1", "O2", "B3") if any(c in runs[s] for s in order)]
    written = [
        total_cost(runs, configs, order, out, rng),
        cost_by_scenario(runs, names, configs, order, out, rng),
        late_frames(runs, names, configs, order, out, rng),
        against(runs, configs, order, out, rng, bands),
        *[timeline(args.results, s, tid, out) for s, tid in SUBJECTS.items() if s in runs],
        offline(out),
        damage_table(out),
    ]
    for path in written:
        if path:
            print(path.relative_to(REPO) if path.is_relative_to(REPO) else path)


if __name__ == "__main__":
    main()

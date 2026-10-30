"""Measures the damage table: how bad it is to put a flow of each class in
each tin, instead of guessing.

  python -m aqos_ml.damage scenarios          write scenarios/damage/*.yaml
  python -m aqos_ml.damage run --seeds 1-3    perform the runs (resumable)
  python -m aqos_ml.damage score              write the table from finished runs

One run is one cell and one seed: a flow of one profile (the subject) is
forced into one tin while a fixed background runs on other hosts, each in
the tin of its own class. Two things are measured: what happens to the
subject, and what happens to the background. A download put in Voice is
barely hurt itself; the harm is to everyone else.

Measuring and scoring are separate. `run` reduces each run to raw numbers
(cell.json); `score` turns those into costs with the thresholds in
configs/damage_scoring.yaml. Changing a threshold needs `score` only.
"""

import argparse
import csv
import gzip
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml

from .run import runner

REPO = runner.REPO
TAXONOMY = runner.TAXONOMY
SCENARIOS = REPO / "scenarios" / "damage"
RESULTS = REPO / "results-damage"
SCORING = REPO / "configs" / "damage_scoring.yaml"
TABLE = REPO / "configs" / "damage_table.measured.yaml"
MEASUREMENTS = REPO / "configs" / "damage_measurements.csv"

TINS = ["bulk", "best_effort", "video", "voice"]
CONFIG = {"bulk": "D1", "best_effort": "D2", "video": "D3", "voice": "D4"}
# Every profile with a class of its own to answer for. A class with two
# profiles gets the mean of both. flow_storm is a stress test, not a kind
# of traffic a home wants placed.
SUBJECTS = ["voice_call", "gaming", "cloud_gaming", "video_call", "abr_video", "web_browsing",
            "bulk_download", "game_update", "backup_upload", "p2p"]
# The background: something in every tin a working system would fill, so
# that no tin is measured as an empty lane.
BACKGROUND = [("call", "video_call"), ("stream", "abr_video"), ("download", "bulk_download"), ("seeding", "p2p")]
# Shorter thinking than a person, so that a minute holds enough pages.
SUBJECT_PARAMS = {"web_browsing": {"think_s": 2}}

# The conditions a cell is measured under. CAKE shares a link fairly
# between devices and, on one device, between flows, so a flow alone on its
# device is protected whatever tin it is in. A tin starts to matter when
# the flow has company on its own device.
#
#   alone    the subject is the only traffic on its device
#   crowded  the same device is also fetching a game update over eight
#            connections, as a PC does while its owner plays or is on a
#            call. Fair shares between flows now leave the subject a ninth.
#   uplink   the same device is also on a video call, on a 25/5 Mbit/s
#            link, and nobody else is calling. The call needs a little
#            more upload than the Video tin's half of the link gives it
#            priority for, so whatever shares the upload with it decides
#            whether it survives. This is where demoting a background
#            transfer helps someone. Added after scenario S5's first run,
#            which showed the table had no such case.
#   uplink2  the first attempt at that: the same, with the usual second
#            call in the background. Two calls need more than the Video
#            tin's share between them and were broken whatever the subject
#            did, so it measured nothing. Not part of the table.
#   tight    alone, on a 40/8 Mbit/s link. Measured for one seed and not
#            part of the table: what it showed was a fixed-rate sender
#            overloading a link, not a tin (see the specification).
#
# The table is the mean of the conditions in COUNTED.
ROOMY = {"down_kbit": 100000, "up_kbit": 20000, "delay_ms": 10}
NARROW = {"down_kbit": 25000, "up_kbit": 5000, "delay_ms": 10}
CONDITIONS = {
    "alone": {"link": ROOMY, "company": None},
    "tight": {"link": {"down_kbit": 40000, "up_kbit": 8000, "delay_ms": 10}, "company": None},
    "crowded": {"link": ROOMY, "company": ("update", "game_update", {"connections": 8})},
    "uplink2": {"link": NARROW, "company": ("meeting", "video_call", {"kbit_up": 2500, "kbit_down": 2500})},
    "uplink": {"link": NARROW, "company": ("meeting", "video_call", {"kbit_up": 2500, "kbit_down": 2500}),
               "background": [b for b in BACKGROUND if b[0] != "call"]},
}
COUNTED = ("alone", "crowded", "uplink")
_FIRST_ID = {"alone": 2000, "tight": 2100, "crowded": 2200, "uplink2": 2300, "uplink": 2400}
_OLD_NAME = {"roomy": "alone"}  # what cells written before there were conditions call it

INTERACTIVE = {"voice_call", "video_call", "gaming", "cloud_gaming"}
DATA = {"bulk_download", "game_update", "backup_upload", "p2p"}


def condition_of(scenario_id):
    number = int(str(scenario_id).lstrip("S"))
    return next(name for name, first in sorted(_FIRST_ID.items(), key=lambda kv: -kv[1]) if number >= first)


def scenario(profile, condition="alone"):
    index = SUBJECTS.index(profile)
    setting = CONDITIONS[condition]
    background = setting.get("background", BACKGROUND)
    company = []
    if setting["company"]:
        tid, company_profile, params = setting["company"]
        company = [{"id": tid, "host": "subject", "profile": company_profile, "start_s": 0, "stop_s": None,
                    "params": dict(params)}]
    subject = {"id": runner.SUBJECT, "host": "subject", "profile": profile, "start_s": 0, "stop_s": None}
    if profile in SUBJECT_PARAMS:
        subject["params"] = SUBJECT_PARAMS[profile]
    return {
        "schema_version": 1,
        "id": f"S{_FIRST_ID[condition] + index}",
        "name": f"Damage table: {profile} against the standard background, {condition}",
        "description": "One subject flow, forced into a tin by the D configurations, with a call, a "
                       "stream, a download and a peer-to-peer transfer on other hosts.",
        "duration_s": 60,
        "warmup_s": 10,
        "link": dict(setting["link"]),
        "hosts": ["subject"] + [f"bg-{name}" for name, _ in background],
        "traffic": [subject] + company + [
            {"id": name, "host": f"bg-{name}", "profile": bg, "start_s": 0, "stop_s": None}
            for name, bg in background],
        "events": [],
        "primary_metrics": ["latency_ms", "throughput_kbit"],
    }


def write_scenarios():
    SCENARIOS.mkdir(parents=True, exist_ok=True)
    paths = {}
    for condition in CONDITIONS:
        for profile in SUBJECTS:
            doc = scenario(profile, condition)
            paths[profile, condition] = SCENARIOS / f"{doc['id']}.yaml"
            paths[profile, condition].write_text(
                "# Written by `python -m aqos_ml.damage scenarios`; edit ml/aqos_ml/damage.py instead.\n"
                + yaml.safe_dump(doc, sort_keys=False))
    return paths


def _open(run_dir):
    path = Path(run_dir) / "metrics.jsonl"
    return open(path) if path.exists() else gzip.open(str(path) + ".gz", "rt")


def frame_rate(profile, params, direction):
    """Frames per second a flow carries in one direction, or None when the
    profile has no frames with a deadline there."""
    if profile == "video_call":
        # A replayed call (params "trace") has its own rate each way.
        return params.get("fps_" + direction, params.get("fps"))
    if profile == "gaming":
        return params["tick_hz"]
    if profile == "cloud_gaming":
        return params["fps"] if direction == "down" else params["input_hz"]
    return None


def measure_entries(run_dir):
    """Reduces any run to raw numbers per traffic entry, over the measured
    time only. No thresholds are applied here."""
    run_dir = Path(run_dir)
    doc = yaml.safe_load((run_dir / "scenario.yaml").read_text())
    warmup, seconds = doc["warmup_s"], doc["duration_s"]
    values = defaultdict(list)
    with _open(run_dir) as f:
        for line in f:
            if '"generator"' not in line:
                continue
            try:
                s = json.loads(line)
            except ValueError:
                continue
            if s["t_s"] >= warmup:
                values[s["traffic_id"], s["metric"], s.get("direction")].append(s["value"])

    entries = {}
    for entry in doc["traffic"]:
        tid, profile = entry["id"], entry["profile"]
        params = json.loads((run_dir / "gen" / f"{tid}.client.json").read_text())["params"]
        out = {"profile": profile, "class": TAXONOMY["profiles"][profile]}
        get = lambda metric, direction=None: values.get((tid, metric, direction), [])  # noqa: E731
        if profile in INTERACTIVE:
            for d in ("up", "down"):
                latency = get("latency_ms", d)
                side = {
                    "latency_p95_ms": float(np.percentile(latency, 95)) if latency else None,
                    "jitter_ms": float(np.mean(get("jitter_ms", d))) if get("jitter_ms", d) else None,
                    "loss_ratio": float(np.mean(get("loss_ratio", d))) if get("loss_ratio", d) else None,
                }
                rate = frame_rate(profile, params, d)
                if rate and get("frame_miss", d):
                    side["frame_miss_ratio"] = min(float(np.sum(get("frame_miss", d))) / (rate * seconds), 1.0)
                out[d] = side
        elif profile == "abr_video":
            out["stall_fraction"] = float(np.sum(get("video_stall_ms"))) / (seconds * 1000)
            bitrate = get("video_bitrate_kbit")
            out["bitrate_kbit"] = float(np.mean(bitrate)) if bitrate else 0.0
            out["bitrate_top_kbit"] = float(params["max_kbit"])
        elif profile == "web_browsing":
            loads = get("page_load_ms")
            out["pages"] = len(loads)
            out["page_load_median_ms"] = float(np.median(loads)) if loads else None
            out["page_load_p90_ms"] = float(np.percentile(loads, 90)) if loads else None
        if profile in DATA or profile in ("abr_video", "web_browsing"):
            out["throughput_kbit"] = sum(float(np.sum(get("throughput_kbit", d))) for d in ("up", "down")) / seconds
        entries[tid] = out
    return entries


def measure(run_dir):
    """A damage cell: the run's entries, and which cell it is."""
    run_dir = Path(run_dir)
    entries = measure_entries(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    tins = (run_dir / "tins.json").read_text() if (run_dir / "tins.json").exists() else "null"
    return {"run_id": manifest["run_id"], "seed": manifest["seed"], "config": manifest["config"],
            "condition": condition_of(manifest["scenario_id"]),
            "subject_profile": entries[runner.SUBJECT]["profile"],
            "tin": runner.CONFIGS[manifest["config"]]["force"],
            "tins_sent": json.loads(tins or "null"), "entries": entries}


def finish(run_dir):
    """Writes cell.json and compresses the per-packet samples, which are
    most of a run's size. Nothing is discarded."""
    run_dir = Path(run_dir)
    if not (run_dir / "cell.json").exists():
        (run_dir / "cell.json").write_text(json.dumps(measure(run_dir), indent=1) + "\n")
    raw = run_dir / "metrics.jsonl"
    if raw.exists():
        with open(raw, "rb") as src, gzip.open(str(raw) + ".gz.tmp", "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        Path(str(raw) + ".gz.tmp").rename(str(raw) + ".gz")
        raw.unlink()


def run(seeds, results=RESULTS, profiles=SUBJECTS, conditions=COUNTED, report=print):
    """Performs every cell for every seed that has no valid result yet.
    Seed by seed, so that stopping early leaves whole tables."""
    paths = write_scenarios()
    todo = [(seed, condition, profile, tin)
            for seed in seeds for condition in conditions for profile in profiles for tin in TINS]
    done = 0
    for i, (seed, condition, profile, tin) in enumerate(todo, 1):
        report(f"[{i}/{len(todo)}] {profile} in {tin}, {condition}, seed {seed}:", end=" ", flush=True)
        manifest = runner.run_until_valid(paths[profile, condition], CONFIG[tin], seed, "damage", results, report)
        if manifest:
            finish(Path(results) / manifest["run_id"])
            done += 1
    report(f"{done} of {len(todo)} cells measured")
    return done == len(todo)


# ---- scoring ---------------------------------------------------------------

def ramp(value, bounds):
    """0 at or below bounds[0], 1 at or above bounds[1], linear between. A
    missing value is the worst: nothing got through to be measured."""
    if value is None:
        return 1.0
    fine, ruined = bounds
    return float(np.clip((value - fine) / (ruined - fine), 0.0, 1.0))


def cost(entry, scoring, baseline=None):
    """One flow's experience as a cost from 0 (as good as it gets) to 1
    (unusable). baseline is the same flow, same seed, in the run where the
    subject sat in Best Effort: what the flow gets when nothing is
    misplaced. Data transfers and web pages are judged against it."""
    profile = entry["profile"]
    if profile in INTERACTIVE:
        bounds = scoring["interactive"][profile]
        # The worst aspect in the worse direction: a call with perfect
        # sound one way is still a bad call.
        return max(ramp(entry[d].get(name), b) for d in ("up", "down") for name, b in bounds.items()
                   if name in entry[d] or name != "frame_miss_ratio")
    if profile == "abr_video":
        s = scoring["streaming"]
        drop = 1 - entry["bitrate_kbit"] / entry["bitrate_top_kbit"]
        return max(ramp(entry["stall_fraction"], s["stall_fraction"]),
                   s["bitrate_weight"] * ramp(drop, s["bitrate_drop"]))
    if profile == "web_browsing":
        load = entry["page_load_median_ms"]
        absolute = ramp(load, scoring["web"]["page_load_median_ms"])
        usual = baseline and baseline.get("page_load_median_ms")
        if load is None or not usual:
            return absolute
        # Also by how many times slower than usual: the generated pages are
        # lighter than real ones, so the same slowdown on a real page would
        # cross the absolute thresholds long before these do.
        return max(absolute, ramp(load / usual, scoring["web"]["slowdown"]))
    # Data: the share of its usual throughput the flow lost. Going faster
    # than usual earns nothing; this is a cost, not a score.
    usual = baseline and baseline.get("throughput_kbit")
    if not usual:
        return 0.0
    lost = max(0.0, 1 - entry["throughput_kbit"] / usual)
    return ramp(lost, scoring["data"][entry["class"]])


def load_cells(results=RESULTS):
    """{(subject profile, condition, seed): {tin: cell}} from every valid run."""
    cells = defaultdict(dict)
    for path in sorted(Path(results).glob("*/cell.json")):
        if path.parent.is_symlink():
            continue
        manifest = json.loads((path.parent / "manifest.json").read_text())
        if manifest["valid"]:
            cell = json.loads(path.read_text())
            # Cells written before there were conditions were all "alone",
            # and for a while the conditions were called links.
            old = cell.get("link", "roomy")
            condition = cell.get("condition") or _OLD_NAME.get(old, old)
            cells[cell["subject_profile"], condition, cell["seed"]][cell["tin"]] = cell
    return cells


def score(cells, scoring, unworkable=None, conditions=COUNTED):
    """Returns (table, rows, tables by condition). table is {class: {tin:
    damage 0 to 10}}, the mean over `conditions`; rows are the per-profile
    numbers behind it, for every condition measured. Only (profile,
    condition, seed) triples with all four tins count: a cell is a
    comparison between tins. Triples left out because the flow worked in no
    tin are appended to unworkable, if a list is given."""
    unworkable = [] if unworkable is None else unworkable
    measured = defaultdict(lambda: {tin: {"self": [], "others": []} for tin in TINS})
    for (profile, link, seed), by_tin in sorted(cells.items()):
        if set(by_tin) != set(TINS):
            continue
        usual = by_tin["best_effort"]["entries"]
        costs = {tin: {tid: cost(entry, scoring, usual.get(tid)) for tid, entry in cell["entries"].items()}
                 for tin, cell in by_tin.items()}
        if min(costs[tin][runner.SUBJECT] for tin in TINS) >= scoring["unworkable"]:
            # Broken in every tin: this link cannot carry this flow at all
            # (a fixed-rate cloud game that needs more than its share,
            # typically). Where to put it is then no question, and what it
            # does to the others is the flood of a sender that will not
            # slow down, which a real application would.
            unworkable.append((profile, link, seed))
            continue
        for tin in TINS:
            measured[profile, link][tin]["self"].append(costs[tin].pop(runner.SUBJECT))
            # Summed, not averaged: the damage is to the whole home.
            measured[profile, link][tin]["others"].append(sum(costs[tin].values()))

    rows, raw_by = [], {}
    for (profile, link), by_tin in measured.items():
        own = {tin: float(np.mean(by_tin[tin]["self"])) for tin in TINS}
        others = {tin: float(np.mean(by_tin[tin]["others"])) for tin in TINS}
        raw = raw_by[profile, link] = {}
        for tin in TINS:
            self_damage = own[tin] - min(own.values())
            harm = others[tin] - others["best_effort"]
            # Relief to the others is credited only to a flow that gives
            # up next to nothing for it. A backup in Bulk loses nothing and
            # saves the call beside it: that is what Bulk is for. A call in
            # Bulk is ruined, and that it frees room for a second call on
            # the same device does not make Bulk the place for calls: the
            # rule would be applied to both, and ruin both.
            counted = harm if harm > 0 or self_damage <= scoring.get("relief_free", 1.0) else 0.0
            raw[tin] = self_damage + scoring["harm_weight"] * counted
            rows.append({
                "profile": profile, "class": TAXONOMY["profiles"][profile], "condition": link, "tin": tin,
                "seeds": len(by_tin[tin]["self"]),
                "own_cost": round(own[tin], 4), "own_cost_sd": round(float(np.std(by_tin[tin]["self"])), 4),
                "others_cost": round(others[tin], 4),
                "others_cost_sd": round(float(np.std(by_tin[tin]["others"])), 4),
                "self_damage": round(self_damage, 4), "harm": round(harm, 4), "raw": round(raw[tin], 4),
            })

    def table_from(raws):
        """raws: {profile: {tin: raw damage}}. A class is the mean of its
        profiles; its best tin is its zero (only differences between tins
        of one class affect a decision, so this changes none)."""
        by_class = defaultdict(list)
        for profile, raw in raws.items():
            by_class[TAXONOMY["profiles"][profile]].append(raw)
        out = {}
        for cls in TAXONOMY["classes"]:
            if cls in by_class:
                mean = {tin: float(np.mean([raw[tin] for raw in by_class[cls]])) for tin in TINS}
                out[cls] = {tin: mean[tin] - min(mean.values()) for tin in TINS}
        return out

    links = sorted({link for _, link in raw_by})
    unscaled = {link: table_from({p: raw for (p, l), raw in raw_by.items() if l == link}) for link in links}
    # The table proper: each profile's mean over the conditions that count.
    counted = [link for link in links if link in conditions]
    profiles = sorted({p for p, l in raw_by if l in counted})
    overall = table_from({p: {tin: float(np.mean([raw_by[p, l][tin] for l in counted if (p, l) in raw_by]))
                              for tin in TINS} for p in profiles})
    # One scale for all of them, so that the tables can be read against
    # each other: the largest cell of the table proper, or of a condition
    # in it, is 10. The resolution is what a few seeds can tell apart;
    # anything finer would let noise choose between tins.
    top = max((v for t in [overall, *(unscaled[l] for l in counted)] for row in t.values() for v in row.values()),
              default=0.0)
    step = scoring.get("resolution", 0.01)

    def scale(t):
        return {cls: {tin: round(round(10 * v / top / step) * step, 2) if top > 0 else 0.0 for tin, v in row.items()}
                for cls, row in t.items()}

    return scale(overall), rows, {link: scale(t) for link, t in unscaled.items()}


def write_table(results=RESULTS, table_path=TABLE, rows_path=MEASUREMENTS, scoring_path=SCORING):
    scoring = yaml.safe_load(Path(scoring_path).read_text())
    cells = load_cells(results)
    unworkable = []
    table, rows, by_link = score(cells, scoring, unworkable)
    missing = sorted(set(TAXONOMY["classes"]) - set(table))
    if missing:
        raise SystemExit(f"no complete measurement yet for: {', '.join(missing)}")
    seeds = sorted({row["seeds"] for row in rows})
    order = ["voice", "video", "best_effort", "bulk"]
    lines = [
        "# Damage of placing a flow of a true class (row) in a tin (column), 0 to 10.",
        "#",
        "# MEASURED on the testbed by `python -m aqos_ml.damage` (chunk 5B): the flow's own",
        "# loss of quality plus the harm to a call, a stream, a download and a peer-to-peer",
        f"# transfer running beside it. `damage` is the mean over the conditions {' and '.join(COUNTED)}",
        "# and is what the decision engine reads; `by_condition` shows each on the same scale.",
        f"# The numbers behind each cell are in {Path(rows_path).name}; the thresholds are",
        f"# in {Path(scoring_path).name}.",
        "status: measured",
        f"seeds_per_cell: {seeds[0] if len(seeds) == 1 else seeds}",
        f"resolution: {scoring.get('resolution', 0.01)}",
        "# Left out, because the flow worked in no tin on that link:",
        f"unworkable: [{', '.join(f'{p} on {l} seed {s}' for p, l, s in unworkable)}]",
    ]

    def block(title, indent, t):
        lines.append(" " * (indent - 2) + title + ":")
        for cls in TAXONOMY["classes"]:
            if cls in t:
                cells_text = ", ".join(f"{tin}: {t[cls][tin]:g}" for tin in order)
                lines.append(" " * indent + f"{cls + ':':25}{{{cells_text}}}")

    block("damage", 2, table)
    lines.append("by_condition:")
    for name, t in by_link.items():
        block(name, 4, t)
    Path(table_path).write_text("\n".join(lines) + "\n")
    with open(rows_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return table, rows, by_link


def _seeds(text):
    first, _, last = text.partition("-")
    return list(range(int(first), int(last or first) + 1))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.damage", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scenarios")
    p = sub.add_parser("run")
    p.add_argument("--seeds", default="1-3", help="one seed or a range such as 1-3")
    p.add_argument("--profiles", nargs="*", default=SUBJECTS)
    p.add_argument("--conditions", nargs="*", default=list(COUNTED), choices=list(CONDITIONS))
    p.add_argument("--results", default=RESULTS)
    p = sub.add_parser("score")
    p.add_argument("--results", default=RESULTS)
    p.add_argument("--out", default=TABLE)
    p.add_argument("--rows", default=MEASUREMENTS, help="where the per-profile numbers go")
    args = parser.parse_args(argv)
    try:
        if args.command == "scenarios":
            for path in write_scenarios().values():
                print(path.relative_to(REPO))
        elif args.command == "run":
            return 0 if run(_seeds(args.seeds), args.results, args.profiles, args.conditions) else 1
        else:
            table, _, by_link = write_table(args.results, args.out, args.rows)
            for title, t in [(f"mean of {' and '.join(COUNTED)}", table), *by_link.items()]:
                print(title)
                for cls, row in t.items():
                    print(f"  {cls:25}", "  ".join(f"{tin} {row[tin]:5.2f}" for tin in ["voice", "video", "best_effort", "bulk"]))
        return 0
    except runner.RunError as exc:
        print(f"aqos_ml.damage: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted: the run in progress left no manifest and will be run again", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

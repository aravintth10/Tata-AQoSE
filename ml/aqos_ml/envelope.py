"""The reclassifier's check, as the ML side defines it (chunk 7A).

A labelled flow is compared once a second with the normal range of its
class: envelopes.json, the 5th to 95th percentile of each window statistic
over that class's training flows. A flow that stays outside goes back to
the classifier. The controller does the check itself, in Go
(internal/reclass); this file is the definition it must agree with.

  python -m aqos_ml.envelope cases   # rewrite the test cases the Go check must pass
  python -m aqos_ml.envelope rates --dataset data/dataset/v1.jsonl.gz
        # how often flows that have not changed would be sent back, for k = 1 to 3

Settings are the starting values of the technical specification, 9.1.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from . import evaluate
from .dataset import build as dataset
from .features import WINDOW

REPO = Path(__file__).resolve().parents[2]
CASES = REPO / "internal" / "reclass" / "testdata" / "cases.json"
MODELS = REPO / "models"
DEFAULTS = {"k": 2, "windows": 3, "interactive": 2, "slow_every": 2, "backoff": 5, "backoff_max": 40}
INTERACTIVE_TINS = {"voice", "video"}


def outside(envelopes, cls, window):
    """(how many statistics are outside the class's range, the name of the
    one furthest outside in widths of its range). None for a class with no
    envelope."""
    ranges = envelopes.get(cls)
    if ranges is None or window is None:
        return None
    count, worst, far = 0, None, 0.0
    for name in WINDOW:
        low, high = ranges[name]
        beyond = max(low - window[name], window[name] - high)
        if beyond <= 0:
            continue
        count += 1
        distance = beyond / max(high - low, abs(high) * 0.05, 1e-9)
        if distance > far:
            worst, far = name, distance
    return count, worst


class Watcher:
    """One flow's state. window() returns the statistic named in the
    trigger's reason when the flow is to be sent back, else None. The
    routine recheck is random and is not part of this definition."""

    def __init__(self, envelopes, cls, **settings):
        self.envelopes, self.cls = envelopes, cls
        self.cfg = {**DEFAULTS, **settings}
        self.age = self.outside = self.cooldown = self.backoff = 0
        self.worst = (None, 0.0)

    def window(self, tin, window):
        cfg = self.cfg
        self.age += 1
        self.cooldown = max(self.cooldown - 1, 0)
        every, need = (1, cfg["interactive"]) if tin in INTERACTIVE_TINS else (cfg["slow_every"], cfg["windows"])
        if self.age % every:
            return None
        found = outside(self.envelopes, self.cls, window)
        if found is None:
            return None
        count, name = found
        if count < cfg["k"]:
            self.outside, self.backoff, self.worst = 0, 0, (None, 0.0)
            return None
        self.outside += 1
        low, high = self.envelopes[self.cls][name]
        far = max(low - window[name], window[name] - high) / max(high - low, abs(high) * 0.05, 1e-9)
        if far > self.worst[1]:
            self.worst = (name, far)
        if self.outside < need or self.cooldown:
            return None
        named = self.worst[0]
        self.backoff = min(max(2 * self.backoff, cfg["backoff"]), max(cfg["backoff_max"], cfg["backoff"]))
        self.cooldown, self.outside, self.worst = self.backoff, 0, (None, 0.0)
        return named


def _middle(ranges, **changed):
    window = {name: (low + high) / 2 for name, (low, high) in ranges.items()}
    for name, factor in changed.items():
        low, high = ranges[name]
        window[name] = high * factor if factor > 1 else low * factor
    return {"samples": 5, **window}


def cases(envelopes):
    """Sequences of windows with the answer the check must give for each.
    Built from the real envelopes, so they move when the model does."""
    call, download = envelopes["multimedia_conferencing"], envelopes["high_throughput_data"]
    inside = _middle(call)
    one = _middle(call, bytes_down_per_s=3)
    two = _middle(call, bytes_down_per_s=3, pkts_down_per_s=3)
    gone = _middle(call, bytes_down_per_s=0.01, pkts_down_per_s=0.1, mean_pkt_size_down=0.5)
    quiet = _middle(download, bytes_down_per_s=0.001, pkts_down_per_s=0.001)
    spec = [
        ("a flow inside its range is never sent back", "multimedia_conferencing", "video", {}, [inside] * 12),
        ("one statistic outside is not enough", "multimedia_conferencing", "video", {}, [one] * 12),
        ("two outside for two windows sends a Video flow back, then backs off", "multimedia_conferencing", "video", {},
         [inside, two, two, two, two, two, two, two, two, two, two, two, two, two, two, two, two, two, two]),
        ("an outside window between inside ones is forgiven", "multimedia_conferencing", "video", {},
         [two, inside, two, inside, two, inside]),
        ("coming back inside clears the back-off", "multimedia_conferencing", "video", {},
         [two, two, inside, two, two, inside, two, two]),
        ("a Best Effort flow is checked every other second and needs three", "high_throughput_data", "best_effort", {},
         [quiet] * 10),
        ("the same flow in Video would have been sent back sooner", "high_throughput_data", "video", {}, [quiet] * 4),
        ("with the waiting switched off the first outside window is enough", "multimedia_conferencing", "video",
         {"windows": 1, "interactive": 1}, [inside, gone, inside]),
        ("k = 1 counts a single statistic", "multimedia_conferencing", "video", {"k": 1}, [one, one, one]),
        ("the reason names the statistic furthest outside", "multimedia_conferencing", "video", {}, [gone, gone]),
        ("a class without an envelope is never checked", "no_such_class", "video", {}, [gone] * 4),
    ]
    out = []
    for name, cls, tin, settings, windows in spec:
        watcher = Watcher(envelopes, cls, **settings)
        out.append({"name": name, "class": cls, "tin": tin, "settings": {**DEFAULTS, **settings},
                    "windows": windows, "expect": [watcher.window(tin, w) for w in windows]})
    return out


def chosen_envelopes(dataset_name="v2"):
    version = (MODELS / f"chosen-{dataset_name}.txt").read_text().strip()
    return version, json.loads((MODELS / version / "envelopes.json").read_text())


def write_cases(path=CASES):
    version, envelopes = chosen_envelopes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model_version": version, "envelopes": envelopes, "cases": cases(envelopes)},
                               indent=1) + "\n")
    return path


def rates(dataset_path, split="validation"):
    """Per class: the share of window records with at least k statistics
    outside the class's own range, and the share of flows that would be
    sent back at least once. These flows did not change, so every one sent
    back is a false trigger. The dataset has a window record every five
    seconds, not every second, so "in a row" here means records in a row."""
    _, envelopes = chosen_envelopes()
    flows = defaultdict(list)
    for row in dataset.load(dataset_path, splits={split}):
        if row["kind"] == "window" and row["label"] in envelopes:
            flows[(row["session"], row["flow_id"], row["label"])].append(row)
    table = {}
    for cls in evaluate.CLASSES:
        mine = [sorted(rows, key=lambda r: r["ts_ns"]) for key, rows in flows.items() if key[2] == cls]
        if not mine:
            continue
        counts = [[outside(envelopes, cls, r["window"])[0] for r in rows] for rows in mine]
        records = sum(len(c) for c in counts)
        row = {"flows": len(mine), "records": records}
        for k in (1, 2, 3):
            row[f"records_k{k}"] = sum(n >= k for c in counts for n in c) / records
            for need in (2, 3):
                hit = sum(any(all(n >= k for n in c[i:i + need]) for i in range(len(c) - need + 1)) for c in counts)
                row[f"flows_k{k}_n{need}"] = hit / len(mine)
        table[cls] = row
    return table


def rates_report(table, dataset_path, split):
    lines = [f"# Envelope check: false triggers on flows that did not change", "",
             f"Dataset `{Path(dataset_path).name}`, {split} split. Each flow is checked against the range of its own "
             "true class, so a flow sent back is a false trigger. Window records are five seconds apart here; "
             "live they are one second apart and overlap, so live figures will differ.", "",
             "| Class | Flows | Records | Records outside, k=1 | k=2 | k=3 | Flows sent back, k=2, 2 in a row | k=2, 3 in a row "
             "| k=3, 2 in a row |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for cls, r in table.items():
        lines.append(f"| {cls} | {r['flows']} | {r['records']} | {r['records_k1']:.0%} | {r['records_k2']:.0%} | "
                     f"{r['records_k3']:.0%} | {r['flows_k2_n2']:.0%} | {r['flows_k2_n3']:.0%} | {r['flows_k3_n2']:.0%} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.envelope", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cases")
    p = sub.add_parser("rates")
    p.add_argument("--dataset", required=True)
    p.add_argument("--split", default="validation")
    p.add_argument("--out", default=str(REPO / "reports" / "envelopes-v1.md"))
    args = parser.parse_args(argv)
    if args.command == "cases":
        print(write_cases())
    else:
        report = rates_report(rates(args.dataset, args.split), args.dataset, args.split)
        Path(args.out).write_text(report)
        print(report)


if __name__ == "__main__":
    main()

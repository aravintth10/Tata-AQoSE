"""A few numbers from one run, to read at a glance. The report (chunk 9A)
does the real statistics."""

import json
from collections import defaultdict
from pathlib import Path

import numpy as np


def summarise(out_dir):
    """Returns lines of text. Only samples after the warm-up count."""
    out_dir = Path(out_dir)
    manifest = json.loads((out_dir / "manifest.json").read_text())
    warmup = manifest["warmup_s"]
    values = defaultdict(list)
    with open(out_dir / "metrics.jsonl") as f:
        for line in f:
            try:
                s = json.loads(line)
            except ValueError:
                continue
            if s["t_s"] < warmup:
                continue
            who = s.get("traffic_id") or s.get("tin") or ""
            values[s["metric"], who, s.get("direction") or ""].append(s["value"])

    verdict = "valid" if manifest["valid"] else f"INVALID: {manifest['invalid_reason']}"
    lines = [f"{manifest['run_id']}: {verdict}"]
    for (metric, who, direction), v in sorted(values.items()):
        v = np.asarray(v)
        if metric == "latency_ms":
            p50, p95, p99 = np.percentile(v, [50, 95, 99])
            lines.append(f"  {who:10} {direction:5} latency ms   p50 {p50:7.1f}  p95 {p95:7.1f}  p99 {p99:7.1f}")
        elif metric == "throughput_kbit":
            lines.append(f"  {who:10} {direction:5} throughput   mean {v.mean() / 1000:6.1f} Mbit/s")
        elif metric == "frame_miss":
            lines.append(f"  {who:10} {direction:5} frame misses total {int(v.sum())}")
        elif metric == "queue_delay_ms" and v.max() > 0:
            lines.append(f"  {who:10} {direction:5} cake delay   mean {v.mean():6.2f} ms  max {v.max():6.2f} ms")
    return lines

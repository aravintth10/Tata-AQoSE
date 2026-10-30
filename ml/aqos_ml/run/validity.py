"""Was a finished run sound enough to count?"""

import json
from pathlib import Path

# A traffic entry may not go this many seconds without a sample, from its
# start to its stop.
WINDOW_S = 5
# Seconds at each end of an entry that are not checked: flows take a moment
# to start, and the last second may be cut short.
EDGE_S = 3
RATE_TOLERANCE = 0.01


def sample_seconds(metrics_path):
    """Returns {traffic_id: set of whole seconds that have a sample}."""
    seen = {}
    with open(metrics_path) as f:
        for line in f:
            # Cheap test first: most lines are per-packet latency samples.
            if '"traffic_id"' not in line:
                continue
            try:
                sample = json.loads(line)
            except ValueError:
                continue  # a line cut short by a killed process
            seen.setdefault(sample["traffic_id"], set()).add(int(sample["t_s"]))
    return seen


def coverage_problems(scenario, seen):
    """Every traffic entry must have produced samples through the whole of
    its measured time, not only at the start."""
    problems = []
    run_end = scenario["warmup_s"] + scenario["duration_s"]
    for entry in scenario["traffic"]:
        stop = entry.get("stop_s")
        stop = run_end if stop is None else min(stop, run_end)
        first = int(max(entry["start_s"], scenario["warmup_s"])) + EDGE_S
        last = int(stop) - EDGE_S
        seconds = seen.get(entry["id"], set())
        if not seconds:
            problems.append(f"{entry['id']}: no samples at all")
            continue
        # No stretch of WINDOW_S seconds without a sample, wherever it falls.
        previous = first - 1
        for second in sorted(s for s in seconds if first <= s < last) + [last]:
            if second - previous > WINDOW_S:
                problems.append(f"{entry['id']}: no samples between {previous + 1} s and {second} s")
                break
            previous = second
    return problems


def link_after_events(scenario):
    """The bottleneck rates the scenario leaves in place at the end."""
    rates = {"up": scenario["link"]["up_kbit"], "down": scenario["link"]["down_kbit"]}
    for event in sorted(scenario["events"], key=lambda e: e["at_s"]):
        if event["type"] == "link_rate":
            rates[event["args"]["direction"]] = int(event["args"]["rate_kbit"])
    return rates


def rate_problems(scenario, measured_kbit):
    """measured_kbit is what the bottleneck was actually set to at the end,
    read back from tc."""
    problems = []
    for direction, want in link_after_events(scenario).items():
        got = measured_kbit.get(direction)
        if got is None or abs(got - want) > want * RATE_TOLERANCE:
            problems.append(f"bottleneck {direction} is {got} kbit, the scenario says {want}")
    return problems


def check(out_dir, scenario, measured_kbit):
    """All the reasons the run is invalid. Empty means valid."""
    from ..gen import validate

    out_dir = Path(out_dir)
    problems = validate.check(out_dir, scenario)
    metrics = out_dir / "metrics.jsonl"
    if not metrics.exists():
        return problems + ["no metrics.jsonl"]
    problems += coverage_problems(scenario, sample_seconds(metrics))
    problems += rate_problems(scenario, measured_kbit)
    return problems

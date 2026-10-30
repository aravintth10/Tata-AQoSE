#!/usr/bin/env python3
"""Paired live controller CPU measurements; owns/replaces the isolated testbed.

Keeps the Rust classifier and scenario fixed while alternating runtime order.
Run on an otherwise idle host. Network QoE from these short runs is diagnostic,
not a statistical claim.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ml"))
from aqos_ml.run import runner  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    if args.trials < 1 or args.results.exists():
        parser.error("use a fresh results directory and positive trial count")
    args.results.mkdir(parents=True)
    os.environ["AQOS_SERVICE"] = "rust"
    raw = {"go": [], "rust": []}
    scenario = REPO / "ml/tests/scenarios/s1_short.yaml"
    binaries = {name: {binary: hashlib.sha256((REPO / "bin" / ("go" if name == "go" else "") / (binary + ("-rs" if name == "rust" else ""))).read_bytes()).hexdigest()
                       for binary in ["aqosd", "aqos-cake-reader", "aqos-reflector", "aqos-watchdog"]} for name in raw}
    for trial in range(args.trials):
        for backend in (["go", "rust"] if trial % 2 == 0 else ["rust", "go"]):
            os.environ["AQOS_RUNTIME"] = backend
            directory = args.results / f"{trial}-{backend}"
            manifest = runner.run_one(scenario, "O2", 7 + trial, results=directory)
            out = directory / manifest["run_id"]
            assert manifest["valid"], (backend, manifest["invalid_reason"])
            report = json.loads((out / "controller.json").read_text())
            assert report["flows"]["errors"] == report["flows"]["mismatch"] == report["extractor"]["overflows"] == 0, report
            raw[backend].append({"trial": trial, "seed": 7 + trial, "cpu_s": report["cpu_s"],
                                 "process_cpu": json.loads((out / "cpu.json").read_text()),
                                 "flows": report["flows"], "result": str(out)})
            print(backend, trial, report["cpu_s"], flush=True)
    median = {name: statistics.median(t["cpu_s"] for t in values) for name, values in raw.items()}
    result = {"scenario": str(scenario.relative_to(REPO)), "classifier": "rust", "config": "O2",
              "binary_sha256": binaries, "trials": raw, "median_controller_cpu_s": median,
              "cpu_reduction_pct": 100 * (1 - median["rust"] / median["go"]),
              "limits": "Short laptop namespace runs, few flows. CPU excludes child tc/nft processes. Does not measure saturation, maximum throughput, real-router performance or long-run stability."}
    (args.results / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in ["median_controller_cpu_s", "cpu_reduction_pct"]}, indent=2))


if __name__ == "__main__":
    main()

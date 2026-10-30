#!/usr/bin/env python3
"""Reproducible offline extractor benchmark; does not measure router forwarding.

Uses equal captures, flow limits, keys and output sinks. Alternates execution
order, warms both binaries, pins each process to one CPU, and records raw trials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import struct
import subprocess
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
KEY = "00" * 32


def capture(path, flows):
    with path.open("wb") as out:
        out.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 128, 1))
        for packet in range(8):
            for flow in range(flows):
                host = bytes([192, 168, 1, flow % 200 + 1])
                remote = bytes([203, 0, 113, flow // 60000 + 1])
                sport, dport = 1024 + flow % 60000, 443
                if packet % 2:
                    host, remote, sport, dport = remote, host, dport, sport
                protocol = 6 if flow % 2 else 17
                length = 60 + (flow * 31 + packet * 97) % 1400
                ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, length, 0, 0, 64, protocol, 0, host, remote)
                l4 = (struct.pack("!HHIIBB", sport, dport, 0, 0, 0x50, 2 if packet == 0 else 16)
                      if protocol == 6 else struct.pack("!HHHH", sport, dport, 0, 0))
                frame = bytes(12) + b"\x08\x00" + (ip + l4 + bytes(64))[:64]
                micros = 10_000_000 + packet * 100_000 + flow
                out.write(struct.pack("<IIII", micros // 1_000_000, micros % 1_000_000, len(frame), length + 14))
                out.write(frame)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flows", type=int, default=50000)
    parser.add_argument("--trials", type=int, default=7)
    parser.add_argument("--out", type=Path, default=REPO / "reports/rust-runtime-benchmark.json")
    args = parser.parse_args()
    if not 100 <= args.flows <= 60000 or args.trials < 3:
        parser.error("use 100–60000 flows and at least 3 trials")
    bins = {name: REPO / "bin" / ("go" if name == "go" else "") / binary for name, binary in
            [("go", "aqos-extract"), ("rust", "aqos-extract-rs")]}
    cpu = min(os.sched_getaffinity(0))
    raw = {name: [] for name in bins}
    with tempfile.TemporaryDirectory(prefix="aqos-runtime-bench-") as tmp:
        tmp = Path(tmp)
        pcap = tmp / "input.pcap"
        capture(pcap, args.flows)
        common = ["-pcap", str(pcap), "-key", KEY, "-max-flows", str(args.flows + 1), "-window-every", "0"]
        # A complete parity pass must precede timing: faster dropped work is not an improvement.
        digests = {}
        for name, binary in bins.items():
            output = tmp / f"{name}.jsonl"
            subprocess.run([str(binary), *common, "-out", str(output)], check=True, capture_output=True)
            records = sorted((json.loads(line) for line in output.read_text().splitlines()), key=lambda r: r["flow_id"])
            assert len(records) == args.flows, (name, len(records))
            digests[name] = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
        assert digests["go"] == digests["rust"], "Go/Rust feature records differ"
        for trial in range(-2, args.trials):
            for name in (["go", "rust"] if trial % 2 == 0 else ["rust", "go"]):
                timing = tmp / "time.json"
                command = ["/usr/bin/time", "-f", '{"user_s":%U,"system_s":%S,"peak_rss_kib":%M}',
                           "-o", str(timing), "taskset", "-c", str(cpu), str(bins[name]), *common, "-out", "/dev/null"]
                start = time.perf_counter()
                subprocess.run(command, check=True, capture_output=True)
                elapsed = time.perf_counter() - start
                if trial >= 0:
                    raw[name].append({"trial": trial, "wall_s": elapsed, **json.loads(timing.read_text())})
        result = {
            "workload": {"flows": args.flows, "packets": args.flows * 8, "pcap_bytes": pcap.stat().st_size,
                         "parity_sha256": digests["go"], "windows": False, "output": "/dev/null", "warmups": 2},
            "environment": {"platform": platform.platform(), "cpu_affinity": cpu,
                            "cpu": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines() if line.startswith("model name")), "unknown"),
                            "go": subprocess.check_output(["go", "version"], text=True).strip(),
                            "rust": subprocess.check_output([str(Path.home() / ".cargo/bin/rustc"), "--version"], text=True).strip(),
                            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                            "working_tree": "uncommitted migration; binary hashes identify measured builds"},
            "binaries": {name: {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for name, path in bins.items()},
            "trials": raw,
            "median": {name: {key: statistics.median(t[key] for t in trials) for key in ["wall_s", "user_s", "system_s", "peak_rss_kib"]} for name, trials in raw.items()},
            "limits": "Offline initial-flow extraction on this host; not forwarding throughput, controller latency, window extraction, router hardware, or long-soak stability.",
        }
        result["speedup"] = result["median"]["go"]["wall_s"] / result["median"]["rust"]["wall_s"]
        result["rss_reduction_pct"] = 100 * (1 - result["median"]["rust"]["peak_rss_kib"] / result["median"]["go"]["peak_rss_kib"])
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({k: result[k] for k in ["median", "speedup", "rss_reduction_pct"]}, indent=2))


if __name__ == "__main__":
    main()

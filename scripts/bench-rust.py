#!/usr/bin/env python3
"""Reproducible matched Go/Rust comparison; raw samples and metadata in JSON.

Run after `make build build-rust`. Each repetition runs all implementations,
randomising their order. Core timings exclude loading. Socket timings include
JSON, inference, policy, proposal generation, IPC and client read overhead.
"""
import argparse
import contextlib
import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import socket
import statistics
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def command_output(argv):
    return subprocess.check_output(argv, cwd=ROOT, text=True).strip()


def percentile(values, percent):
    ordered = sorted(values)
    pos = (len(ordered) - 1) * percent / 100
    lo = int(pos)
    return ordered[lo] + (ordered[min(lo+1, len(ordered)-1)] - ordered[lo]) * (pos-lo)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rss_kib(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return None


@contextlib.contextmanager
def running_service(binary, model, directory, cpu, env):
    path = directory / "bench.sock"
    with (directory / "service.log").open("w+") as log:
        proc = subprocess.Popen(["taskset", "-c", str(cpu), str(binary), "-socket", str(path), "-model", str(model),
                                 "-damage", str(ROOT / "configs/damage_table.yaml")], cwd=ROOT, env=env, stderr=log)
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(10)
        try:
            end = time.monotonic() + 20
            while True:
                try:
                    conn.connect(str(path))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if proc.poll() is not None or time.monotonic() > end:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    time.sleep(0.01)
            with conn.makefile("rb") as reader:
                yield conn, reader, proc
        finally:
            conn.close()
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            path.unlink(missing_ok=True)


def socket_sample(binary, model, directory, requests, cpu, env):
    with running_service(binary, model, directory, cpu, env) as (conn, reader, proc):
        def exchange(raw):
            conn.sendall(raw)
            answer = reader.readline()
            if not answer:
                raise RuntimeError("classifier disconnected")
            return answer
        # Warm both implementations on the entire same corpus.
        for req in requests:
            if not json.loads(exchange(req))["ok"]:
                raise RuntimeError("invalid warmup response")
        latencies, inference = [], []
        for req in requests:
            start = time.perf_counter_ns()
            answer = exchange(req)
            latencies.append((time.perf_counter_ns() - start) / 1000)
            obj = json.loads(answer)  # decoding outside the timed round trip
            if not obj["ok"]:
                raise RuntimeError(obj)
            inference.append(obj["classification"]["inference_us"])
        return {"requests": len(requests), "roundtrip_us": latencies,
                "p50_us": percentile(latencies, 50), "p95_us": percentile(latencies, 95),
                "p99_us": percentile(latencies, 99), "mean_us": statistics.mean(latencies),
                "median_reported_inference_us": statistics.median(inference), "rss_kib": rss_kib(proc.pid)}


def ratio_summary(go, rust, rng, comparator="go"):
    ratios = [a/b for a,b in zip(go, rust)]
    bootstrap = [statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(5000)]
    return {comparator + "_median": statistics.median(go), "rust_median": statistics.median(rust),
            "paired_speedup_median": statistics.median(ratios),
            "paired_speedup_bootstrap_95pct": [percentile(bootstrap, 2.5), percentile(bootstrap, 97.5)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--baseline-dir", type=Path,
                        help="also compare saved aqos-classify-rs and aqos-core-bench-rs binaries")
    args = parser.parse_args()
    if args.repeats < 3 or args.rounds < 1:
        parser.error("need at least three repetitions and one round")
    fixture_path = ROOT / "internal/classify/testdata/fixture.json.gz"
    with gzip.open(fixture_path) as file:
        fixture = json.load(file)
    model = ROOT / "models" / fixture["model_version"] / "trees.json"
    damage = ROOT / "configs/damage_table.yaml"
    core = {"go": ROOT / "bin/go/aqos-core-bench", "rust": ROOT / "bin/aqos-core-bench-rs"}
    services = {"go": ROOT / "bin/go/aqos-classify", "rust": ROOT / "bin/aqos-classify-rs"}
    if args.baseline_dir:
        core["rust_baseline"] = args.baseline_dir.resolve() / "aqos-core-bench-rs"
        services["rust_baseline"] = args.baseline_dir.resolve() / "aqos-classify-rs"
    def binary_name(path):
        return os.path.relpath(path, ROOT)
    for binary in [*core.values(), *services.values()]:
        if not binary.is_file():
            parser.error(f"missing {binary}; run make build build-rust")
    affinity = sorted(os.sched_getaffinity(0))
    cpu, client_cpu = affinity[0], affinity[-1]
    os.sched_setaffinity(0, {client_cpu})
    env = {**os.environ, "GOMAXPROCS": "1"}
    rustc = Path.home() / ".cargo/bin/rustc"
    rng = random.Random(20261021)
    report = {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "host": {"platform": platform.platform(), "processor": command_output(["lscpu"]),
                       "benchmark_cpu": cpu, "client_cpu": client_cpu, "GOMAXPROCS": 1},
              "versions": {"go": command_output(["go", "version"]), "rust": command_output([str(rustc), "--version"]),
                           "git": command_output(["git", "rev-parse", "HEAD"]), "dirty": bool(command_output(["git", "status", "--porcelain"]))},
              "inputs": {"model": str(model.relative_to(ROOT)), "model_sha256": digest(model),
                         "fixture_sha256": digest(fixture_path), "damage_sha256": digest(damage),
                         "binary_sha256": {binary_name(p): digest(p) for p in [*core.values(), *services.values()]},
                         "binary_bytes": {binary_name(p): p.stat().st_size for p in [*core.values(), *services.values()]},
                         "cases": len(fixture["cases"]), "rounds": args.rounds, "repeats": args.repeats},
              "method": "Same model, corpus order, f64 arithmetic and policy; release Rust, normal optimised Go. Single CPU per child, GOMAXPROCS=1. Random backend order within each repetition. Core excludes load/JSON/validation; pipeline includes vectorisation, inference and policy. Socket measures warmed sequential persistent-connection requests; client JSON decoding excluded. RSS is a snapshot after requests, not peak. Speedup intervals bootstrap the repetition pairs, not individual packets. Binary sizes reflect each build's flags (Rust strips symbols; Go uses the existing build), not a language-only comparison. No router or network-QoE claim.",
              "core": [], "socket": [], "summary": {}}
    requests = [(json.dumps({"op": "classify", "id": i+1, "record": case["record"],
                             "previous_tin": None, "leased_classes": []}, separators=(",", ":")).encode() + b"\n")
                for i, case in enumerate(fixture["cases"])]
    try:
        with tempfile.TemporaryDirectory(prefix="aqos-rust-bench-") as temporary:
            directory = Path(temporary)
            corpus = directory / "corpus.json"
            corpus.write_text(json.dumps(fixture))
            for repeat in range(args.repeats):
                for mode in ("infer", "pipeline", "socket"):
                    order = list(core)
                    rng.shuffle(order)
                    for backend in order:
                        print(f"pair {repeat+1}/{args.repeats}: {mode} {backend}", flush=True)
                        if mode == "socket":
                            sample = socket_sample(services[backend], model, directory, requests, cpu, env)
                            sample.update(backend=backend, repeat=repeat)
                            report["socket"].append(sample)
                        else:
                            output = subprocess.check_output(["taskset", "-c", str(cpu), str(core[backend]), str(model), str(corpus),
                                                              str(damage), str(args.rounds), mode], cwd=ROOT, env=env, text=True)
                            sample = json.loads(output)
                            sample["backend"] = backend
                            sample["repeat"] = repeat
                            report["core"].append(sample)
                # Save progress so interrupted runs are visible, never presented as complete.
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(report, indent=2) + "\n")
        for comparator in (b for b in core if b != "rust"):
            summary = report["summary"] if comparator == "go" else report.setdefault("versus_" + comparator, {})
            for mode in ("infer", "pipeline"):
                samples = {b: [r for r in report["core"] if r["backend"] == b and r["mode"] == mode] for b in (comparator, "rust")}
                for a, b in zip(samples[comparator], samples["rust"]):
                    if abs(a["checksum"] - b["checksum"]) > 1e-7 * max(1, abs(a["checksum"])):
                        raise RuntimeError("core benchmark checksum mismatch")
                summary[mode + "_ns"] = ratio_summary(*[[r["ns_per_op"] for r in samples[b]] for b in (comparator, "rust")], rng, comparator)
            for metric in ("p50_us", "p95_us", "p99_us", "rss_kib"):
                summary["socket_" + metric] = ratio_summary(*[[r[metric] for r in report["socket"] if r["backend"] == b] for b in (comparator, "rust")], rng, comparator)
        report["complete"] = True
        args.out.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report["summary"], indent=2))
    finally:
        os.sched_setaffinity(0, set(affinity))


if __name__ == "__main__":
    main()

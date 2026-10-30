#!/usr/bin/env python3
"""One continuous Rust O2 run, with bounded resource sampling and no retries.

Owns the isolated testbed through the normal runner lock. SIGTERM/Ctrl-C requests
runner cleanup. A short --seconds run validates this harness, not six-hour stability.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import time

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ml"))
from aqos_ml.run import runner  # noqa: E402

RUNTIMES = {"aqosd-rs", "aqos-classify-rs", "aqos-watchdog-rs",
            "aqos-reflector-rs", "aqos-cake-reader-rs"}


def scenario(seconds):
    """Interactive traffic, saturation and flow churn; repeated capacity changes."""
    result = yaml.safe_load((REPO / "ml/tests/scenarios/s1_short.yaml").read_text())
    result.update(id="S903", name="Continuous Rust stability", duration_s=seconds, warmup_s=10)
    result["traffic"][0].update(profile="gaming", params={"tick_hz": 30})
    result["traffic"][1].update(start_s=4, params={"connections": 2})
    result["traffic"].append({"id": "churn", "host": "desktop", "profile": "flow_storm",
                              "start_s": 0, "stop_s": None,
                              "params": {"flows_per_s": 5, "udp_share": 0.5}})
    result["description"] = "30 Hz gaming, a two-connection download, and five short flows/s."
    # Enough time for the verifier to settle between changes in a full soak.
    period = min(600, max(20, seconds // 3))
    result["events"] = [
        {"at_s": t, "type": "link_rate", "args": {
            "direction": "down", "rate_kbit": 20000 if i % 2 == 0 else 100000}}
        for i, t in enumerate(range(20, seconds + 10 - 10, period))]
    return result


def sample_processes(root, proc_root=Path("/proc")):
    """Discover descendants; key samples by PID AND start time to detect PID reuse."""
    rows, children = {}, {}
    for path in proc_root.glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            pid = int(path.parent.name)
            rows[pid] = fields
            children.setdefault(int(fields[1]), []).append(pid)
        except (OSError, ValueError, IndexError):
            continue  # process exited between enumeration and read
    samples, todo = [], [root]
    while todo:
        pid = todo.pop()
        todo.extend(children.get(pid, []))
        if pid not in rows:
            continue
        path, fields = proc_root / str(pid), rows[pid]
        try:
            binary = (path / "exe").resolve(strict=True).name
            if binary not in RUNTIMES:
                continue
            samples.append({"binary": binary, "pid": pid, "start_ticks": int(fields[19]),
                            "cpu_s": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                            "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
                            "fds": len(list((path / "fd").iterdir()))})
        except (OSError, ValueError, IndexError):
            continue
    return samples


def resource_summary(path):
    # Keep fixed-size initial/tail windows and maxima, not all six hours in RAM.
    from collections import deque
    grouped = {}
    with path.open() as stream:
        for line in stream:
            for row in json.loads(line)["processes"]:
                group = grouped.setdefault(row["binary"], {
                    "identities": set(), "samples": 0, "rss_peak_bytes": 0,
                    "fd_peak": 0, "first": [], "last": deque(maxlen=12)})
                group["identities"].add((row["pid"], row["start_ticks"]))
                group["samples"] += 1
                group["rss_peak_bytes"] = max(group["rss_peak_bytes"], row["rss_bytes"])
                group["fd_peak"] = max(group["fd_peak"], row["fds"])
                if len(group["first"]) < 12:
                    group["first"].append(row["rss_bytes"])
                group["last"].append(row["rss_bytes"])
    for group in grouped.values():
        group["process_identities"] = len(group.pop("identities"))
        group["rss_first_median_bytes"] = statistics.median(group.pop("first"))
        group["rss_last_median_bytes"] = statistics.median(group.pop("last"))
    return grouped


def outcome(out, seed, resources):
    run = out / "runs" / runner.run_id("S903", "O2", seed)
    problems = []
    try:
        manifest = json.loads((run / "manifest.json").read_text())
        controller = json.loads((run / "controller.json").read_text())
        env = json.loads((run / "environment.json").read_text())
        if not manifest["valid"]:
            problems.append(manifest["invalid_reason"])
        if env["runtime"] != "rust" or env["service"] != "rust":
            problems.append("runtime or classifier was not Rust")
        for key in ("errors", "mismatch", "service_down", "timeouts", "service_busy"):
            if controller["flows"][key]:
                problems.append(f"controller {key}: {controller['flows'][key]}")
        if controller["extractor"]["overflows"]:
            problems.append("extractor overflow")
        if not controller["flows"]["labelled"] or not controller["flows"]["rate_changes"]:
            problems.append("labels or capacity changes were never applied")
    except (OSError, ValueError, KeyError) as exc:
        problems.append(f"missing or invalid run evidence: {exc}")
    if (run / "heartbeat.safe").exists():
        problems.append("watchdog entered safe state during an uninterrupted run")
    for name in sorted(RUNTIMES):
        if name not in resources:
            problems.append(f"no resource samples for {name}")
        elif resources[name]["process_identities"] != 1:
            problems.append(f"{name} changed process identity")
    return problems


def child(out, seed):
    # Exactly one attempt: a failed six-hour run must never silently restart.
    manifest = runner.run_one(out / "scenario.yaml", "O2", seed, results=out / "runs")
    return 0 if manifest["valid"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True, help="fresh results directory")
    parser.add_argument("--seconds", type=int, default=21600, help="measured seconds, plus 10s warm-up")
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument("--reserve-mib", type=int, default=1024, help="stop below this free space")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    out = args.results.resolve()
    if args.child:
        try:
            return child(out, args.seed)
        except runner.RunError as exc:
            print(f"soak runner: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("soak runner interrupted; testbed cleanup requested", file=sys.stderr)
            return 130
    if args.seconds < 60 or args.reserve_mib < 256:
        parser.error("use at least 60 seconds and 256 MiB disk reserve")
    if out.exists():
        parser.error("results directory already exists; choose a fresh directory")
    # Do not inherit ablation flags that would change what this test measures.
    forbidden = [k for k in ("AQOS_RUNTIME_DIR", "AQOS_SERVE_ARGS", "AQOSD_ARGS",
                             "AQOS_ENFORCE_FAULT", "SHAPER_PCT") if os.environ.get(k)]
    if forbidden:
        parser.error(f"unset experiment overrides: {', '.join(forbidden)}")
    hashes = {name: hashlib.sha256((REPO / "bin" / name).read_bytes()).hexdigest()
              for name in sorted(RUNTIMES)}
    out.parent.mkdir(parents=True, exist_ok=True)
    reserve = args.reserve_mib * 1024 ** 2
    if shutil.disk_usage(out.parent).free < reserve:
        parser.error("insufficient free disk space for the reserve")
    out.mkdir()
    (out / "scenario.yaml").write_text(yaml.safe_dump(scenario(args.seconds)))
    report = {"status": "running", "measured_seconds": args.seconds, "seed": args.seed,
              "binary_sha256": hashes, "reserve_mib": args.reserve_mib,
              "limits": "Host namespace stability only; not router throughput or leak proof. "
                        "CPU samples exclude child tc/nft commands. Exporter is not exercised."}
    report_path = out / "soak.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    started = time.monotonic()
    interrupted = []
    previous = {s: signal.signal(s, lambda signum, frame: interrupted.append(signum))
                for s in (signal.SIGINT, signal.SIGTERM)}
    env = {**os.environ, "AQOS_RUNTIME": "rust", "AQOS_SERVICE": "rust",
           "AQOS_ESTIMATOR": "1", "AQOS_RECLASSIFIER": "1"}
    reasons = []
    proc = None
    try:
        with (out / "runner-output.txt").open("w") as log, (out / "resources.jsonl").open("w") as samples:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child",
                                     "--results", str(out), "--seed", str(args.seed)],
                                    cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
            report["monitor_pid"], report["runner_pid"] = os.getpid(), proc.pid
            report_path.write_text(json.dumps(report, indent=2) + "\n")
            while proc.poll() is None:
                elapsed = time.monotonic() - started
                free = shutil.disk_usage(out).free
                samples.write(json.dumps({"elapsed_s": elapsed, "free_bytes": free,
                                          "processes": sample_processes(proc.pid)}) + "\n")
                samples.flush()
                if interrupted:
                    reasons.append("interrupted")
                elif free < reserve:
                    reasons.append("disk reserve reached")
                elif elapsed > args.seconds + 180:
                    reasons.append("run exceeded duration plus 180s cleanup allowance")
                if reasons:
                    break
                time.sleep(1 if elapsed < 20 else 5)
    except Exception as exc:
        reasons.append(f"monitor failed: {type(exc).__name__}: {exc}")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()  # runner owns testbed teardown
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                reasons.append("runner cleanup timed out; inspect testbed state")
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    try:
        resources = resource_summary(out / "resources.jsonl")
    except (OSError, ValueError, KeyError) as exc:
        resources = {}
        reasons.append(f"invalid resource evidence: {exc}")
    reasons.extend(outcome(out, args.seed, resources))
    if proc is not None and proc.returncode:
        reasons.append(f"runner exit {proc.returncode}")
    report.update(status="failed" if reasons else "passed", problems=reasons,
                  elapsed_s=round(time.monotonic() - started, 3), resources=resources)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())

"""Starts a scenario's generators, on localhost or inside the testbed.

Only for tests. The experiment runner (chunk 3C) is the real thing.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTBED = REPO / "bin" / "aqos-testbed"


def start(scenario_path, scenario, seed, out, testbed=False, only=None, lead_s=3.0):
    """Returns {(traffic_id, role): Popen}. Servers are started first."""
    env = {**os.environ, "PYTHONPATH": str(REPO / "ml")}
    t0_ns = time.monotonic_ns() + int(lead_s * 1e9)
    procs = {}
    for role in ("server", "client"):
        for entry in scenario["traffic"]:
            if only and entry["id"] not in only:
                continue
            cmd = [sys.executable, "-m", "aqos_ml.gen", role, "--scenario", str(scenario_path),
                   "--traffic-id", entry["id"], "--seed", str(seed), "--run-id", f"{scenario['id']}-B0-s{seed:04d}",
                   "--t0-ns", str(t0_ns), "--out", str(out)]
            if testbed:
                cmd += ["--server4", "10.78.0.10", "--server6", "fd77:0:0:4::2"]
                ns = "server" if role == "server" else "host-" + entry["host"]
                cmd = [str(TESTBED), "exec", ns, "--", *cmd]
            else:
                cmd += ["--server4", "127.0.0.1", "--server6", "::1"]
            procs[entry["id"], role] = subprocess.Popen(cmd, env=env, stderr=subprocess.PIPE, text=True)
        if role == "server":
            time.sleep(0.4)
    return procs


def wait(procs, timeout_s):
    """Returns {(traffic_id, role): (exit code, stderr)}."""
    deadline = time.monotonic() + timeout_s
    results = {}
    for key, proc in procs.items():
        try:
            _, err = proc.communicate(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
            err += "\n(killed: did not exit in time)"
        results[key] = (proc.returncode, err)
    return results

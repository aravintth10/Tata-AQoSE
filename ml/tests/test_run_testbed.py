"""Real runs through the testbed. Replaces any running testbed, so it runs
only when asked: make testbed-check."""

import json
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict

import numpy as np
import pytest
from jsonschema import Draft202012Validator

from aqos_ml.run import runner, summary

from harness import REPO

pytestmark = pytest.mark.skipif(not os.environ.get("AQOS_INTEGRATION"), reason="set AQOS_INTEGRATION=1")
SCENARIO = REPO / "ml/tests/scenarios/s1_short.yaml"
SAMPLE = Draft202012Validator(json.loads((REPO / "contracts/schema/metric_sample.schema.json").read_text()))
MANIFEST = Draft202012Validator(json.loads((REPO / "contracts/schema/run_manifest.schema.json").read_text()))


def after_download(out, metric, traffic_id, direction):
    """Values once the download is running (it starts at 8 s)."""
    values = []
    for line in (out / "metrics.jsonl").read_text().splitlines():
        s = json.loads(line)
        if s["metric"] == metric and s.get("traffic_id") == traffic_id and s.get("direction") == direction and s["t_s"] >= 10:
            values.append(s["value"])
    return np.array(values)


def test_cake_beats_the_fifo(tmp_path):
    """Chunk 3A's done-when, in miniature: the same call and download, with
    and without CAKE."""
    b0 = runner.run_one(SCENARIO, "B0", 7, results=tmp_path)
    b2 = runner.run_one(SCENARIO, "B2", 7, results=tmp_path)
    for manifest in (b0, b2):
        assert manifest["valid"], manifest["invalid_reason"]
        assert [e.message for e in MANIFEST.iter_errors(manifest)] == []
    out0, out2 = tmp_path / b0["run_id"], tmp_path / b2["run_id"]
    print("\n" + "\n".join(summary.summarise(out0) + summary.summarise(out2)))

    # The download fills the link in both; only the call's fate differs.
    for out in (out0, out2):
        assert after_download(out, "throughput_kbit", "download", "down").mean() > 80_000
    fifo = np.percentile(after_download(out0, "latency_ms", "call", "down"), 95)
    cake = np.percentile(after_download(out2, "latency_ms", "call", "down"), 95)
    assert fifo > 200, "the bloated FIFO should hurt the call"
    assert cake < 30, "CAKE should keep the call near the path delay"

    # Every line of the run follows the contract, whichever process wrote it.
    by_source = defaultdict(set)
    for line in (out2 / "metrics.jsonl").read_text().splitlines():
        s = json.loads(line)
        assert [e.message for e in SAMPLE.iter_errors(s)] == [], s
        by_source[s["source"]].add(s["metric"])
    assert by_source["cake"] == {"queue_delay_ms", "queue_backlog_bytes", "tin_drops", "shaper_rate_kbit"}
    assert by_source["system"] == {"capacity_true_kbit"}
    assert "cake" not in {json.loads(line)["source"] for line in (out0 / "metrics.jsonl").read_text().splitlines()}
    # Same seed, same traffic, whatever the configuration.
    digest = lambda out: json.loads((out / "gen" / "call.client.json").read_text())["plan_digest"]  # noqa: E731
    assert digest(out0) == digest(out2)
    assert json.loads((out2 / "environment.json").read_text())["testbed_mode"] in ("rootless", "root")
    assert (tmp_path / "current").resolve() == out2.resolve()


def test_a_killed_runner_is_resumed(tmp_path):
    """Chunk 3C's done-when: kill the runner part way, run again, and only
    what is missing gets run."""
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text(f"scenarios: [{SCENARIO}]\nconfigs: [B2]\nseeds: [1, 2]\n")
    cmd = [sys.executable, "-m", "aqos_ml.run", "matrix", str(matrix), "--results", str(tmp_path)]
    env = {**os.environ, "PYTHONPATH": str(REPO / "ml")}

    first = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, text=True)
    # Wait for the first run to finish, then kill during the second.
    deadline = time.time() + 90
    while not (tmp_path / "S901-B2-s0001" / "manifest.json").exists():
        assert time.time() < deadline and first.poll() is None
        time.sleep(0.5)
    time.sleep(6)
    first.send_signal(signal.SIGTERM)
    first.wait(30)
    assert not (tmp_path / "S901-B2-s0002" / "manifest.json").exists()
    assert subprocess.run([str(REPO / "bin/aqos-testbed"), "status"], capture_output=True).returncode != 0, \
        "an interrupted runner must tear the testbed down"

    second = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "skip   S901-B2-s0001: already valid" in second.stdout
    assert "run    S901-B2-s0002" in second.stdout
    assert "2 of 2 runs valid" in second.stdout

"""Runs every profile through the emulated network. Replaces any running
testbed, so it runs only when asked: make testbed-check."""

import json
import os
import subprocess
from collections import defaultdict

import pytest
import yaml

from aqos_ml.gen import validate

import harness
from harness import REPO, TESTBED

pytestmark = pytest.mark.skipif(not os.environ.get("AQOS_INTEGRATION"), reason="set AQOS_INTEGRATION=1")

SCENARIO_PATH = REPO / "ml/tests/scenarios/all_profiles.yaml"
SCENARIO = yaml.safe_load(SCENARIO_PATH.read_text())


@pytest.fixture
def testbed():
    def up(path):
        subprocess.run([TESTBED, "up", path], check=True, capture_output=True)

    yield up
    subprocess.run([TESTBED, "down"], check=True, capture_output=True)


def samples(out):
    by = defaultdict(list)
    for line in (out / "metrics.jsonl").read_text().splitlines():
        s = json.loads(line)
        by[s["traffic_id"], s["metric"], s.get("direction")].append(s["value"])
    return by


def test_every_profile_crosses_the_testbed(testbed, tmp_path):
    testbed(SCENARIO_PATH)
    procs = harness.start(SCENARIO_PATH, SCENARIO, 7, tmp_path, testbed=True)
    results = harness.wait(procs, 25)
    assert {k: err[-300:] for k, (code, err) in results.items() if code} == {}
    assert validate.check(tmp_path, SCENARIO) == []
    by = samples(tmp_path)
    # One way is 10 ms of emulated delay; nothing can arrive sooner.
    for tid in ("voice", "call", "game", "cloud"):
        for direction in ("up", "down"):
            assert min(by[tid, "latency_ms", direction]) >= 10, (tid, direction)
    # The upload is saturated by the backup and p2p and has no QoS: the
    # 500 ms FIFO must show up as call latency.
    assert max(by["voice", "latency_ms", "up"]) > 100
    # IPv4 flows are logged with the host's own address, before NAT.
    flows = [json.loads(line) for line in (tmp_path / "flows.jsonl").read_text().splitlines()]
    assert {f["src_ip"] for f in flows if f["traffic_id"] == "voice"} == {"10.77.0.10"}
    assert {f["dst_ip"] for f in flows if f["traffic_id"] == "game"} == {"fd77:0:0:4::2"}


def test_cloud_gaming_sender_holds_its_schedule(testbed, tmp_path):
    """The spec's risk for 2B: Python may not hold timing at about 2,600
    packets per second. Alone on the link, the stream must also arrive
    whole and on time."""
    scenario = {
        **SCENARIO, "duration_s": 20, "hosts": ["desktop"],
        "traffic": [{"id": "cloud", "host": "desktop", "profile": "cloud_gaming", "start_s": 0,
                     "stop_s": None, "params": {"kbit_down": 25000}}],
    }
    path = tmp_path / "cloud.yaml"
    path.write_text(yaml.safe_dump(scenario))
    testbed(path)
    results = harness.wait(harness.start(path, scenario, 7, tmp_path, testbed=True), 40)
    assert {k: err[-300:] for k, (code, err) in results.items() if code} == {}
    stats = json.loads((tmp_path / "gen" / "cloud.server.json").read_text())["stats"]
    print("\ncloud_gaming at 25 Mbit/s:", stats)
    assert stats["sent"] / 21 > 2400
    assert stats["send_late_us"]["p99"] < 1000
    by = samples(tmp_path)
    assert sum(by["cloud", "frame_miss", "down"]) == 0
    assert max(by["cloud", "loss_ratio", "down"]) == 0
    assert sorted(by["cloud", "throughput_kbit", "down"])[10] == pytest.approx(25000, rel=0.05)

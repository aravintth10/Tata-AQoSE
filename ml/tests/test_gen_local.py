"""Runs the real generator processes against each other on localhost."""

import json
import os
import signal
import time
from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from aqos_ml.gen import validate

import harness
from harness import REPO

SCENARIO_PATH = REPO / "ml/tests/scenarios/all_profiles.yaml"
SCENARIO = yaml.safe_load(SCENARIO_PATH.read_text())
SAMPLE = Draft202012Validator(json.loads((REPO / "contracts/schema/metric_sample.schema.json").read_text()))
SCENARIO_SCHEMA = Draft202012Validator(json.loads((REPO / "contracts/schema/scenario.schema.json").read_text()))

MEDIA = {"latency_ms", "jitter_ms", "loss_ratio", "throughput_kbit"}
# (metric, direction) each profile must produce.
EXPECTED = {
    "voice_call": {(m, d) for m in MEDIA for d in ("up", "down")},
    "video_call": {(m, d) for m in MEDIA | {"frame_miss"} for d in ("up", "down")},
    "gaming": {(m, d) for m in MEDIA | {"frame_miss"} for d in ("up", "down")},
    "cloud_gaming": {(m, d) for m in MEDIA | {"frame_miss"} for d in ("up", "down")},
    "abr_video": {("throughput_kbit", "down"), ("video_stall_ms", None), ("video_bitrate_kbit", None)},
    "web_browsing": {("throughput_kbit", "down"), ("page_load_ms", None)},
    "bulk_download": {("throughput_kbit", "down")},
    "game_update": {("throughput_kbit", "down")},
    "backup_upload": {("throughput_kbit", "up")},
    "p2p": {("throughput_kbit", "up"), ("throughput_kbit", "down")},
    "flow_storm": {("throughput_kbit", "down")},
}

GREEDY = {"bulk_download", "game_update", "backup_upload", "p2p"}


def run_end():
    return SCENARIO["warmup_s"] + SCENARIO["duration_s"]


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_test_scenario_is_valid():
    assert [e.message for e in SCENARIO_SCHEMA.iter_errors(SCENARIO)] == []
    assert {t["profile"] for t in SCENARIO["traffic"]} == set(EXPECTED)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """Every entry of the scenario, in two passes into one result directory.

    Loopback has no bottleneck, so the greedy profiles would take every core
    and make the media profiles late. On the testbed they share a link
    instead, and run together.
    """
    out = tmp_path_factory.mktemp("run")
    results = {}
    for group in (set(EXPECTED) - GREEDY, GREEDY):
        only = {t["id"] for t in SCENARIO["traffic"] if t["profile"] in group}
        results.update(harness.wait(harness.start(SCENARIO_PATH, SCENARIO, 7, out, only=only), run_end() + 15))
    return out, results


def test_every_generator_exits_cleanly(run):
    out, results = run
    failed = {key: err[-400:] for key, (code, err) in results.items() if code != 0}
    assert failed == {}
    assert validate.check(out, SCENARIO) == []


def test_metric_samples_follow_the_contract(run):
    out, _ = run
    samples = read_jsonl(out / "metrics.jsonl")
    seen = defaultdict(set)
    for s in samples:
        assert [e.message for e in SAMPLE.iter_errors(s)] == [], s
        assert s["source"] == "generator"
        assert 0 <= s["t_s"] <= run_end() + 1.5
        seen[s["profile"]].add((s["metric"], s.get("direction")))
    assert dict(seen) == EXPECTED


def test_loopback_media_is_clean(run):
    """With no bottleneck nothing may be lost, late or stalled, so anything
    else here is the generators' own error."""
    out, _ = run
    by = defaultdict(list)
    for s in read_jsonl(out / "metrics.jsonl"):
        by[s["traffic_id"], s["metric"]].append(s["value"])
    for tid in ("voice", "call", "game", "cloud"):
        assert max(by[tid, "loss_ratio"]) == 0, tid
        latency = sorted(by[tid, "latency_ms"])
        assert 0 < latency[len(latency) // 2] < 5, tid
        assert latency[int(len(latency) * 0.99)] < 20, tid
    for tid in ("call", "game", "cloud"):
        assert sum(by[tid, "frame_miss"]) == 0, tid
    assert sum(by["tv", "video_stall_ms"]) == 0
    assert max(by["tv", "video_bitrate_kbit"]) > 0
    for tid in ("bulk", "update", "backup", "torrent"):
        assert max(by[tid, "throughput_kbit"]) > 10_000, tid


def test_media_rates_match_the_plan(run):
    out, _ = run
    summary = json.loads((out / "gen" / "voice.client.json").read_text())
    by = defaultdict(list)
    for s in read_jsonl(out / "metrics.jsonl"):
        if s["traffic_id"] == "voice" and s["metric"] == "throughput_kbit":
            by[s["direction"]].append(s["value"])
    for direction in ("up", "down"):
        want = summary["params"]["payload_" + direction] * 8 * 50 / 1000
        full_seconds = sorted(by[direction])[len(by[direction]) // 2]
        assert full_seconds == pytest.approx(want, rel=0.1)


def test_send_timing_is_reported(run):
    """How late each packet left is recorded for every media sender. The
    testbed run checks the figure at full cloud-gaming rate."""
    out, _ = run
    for name in ("cloud.server", "call.client", "voice.client"):
        stats = json.loads((out / "gen" / f"{name}.json").read_text())["stats"]
        assert stats["sent"] > 0
        assert 0 <= stats["send_late_us"]["mean"] < 20_000, (name, stats)


def test_flows_are_logged_with_their_truth(run):
    out, _ = run
    flows = read_jsonl(out / "flows.jsonl")
    by = defaultdict(list)
    for f in flows:
        by[f["traffic_id"]].append(f)
    assert set(by) == {t["id"] for t in SCENARIO["traffic"]}
    classes = {t["id"]: t["profile"] for t in SCENARIO["traffic"]}
    for tid, entries in by.items():
        assert {f["profile"] for f in entries} == {classes[tid]}
    assert {f["ip_version"] for f in by["game"]} == {6}
    assert {f["protocol"] for f in by["voice"]} == {17}
    assert {f["protocol"] for f in by["torrent"]} == {6, 17}
    assert len(by["storm"]) > 20
    # One line per flow: the 5-tuples are distinct.
    tuples = [(f["protocol"], f["src_ip"], f["src_port"], f["dst_ip"], f["dst_port"]) for f in flows]
    assert len(set(tuples)) == len(tuples)


def test_a_killed_generator_makes_the_run_invalid(tmp_path):
    procs = harness.start(SCENARIO_PATH, SCENARIO, 7, tmp_path, only={"voice", "bulk"})
    time.sleep(3)
    os.kill(procs["bulk", "client"].pid, signal.SIGKILL)
    procs["voice", "server"].terminate()
    results = harness.wait(procs, run_end() + 15)
    assert results["voice", "server"][0] != 0
    one_scenario = {**SCENARIO, "traffic": [t for t in SCENARIO["traffic"] if t["id"] in ("voice", "bulk")]}
    problems = validate.check(tmp_path, one_scenario)
    assert any(p.startswith("bulk client: no summary") for p in problems)
    assert any(p.startswith("voice server:") and "terminated" in p for p in problems)


def test_a_client_without_a_server_fails(tmp_path):
    procs = harness.start(SCENARIO_PATH, SCENARIO, 7, tmp_path, only={"voice", "bulk"})
    for key in [("voice", "server"), ("bulk", "server")]:
        procs.pop(key).kill()
    results = harness.wait(procs, run_end() + 15)
    assert results["voice", "client"][0] != 0
    assert results["bulk", "client"][0] != 0
    assert "no packets received" in json.loads((tmp_path / "gen" / "voice.client.json").read_text())["error"]


def test_a_replayed_call_runs_clean_on_loopback(tmp_path):
    """The generators send a recorded call as recorded: nothing lost or late
    with no bottleneck, at the recording's rate."""
    from aqos_ml import trace as traces
    doc = {"schema_version": 1, "id": "S98", "name": "replay on loopback", "duration_s": 12, "warmup_s": 0,
           "link": {"down_kbit": 100000, "up_kbit": 100000, "delay_ms": 0}, "hosts": ["laptop"],
           "traffic": [{"id": "call", "host": "laptop", "profile": "video_call", "start_s": 0, "stop_s": None,
                        "params": {"trace": "whatsapp-video-010544"}}],
           "events": [], "primary_metrics": ["frame_miss"]}
    assert [e.message for e in SCENARIO_SCHEMA.iter_errors(doc)] == []
    path = tmp_path / "S98.yaml"
    path.write_text(yaml.safe_dump(doc))
    out = tmp_path / "run"
    results = harness.wait(harness.start(path, doc, 7, out), doc["duration_s"] + 15)
    assert {k: e[-400:] for k, (code, e) in results.items() if code != 0} == {}
    by = defaultdict(list)
    for s in read_jsonl(out / "metrics.jsonl"):
        by[s["metric"], s.get("direction")].append(s["value"])
    recorded = traces.load("whatsapp-video-010544")
    for direction in ("up", "down"):
        assert max(by["loss_ratio", direction]) == 0
        assert sum(by["frame_miss", direction]) == 0
        # The call's own first 12 s: a call starts slower than it goes on.
        sent = sum(size for t, size in zip(recorded[direction]["t_s"], recorded[direction]["size"]) if t < 12)
        got = sum(by["throughput_kbit", direction])  # one sample a second
        assert got == pytest.approx(sent * 8 / 1000, rel=0.25)

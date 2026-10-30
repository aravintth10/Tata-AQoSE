"""The live extractor, inside the router of a real testbed. Replaces any
running testbed, so it runs only when asked: make testbed-check."""

import json
import os
import subprocess
import time

import pytest
import yaml
from jsonschema import Draft202012Validator

from aqos_ml.gen import validate

import harness
from harness import REPO, TESTBED

pytestmark = pytest.mark.skipif(not os.environ.get("AQOS_INTEGRATION"), reason="set AQOS_INTEGRATION=1")
RECORD = Draft202012Validator(json.loads((REPO / "contracts/schema/feature_record.schema.json").read_text()))
EXTRACT = REPO / "bin" / "aqos-extract"
KEY = "ab" * 32
LAN = "10.77.0.0/24,fd77:0:0:1::/64"


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def same_features(record):
    """What must be identical between the live and the offline path. ts_ns
    is the time of emission, and the window depends on when the one-second
    reads happened to fall."""
    return {k: v for k, v in record.items() if k not in ("ts_ns", "window")}


def test_live_and_offline_give_the_same_records(tmp_path):
    """Chunk 3B's done-when. Every profile runs through the router while the
    live extractor watches. It also saves each packet it was given; the
    offline tool, fed that file, must produce the same records."""
    scenario_path = REPO / "ml/tests/scenarios/all_profiles.yaml"
    scenario = yaml.safe_load(scenario_path.read_text())
    subprocess.run([TESTBED, "up", scenario_path], check=True, capture_output=True)
    try:
        live = subprocess.Popen(
            [TESTBED, "exec", "router", "--", EXTRACT, "-live", "-out", tmp_path / "live.jsonl",
             "-keys", tmp_path / "live.keys.jsonl", "-dump", tmp_path / "first8.pcap", "-key", KEY, "-lan", LAN,
             "-window-every", "2"], stderr=subprocess.PIPE, text=True)
        time.sleep(1)
        rule = subprocess.run([TESTBED, "exec", "router", "--", "nft", "list", "table", "inet", "aqos_extract"],
                              capture_output=True, text=True).stdout
        assert "ct packets < 9 log group 5 snaplen 64" in rule

        results = harness.wait(harness.start(scenario_path, scenario, 7, tmp_path, testbed=True), 30)
        assert {k: err[-300:] for k, (code, err) in results.items() if code} == {}
        assert validate.check(tmp_path, scenario) == []
        time.sleep(2.5)  # flows end, and the extractor sees their entries go
        live.terminate()
        _, summary = live.communicate(timeout=10)
        print("\n" + summary.strip())
        assert live.returncode == 0, summary
        assert " 0 buffer overflows" in summary
        rule_after = subprocess.run([TESTBED, "exec", "router", "--", "nft", "list", "tables"],
                                    capture_output=True, text=True).stdout
        assert "aqos_extract" not in rule_after, "the capture rule must go when the extractor stops"
    finally:
        subprocess.run([TESTBED, "down"], capture_output=True)

    records, keys = read(tmp_path / "live.jsonl"), read(tmp_path / "live.keys.jsonl")
    for record in records:
        assert [e.message for e in RECORD.iter_errors(record)] == [], record
    initial = {r["flow_id"]: r for r in records if r["kind"] == "initial"}
    windows = [r for r in records if r["kind"] == "window"]
    assert len(initial) == len(keys) > 50
    assert windows, "long flows must yield window records from conntrack's counters"

    # The same packets through the offline tool.
    subprocess.run([EXTRACT, "-pcap", tmp_path / "first8.pcap", "-out", tmp_path / "offline.jsonl",
                    "-key", KEY, "-lan", LAN], check=True, capture_output=True)
    offline = {r["flow_id"]: r for r in read(tmp_path / "offline.jsonl")}
    assert set(offline) == set(initial)
    different = [fid for fid in initial if same_features(initial[fid]) != same_features(offline[fid])]
    assert different == []

    # The kernel stops sending a flow after its eighth packet.
    sizes = (tmp_path / "first8.pcap").stat().st_size
    assert sizes < 2_000_000, "the extractor was sent far more than eight packets per flow"

    # Against the truth the generators logged: every flow they opened that
    # lasted eight packets has a record, with the right LAN side.
    by_tuple = {(k["protocol"], k["src_ip"], k["src_port"], k["dst_ip"], k["dst_port"]): k for k in keys}
    truth = read(tmp_path / "flows.jsonl")
    found = 0
    for flow in truth:
        key = by_tuple.get((flow["protocol"], flow["src_ip"], flow["src_port"], flow["dst_ip"], flow["dst_port"]))
        if key is None:
            # Only the short flows of web browsing and the flow storm may be missing.
            assert flow["profile"] in ("web_browsing", "flow_storm", "p2p"), flow
            continue
        found += 1
        assert key["lan_is_src"] is True
    long_lived = [f for f in truth if f["profile"] not in ("web_browsing", "flow_storm", "p2p")]
    assert found >= len(long_lived) > 15
    print(f"{len(initial)} flows classified live, {found} of {len(truth)} generator flows matched, "
          f"{len(windows)} window records")

    # A voice call: one packet every 20 ms each way, so 50 a second.
    voice = next(k for k in keys if (k["src_ip"], k["protocol"]) == ("10.77.0.10", 17)
                 and any(f["traffic_id"] == "voice" and f["src_port"] == k["src_port"] for f in truth))
    voice_windows = [r["window"] for r in windows if r["flow_id"] == voice["flow_id"]]
    assert len(voice_windows) >= 2
    # The first window is short and the last one spans the call's end; one
    # from the middle shows the steady rate.
    middle = voice_windows[len(voice_windows) // 2 - 1]
    print("voice call windows:", [(w["samples"], w["pkts_up_per_s"], w["pkts_down_per_s"]) for w in voice_windows])
    assert middle["pkts_up_per_s"] == pytest.approx(50, abs=3)
    assert middle["pkts_down_per_s"] == pytest.approx(50, abs=3)

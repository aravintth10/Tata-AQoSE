"""Checks the default extractor from outside: its records follow the contract,
and they agree with a second implementation written separately."""

import importlib.util
import ipaddress
import json
import os
import random
import struct
import subprocess

import pytest
from jsonschema import Draft202012Validator

from harness import REPO

RECORD = Draft202012Validator(json.loads((REPO / "contracts/schema/feature_record.schema.json").read_text()))
KEY = "00" * 32


@pytest.fixture(scope="module")
def extract(tmp_path_factory):
    if selected := os.environ.get("AQOS_EXTRACT_BIN"):
        from pathlib import Path
        binary = Path(selected).resolve(strict=True)
    else:
        binary = REPO / "bin/aqos-extract"
        if not binary.exists():
            subprocess.run(["make", "build"], cwd=REPO, check=True)

    def run(pcap, *args):
        out, keys = pcap.with_suffix(".features.jsonl"), pcap.with_suffix(".keys.jsonl")
        subprocess.run([binary, "-pcap", pcap, "-out", out, "-keys", keys, "-key", KEY, *args],
                       check=True, capture_output=True)
        read = lambda p: [json.loads(line) for line in p.read_text().splitlines()]  # noqa: E731
        return read(out), read(keys)

    return run


def frame(src, dst, protocol, sport, dport, l3_len, flags=0x10):
    """An Ethernet frame cut to 64 bytes of IP, as the capture rule keeps."""
    a, b = ipaddress.ip_address(src), ipaddress.ip_address(dst)
    if a.version == 4:
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, l3_len, 0, 0, 64, protocol, 0, a.packed, b.packed)
        ethertype = 0x0800
    else:
        ip = struct.pack("!IHBB16s16s", 6 << 28, l3_len - 40, protocol, 64, a.packed, b.packed)
        ethertype = 0x86DD
    l4 = struct.pack("!HHIIBB", sport, dport, 0, 0, 0x50, flags) if protocol == 6 else struct.pack("!HHHH", sport, dport, 0, 0)
    return bytes(12) + struct.pack("!H", ethertype) + (ip + l4 + bytes(64))[:64]


def write_pcap(path, packets):
    """packets: (time in seconds, frame)."""
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 128, 1)
    for t, data in sorted(packets, key=lambda p: p[0]):
        micros = round(t * 1e6)
        # The length on the wire, which a headers-only capture still records.
        if data[14] >> 4 == 4:
            wire = 14 + struct.unpack_from("!H", data, 16)[0]
        else:
            wire = 14 + 40 + struct.unpack_from("!H", data, 18)[0]
        out += struct.pack("<IIII", micros // 10**6, micros % 10**6, len(data), wire) + data
    path.write_bytes(out)


def synthetic_capture(seed=1):
    """A mix of flows a home router would see, as (time, frame) packets."""
    rng = random.Random(seed)
    packets = []
    lan4, lan6 = "192.168.1.20", "fd00::20"
    for i in range(40):
        v6 = i % 5 == 0
        host, far = (lan6, f"2001:db8::{i + 1}") if v6 else (lan4, f"203.0.113.{i + 1}")
        protocol = rng.choice([6, 17])
        sport, dport = 40000 + i, rng.choice([443, 80, 3478, 53, 51820])
        t = rng.uniform(0, 20)
        for n in range(rng.choice([3, 8, 9, 30, 400])):
            up = rng.random() < 0.5 or n == 0
            size = rng.randint(60 if protocol == 6 else 48, 1500)
            flags = 0x02 if (protocol == 6 and n == 0) else 0x10
            if up:
                packets.append((t, frame(host, far, protocol, sport, dport, size, flags)))
            else:
                packets.append((t, frame(far, host, protocol, dport, sport, size, flags)))
            t += rng.choice([0.0002, 0.003, 0.02, 0.05])
    return packets


def test_records_follow_the_contract(extract, tmp_path):
    pcap = tmp_path / "mix.pcap"
    write_pcap(pcap, synthetic_capture())
    records, keys = extract(pcap, "-window-every", "2")
    assert len(records) > 20
    for record in records:
        assert [e.message for e in RECORD.iter_errors(record)] == [], record
    initial = [r for r in records if r["kind"] == "initial"]
    window = [r for r in records if r["kind"] == "window"]
    assert window and all(r["window"] is not None for r in window)
    # One initial record per flow, one key line for each, and nothing else.
    assert len({r["flow_id"] for r in initial}) == len(initial) == len(keys)
    assert {r["flow_id"] for r in window} <= {r["flow_id"] for r in initial}
    # One LAN host of each address family.
    assert len({r["host_id"] for r in initial}) == 2
    assert not any("192.168" in json.dumps(r) or "203.0.113" in json.dumps(r) for r in records)


def test_agrees_with_a_second_implementation(extract, tmp_path):
    """The Kaggle script builds flows from captures with its own code, in
    another language. First-packet sizes, order and gaps must match."""
    spec = importlib.util.spec_from_file_location("flow_tables", REPO / "kaggle/pcap_flow_tables.py")
    other = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(other)

    pcap = tmp_path / "mix.pcap"
    write_pcap(pcap, synthetic_capture(seed=2))
    records, keys = extract(pcap)
    by_tuple = {(k["protocol"], k["src_port"], k["dst_port"]): k["flow_id"] for k in keys}
    ours = {r["flow_id"]: r for r in records}
    theirs, _ = other.read_capture(pcap)
    assert len(theirs) == len(ours) > 15
    for flow in theirs:
        record = ours[by_tuple[flow["protocol"], flow["src_port"], flow["dst_port"]]]
        first = flow["first"][:8]
        # Theirs signs by first sender, ours by LAN side; every flow here is
        # opened from the LAN, so the two agree.
        assert record["pkt_sizes"] == [size for _, size in first]
        gaps = [first[i + 1][0] - first[i][0] for i in range(7)]
        assert record["pkt_gaps_us"] == pytest.approx(gaps, abs=1)


def test_window_statistics_count_whole_seconds(extract, tmp_path):
    """50 packets of 200 bytes up and 100 of 1000 down in every second."""
    pcap = tmp_path / "steady.pcap"
    packets = []
    for second in range(12):
        for n in range(50):
            packets.append((second + n / 50, frame("192.168.1.20", "203.0.113.9", 17, 40000, 3478, 200)))
        for n in range(100):
            packets.append((second + n / 100 + 0.001, frame("203.0.113.9", "192.168.1.20", 17, 3478, 40000, 1000)))
    write_pcap(pcap, packets)
    records, _ = extract(pcap, "-window-every", "5")
    initial, *windows = records
    assert initial["kind"] == "initial" and initial["window"] is None
    assert [r["kind"] for r in windows] == ["window", "window"]
    w = windows[-1]["window"]
    assert w["samples"] == 5
    assert w["pkts_up_per_s"] == pytest.approx(50) and w["pkts_down_per_s"] == pytest.approx(100)
    assert w["bytes_up_per_s"] == pytest.approx(10_000) and w["bytes_down_per_s"] == pytest.approx(100_000)
    assert w["mean_pkt_size_up"] == pytest.approx(200) and w["mean_pkt_size_down"] == pytest.approx(1000)
    assert w["rate_cv"] == pytest.approx(0, abs=1e-9) and w["burstiness"] == pytest.approx(1)
    assert windows[-1]["pkt_sizes"] == initial["pkt_sizes"]


def test_short_flows_are_never_classified(extract, tmp_path):
    pcap = tmp_path / "short.pcap"
    write_pcap(pcap, [(i * 0.01, frame("192.168.1.20", "203.0.113.9", 17, 40000, 53, 80)) for i in range(7)])
    records, keys = extract(pcap)
    assert records == [] and keys == []

"""Differential runtime contracts; opt in after building the Rust runtime."""
import json
import os
import gzip
import struct
import subprocess

import pytest

from harness import REPO
from test_extract import KEY, frame, synthetic_capture, write_pcap

pytestmark = pytest.mark.skipif(not os.environ.get("AQOS_RUNTIME_TEST"), reason="set AQOS_RUNTIME_TEST=1")


@pytest.mark.parametrize("seed", [1, 2, 19])
@pytest.mark.parametrize("every", [0, 2])
def test_extract_matches_go(tmp_path, seed, every):
    pcap = tmp_path / "flows.pcap"
    write_pcap(pcap, synthetic_capture(seed))
    results = []
    for name, binary in [("go", REPO / "bin/go/aqos-extract"),
                         ("rust", REPO / "rust/aqos-runtime/target/debug/aqos-extract-rs")]:
        out, keys = tmp_path / f"{name}.jsonl", tmp_path / f"{name}.keys.jsonl"
        subprocess.run([str(binary), "-pcap", str(pcap), "-out", str(out), "-keys", str(keys),
                        "-key", KEY, "-window-every", str(every)], check=True, capture_output=True)
        records = [json.loads(s) for s in out.read_text().splitlines()]
        # Hash-table iteration order is deliberately not part of the protocol.
        records.sort(key=lambda r: (r["ts_ns"], r["flow_id"], r["kind"]))
        mapping = sorted([json.loads(s) for s in keys.read_text().splitlines()], key=lambda r: r["flow_id"])
        results.append((records, mapping))
    assert results[0][1] == results[1][1]
    assert len(results[0][0]) == len(results[1][0])
    for go, rust in zip(results[0][0], results[1][0]):
        gw, rw = go.pop("window"), rust.pop("window")
        assert go == rust
        assert rw == pytest.approx(gw, rel=1e-12, abs=1e-12) if gw is not None else rw is None


def compare_output(tmp_path, mode, source, every=2):
    records = []
    for name, binary in [("go", REPO / "bin/go/aqos-extract"),
                         ("rust", REPO / "rust/aqos-runtime/target/debug/aqos-extract-rs")]:
        out = tmp_path / f"{name}.jsonl"
        subprocess.run([str(binary), mode, str(source), "-out", str(out), "-key", KEY,
                        "-window-every", str(every)], check=True, capture_output=True)
        records.append(sorted([json.loads(s) for s in out.read_text().splitlines()],
                              key=lambda r: (r["ts_ns"], r["flow_id"], r["kind"])))
    assert len(records[0]) == len(records[1]) > 0
    for go, rust in zip(*records):
        gw, rw = go.pop("window"), rust.pop("window")
        assert go == rust
        assert rw == pytest.approx(gw, rel=1e-12, abs=1e-12) if gw is not None else rw is None


@pytest.mark.parametrize("compressed", [False, True])
def test_public_flow_tables_match_go(tmp_path, compressed):
    rows = []
    for i in range(12):
        rows.append(dict(source="test", file=f"{i}.pcap", folder="capture", protocol=6 if i % 2 else 17,
                         ip_version=6 if i % 3 else 4, src_port=40000 + i, dst_port=443,
                         src_private=i % 2 == 0, dst_private=i % 2 == 1, start_s=10.25,
                         duration_s=12.7, packets=500, bytes=300000, oversize=1 if i == 3 else 0,
                         from_start=i != 4, first=[[n * 30000, 1500 if n % 2 else -60] for n in range(8)],
                         seconds=[[n, 10+n, 3+n, (10+n)*1000, (3+n)*80] for n in range(13)]))
    rows[5]["first"] = rows[5]["first"][:3]
    rows[6]["first"][0][1] = 9000
    path = tmp_path / ("flows.jsonl.gz" if compressed else "flows.jsonl")
    data = "".join(json.dumps(r) + "\n" for r in rows).encode()
    path.write_bytes(gzip.compress(data) if compressed else data)
    compare_output(tmp_path, "-flow-table", path)


@pytest.mark.parametrize("endian", ["<", ">"])
def test_pcapng_endian_and_timestamp_resolution(tmp_path, endian):
    def block(kind, body):
        return struct.pack(endian + "II", kind, len(body) + 12) + body + struct.pack(endian + "I", len(body) + 12)
    data = block(0x0A0D0D0A, struct.pack(endian + "IHHq", 0x1A2B3C4D, 1, 0, -1))
    data += block(1, struct.pack(endian + "HHIHH4sHH", 1, 0, 128, 9, 1, b"\x09\0\0\0", 0, 0))
    for n in range(8):
        packet = frame("10.0.0.1", "203.0.113.1", 17, 40000, 443, 100)
        ns = 10_000_000_000 + n * 1_234_000
        header = struct.pack(endian + "IIIII", 0, ns >> 32, ns & 0xffffffff, len(packet), len(packet))
        data += block(6, header + packet + bytes(-len(packet) % 4))
    path = tmp_path / "capture.pcapng"
    path.write_bytes(data)
    compare_output(tmp_path, "-pcap", path, 0)


def test_mapped_ipv6_record_version_matches_go(tmp_path):
    path = tmp_path / "mapped.pcap"
    packets = [(10 + n * .001, frame("::ffff:10.0.0.1", "::ffff:203.0.113.1", 17, 30000, 443, 100))
               for n in range(8)]
    write_pcap(path, packets)
    compare_output(tmp_path, "-pcap", path, 0)


def test_exporter_serves_metrics_while_another_client_is_slow(tmp_path):
    import socket
    import time
    import urllib.request
    from contextlib import closing

    with closing(socket.socket()) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"metric":"queue_delay_ms","value":2.5,"tin":"voice","run_id":"first"}\n'
                    '{"metric":"latency_ms","value":23.4,"host":"laptop"}\n')
    binary = REPO / "rust/aqos-runtime/target/debug/aqos-exporter-rs"
    proc = subprocess.Popen([str(binary), "-file", str(path), "-listen", f"127.0.0.1:{port}"],
                            stderr=subprocess.PIPE, text=True)
    slow = None
    try:
        deadline = time.monotonic() + 3
        while True:
            try:
                slow = socket.create_connection(("127.0.0.1", port), timeout=.2)
                break
            except OSError:
                assert proc.poll() is None
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        slow.sendall(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n")
        def scrape():
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=1) as response:
                assert response.headers["Content-Type"] == "text/plain; version=0.0.4"
                return response.read().decode()
        text = scrape()
        assert 'aqos_queue_delay_ms{tin="voice"} 2.5\n' in text
        assert 'aqos_latency_ms_bucket{host="laptop",le="30"} 1\n' in text
        # Rotate atomically, as the current run symlink does; old series disappear.
        new = tmp_path / "next"
        new.write_text('{"metric":"cpu_s","value":3,"run_id":"second"}\n')
        new.replace(path)
        deadline = time.monotonic() + 2
        while 'run_id="second"' not in (text := scrape()) and time.monotonic() < deadline:
            time.sleep(.02)
        assert 'run_id="second"' in text
        assert 'aqos_cpu_s 3\n' in text
        assert "queue_delay_ms" not in text
    finally:
        if slow:
            slow.close()
        proc.terminate()
        _, error = proc.communicate(timeout=3)
        assert proc.returncode == 0, error

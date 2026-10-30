import datetime
import ipaddress
import json
import struct

import pytest

from aqos_ml import capture


def ether(payload, ethertype):
    return bytes(12) + struct.pack("!H", ethertype) + payload


def ipv4(src, dst, protocol, sport, dport, length):
    head = struct.pack("!BBHHHBBH4s4s", 0x45, 0, length, 0, 0, 64, protocol, 0,
                       ipaddress.ip_address(src).packed, ipaddress.ip_address(dst).packed)
    return ether(head + struct.pack("!HH", sport, dport) + bytes(40), 0x0800)


def ipv6(src, dst, protocol, sport, dport, length, flags=0x10):
    head = struct.pack("!IHBB16s16s", 6 << 28, length - 40, protocol, 64,
                       ipaddress.ip_address(src).packed, ipaddress.ip_address(dst).packed)
    tcp = struct.pack("!HHIIBB", sport, dport, 0, 0, 0x50, flags)
    return ether(head + tcp + bytes(40), 0x86DD)


def write_pcap(path, packets):
    """packets: (time, frame, original L3 length)."""
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, capture.SNAPLEN, 1)
    for t, frame, l3 in packets:
        frame = frame[:capture.SNAPLEN]
        out += struct.pack("<IIII", int(t), int(t % 1 * 1e6), len(frame), l3 + 14) + frame
    path.write_bytes(out)


@pytest.fixture
def session(tmp_path):
    # The call starts 2 s into the capture; the sync connection opens with
    # a SYN. Both are therefore seen from their first packet.
    call = [(2 + i * 0.02, ipv4("192.168.1.5", "142.250.1.1", 17, 50000, 3478, 1100), 1100) for i in range(40)]
    back = [(2 + i * 0.02 + 0.01, ipv4("142.250.1.1", "192.168.1.5", 17, 3478, 50000, 900), 900) for i in range(30)]
    dns = [(0.5, ipv4("192.168.1.5", "192.168.1.1", 17, 40000, 53, 70), 70)]
    sync = [(1 + i * 0.1, ipv6("fd00::5", "2001:db8::1", 6, 51000, 443, 200, flags=0x02 if i == 0 else 0x10), 200)
            for i in range(10)]
    arp = [(0.1, ether(bytes(28), 0x0806), 28)]
    meta = capture.new_session("video_call", "Google Meet", "Laptop #1", "home-wifi", "wlp2s0",
                               now=datetime.datetime(2026, 10, 10, 15, 0, tzinfo=datetime.timezone.utc))
    write_pcap(tmp_path / meta["pcap"], sorted(call + back + dns + sync + arp, key=lambda p: p[0]))
    path = tmp_path / (meta["session"] + ".json")
    path.write_text(json.dumps(meta))
    return path


def test_new_session_carries_its_label():
    meta = capture.new_session("cloud_gaming", "GeForce NOW", "desktop", "home", "eth0",
                               now=datetime.datetime(2026, 10, 10, 15, 0, tzinfo=datetime.timezone.utc))
    assert meta["session"] == "20261010-150000-cloud-gaming-geforce-now-desktop"
    assert meta["label"] == "realtime_interactive"
    assert meta["snaplen"] == 128 and meta["accepted"] is False and meta["notes"] == ""
    noted = capture.new_session("cloud_gaming", "JioGamesCloud", "laptop", "home", "wlan0", notes="racing")
    assert noted["notes"] == "racing"
    with pytest.raises(ValueError):
        capture.new_session("netflix", "x", "y", "z", "eth0")
    assert "flow_storm" not in capture.ACTIVITIES


def test_capture_is_headers_only():
    cmd = capture.tcpdump_command("wlp2s0", "x.pcap", seconds=60)
    assert cmd[cmd.index("-s") + 1] == "128"
    assert cmd[:3] == ["timeout", "--signal=INT", "60"]


def test_flows_are_grouped_both_ways(session):
    flows = capture.read_flows(session.with_suffix(".pcap"))
    assert [(f.protocol, f.a, f.b, f.packets, f.bytes) for f in flows] == [
        (17, "192.168.1.5:50000", "142.250.1.1:3478", 70, 40 * 1100 + 30 * 900),
        (6, "fd00::5:51000", "2001:db8::1:443", 10, 2000),
        (17, "192.168.1.5:40000", "192.168.1.1:53", 1, 70),
    ]
    assert flows[0].last_s - flows[0].first_s == pytest.approx(0.78)


def test_review_points_at_background_flows(session, capsys):
    assert capture.main(["review", str(session)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "video_call (multimedia_conferencing)" in lines[0]
    assert "background?" not in lines[2]
    assert "under 8 packets, never used" in lines[4]


def test_accept_records_dropped_flows(session, capsys):
    assert capture.main(["accept", str(session), "--drop", "9"]) == 1
    assert capture.main(["accept", str(session), "--keep", "0"]) == 0
    assert len(json.loads(session.read_text())["dropped_flows"]) == 2
    assert capture.main(["accept", str(session), "--drop", "1", "--notes", "cloud sync ran"]) == 0
    meta = json.loads(session.read_text())
    assert meta["accepted"] is True
    assert meta["dropped_flows"] == [{"protocol": 6, "a": "fd00::5:51000", "b": "2001:db8::1:443"}]
    assert "1 flows labelled multimedia_conferencing, 1 dropped" in capsys.readouterr().out
    capture.main(["review", str(session)])
    assert "DROPPED" in capsys.readouterr().out


def test_pcapng_is_refused(tmp_path):
    (tmp_path / "x.pcap").write_bytes(b"\x0a\x0d\x0d\x0a" + bytes(40))
    with pytest.raises(ValueError, match="pcapng"):
        capture.read_flows(tmp_path / "x.pcap")


def test_flows_the_capture_missed_the_start_of_are_refused(tmp_path, capsys):
    """Recording after the activity has started loses the first 8 packets,
    and receive offload merges packets. Neither may reach the dataset."""
    running = [(i * 0.02, ipv4("192.168.1.5", "142.250.1.1", 17, 50000, 3478, 1100), 1100) for i in range(20)]
    no_syn = [(3 + i * 0.1, ipv6("fd00::5", "2001:db8::1", 6, 51000, 443, 200), 200) for i in range(10)]
    merged = [(5 + i * 0.1, ipv4("192.168.1.5", "10.0.0.9", 17, 50001, 443, 1200 if i else 9000), 1200 if i else 9000)
              for i in range(10)]
    fresh = [(6 + i * 0.1, ipv4("192.168.1.5", "10.0.0.8", 17, 50002, 443, 300), 300) for i in range(10)]
    meta = capture.new_session("video_call", "Meet", "laptop", "wifi", "wlp2s0")
    write_pcap(tmp_path / meta["pcap"], sorted(running + no_syn + merged + fresh, key=lambda p: p[0]))
    path = tmp_path / (meta["session"] + ".json")
    path.write_text(json.dumps(meta))

    flows = capture.read_flows(tmp_path / meta["pcap"])
    assert [(f.from_its_start, f.oversize) for f in flows] == [(False, 0), (True, 1), (True, 0), (False, 0)]
    capture.main(["review", str(path)])
    out = capsys.readouterr().out
    assert out.count("began before the capture") == 2
    assert "1 packets over 1500 bytes" in out

    assert capture.main(["accept", str(path)]) == 1
    assert "flows 0,1,3 are UNUSABLE" in capsys.readouterr().err
    assert json.loads(path.read_text())["accepted"] is False
    assert capture.main(["accept", str(path), "--keep", "2"]) == 0
    assert capture.main(["accept", str(path), "--force"]) == 0

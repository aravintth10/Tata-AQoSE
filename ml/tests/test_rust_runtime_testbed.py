"""Real-kernel checks for the staged Rust runtime. Owns the testbed lock."""
import json
import os
import subprocess
import time

import pytest

from harness import REPO
from aqos_ml.run.runner import claim_testbed
from test_extract import KEY

pytestmark = pytest.mark.skipif(not (os.environ.get("AQOS_RUNTIME_INTEGRATION") or os.environ.get("AQOS_INTEGRATION")),
                                reason="set AQOS_RUNTIME_INTEGRATION=1")
TB = REPO / "bin/aqos-testbed"
RUST = REPO / "rust/aqos-runtime/target/debug"


def run(*args, **kw):
    check = kw.pop("check", True)
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                            timeout=30, **kw)
    if check and result.returncode:
        print(result.stderr)
        result.check_returncode()
    return result


def router(*args, **kw):
    return run(TB, "exec", "router", "--", *args, **kw)


def ctl(*args, **kw):
    return router(RUST / "aqos-ctl-rs", *args, **kw)


@pytest.fixture(scope="module")
def network():
    lock = claim_testbed()
    try:
        run(TB, "up", REPO / "scenarios/S1.yaml")
        router("sh", REPO / "configs/baselines/B2.sh", "100000", "20000")
        yield
    finally:
        run(TB, "down")
        lock.close()


def test_marking_rates_restore_and_partial_cleanup(network):
    assert ctl("marking", "install").stdout.strip() == "installed"
    assert ctl("rate", "up", "18000").stdout.strip() == "18000"
    with pytest.raises(subprocess.CalledProcessError) as fault:
        ctl("rate", "up", "17000", env={**os.environ, "AQOS_ENFORCE_FAULT": "rate"})
    assert fault.value.returncode == 4
    assert ctl("rate", "up").stdout.strip() == "18000"
    # Missing one component must not let cleanup claim the others are absent.
    router("nft", "delete", "table", "inet", "aqos_mark")
    assert ctl("marking", "remove").stdout.strip() == "not installed"
    for dev in ["wan0", "ifb0"]:
        q = json.loads(router("tc", "-j", "qdisc", "show", "dev", dev).stdout)
        cake = next(v for v in q if v["kind"] == "cake")
        assert int(cake["options"].get("fwmark", "0"), 0) == 0
    assert ctl("marking", "remove").stdout.strip() == "not installed"
    run(TB, "exec", "host-laptop", "--", "ping", "-c", "1", "-W", "2", "10.78.0.10")


@pytest.mark.parametrize("v6", [False, True])
def test_live_capture_labels_and_replay(network, tmp_path, v6):
    port = 7092 if v6 else 7091
    addr = "fd77:0:0:4::10" if v6 else "10.78.0.10"
    # Use the server's actual address rather than assuming topology's IPv6 prefix.
    if v6:
        links = json.loads(run(TB, "exec", "server", "--", "ip", "-j", "addr").stdout)
        addr = next(a["local"] for d in links for a in d["addr_info"]
                    if a["family"] == "inet6" and a["scope"] == "global")
    server = ("import socket; s=socket.socket(socket.AF_INET6 if " + repr(v6) +
              " else socket.AF_INET,socket.SOCK_DGRAM); s.bind(('::' if " + repr(v6) +
              " else '0.0.0.0'," + str(port) + "));\nwhile True:\n d,a=s.recvfrom(2048); s.sendto(d,a)\n")
    proc = subprocess.Popen([str(TB), "exec", "server", "--", "python3", "-c", server])
    out, keys, pcap = [tmp_path / x for x in ["live.jsonl", "keys.jsonl", "headers.pcap"]]
    extract = None
    try:
        ctl("marking", "install")
        router("nft", "add table inet test_foreign; add chain inet test_foreign forward { type filter hook forward priority -50; }; add rule inet test_foreign forward ct mark set ct mark | 0x100")
        extract = subprocess.Popen([str(TB), "exec", "router", "--", str(RUST / "aqos-extract-rs"),
                                    "-live", "-out", str(out), "-keys", str(keys), "-dump", str(pcap),
                                    "-key", KEY, "-window-every", "1", "-until-s", "6"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.4)
        sender = ("import socket,time; s=socket.socket(socket.AF_INET6 if " + repr(v6) +
                  " else socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(2); s.connect((" + repr(addr) +
                  "," + str(port) + "));\nfor i in range(35):\n s.send(b'PRIVATE_PAYLOAD'*8); s.recv(2048); time.sleep(.1)\n")
        run(TB, "exec", "host-laptop", "--", "python3", "-c", sender)
        _, err = extract.communicate(timeout=10)
        assert extract.returncode == 0, err
        records = [json.loads(s) for s in out.read_text().splitlines()]
        initial = [r for r in records if r["kind"] == "initial"]
        assert len(initial) == 1
        assert any(r["kind"] == "window" for r in records)
        assert b"PRIVATE_PAYLOAD" not in pcap.read_bytes()
        offline = tmp_path / "offline.jsonl"
        run(REPO / "bin/go/aqos-extract", "-pcap", pcap, "-out", offline, "-key", KEY)
        replay = json.loads(offline.read_text())
        for field in ["flow_id", "host_id", "pkt_sizes", "pkt_gaps_us", "ip_version", "protocol"]:
            assert initial[0][field] == replay[field]
        key = json.loads(keys.read_text())
        endpoint = lambda ip, port: f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
        label = ["label", "udp", endpoint(key["src_ip"], key["src_port"]),
                 endpoint(key["dst_ip"], key["dst_port"])]
        assert ctl(*label, "voice").stdout.strip() == "voice"
        assert "mark 260 " in ctl("flows").stdout
        with pytest.raises(subprocess.CalledProcessError) as fault:
            ctl(*label, "video", env={**os.environ, "AQOS_ENFORCE_FAULT": "label"})
        assert fault.value.returncode == 4
        assert ctl(*label).stdout.strip() == "voice"
        assert "mark 260 " in ctl("flows").stdout
        # Restart clears stale owned bits without discarding foreign bits.
        ctl("marking", "install")
        assert ctl(*label).stdout.strip() == "none"
        assert "mark 256 " in ctl("flows").stdout
    finally:
        if extract and extract.poll() is None:
            extract.terminate()
            extract.communicate(timeout=5)
        proc.terminate()
        proc.wait(timeout=5)
        router("nft", "delete", "table", "inet", "test_foreign", check=False)
        ctl("marking", "remove")


@pytest.mark.parametrize("heartbeat_started", [False, True])
def test_independent_watchdog_restores_verified_b2(network, tmp_path, heartbeat_started):
    """Both a dead controller and one that never started must recover."""
    heartbeat = tmp_path / "heartbeat"
    safe = tmp_path / "heartbeat.safe"
    if heartbeat_started:
        heartbeat.write_text("alive\n")
    config = tmp_path / "watchdog.json"
    config.write_text(json.dumps(dict(heartbeat=str(heartbeat), timeout_ms=1000,
                                     safe_up_kbit=19000, safe_down_kbit=95000)))
    ctl("marking", "install")
    ctl("rate", "up", "10000")
    start = time.monotonic()
    proc = subprocess.Popen([str(TB), "exec", "router", "--", str(REPO / "bin/aqos-watchdog-rs"),
                             "-config", str(config)], stderr=subprocess.PIPE, text=True)
    try:
        while not safe.exists() and time.monotonic() - start < 3:
            assert proc.poll() is None
            time.sleep(.02)
        assert safe.exists(), "safe recovery exceeded 3 seconds"
        assert ctl("marking", "status").stdout.strip() == "not installed"
        assert ctl("rate", "up").stdout.strip() == "19000"
        assert ctl("rate", "down").stdout.strip() == "95000"
        for dev in ["wan0", "ifb0"]:
            qdiscs = json.loads(router("tc", "-j", "qdisc", "show", "dev", dev).stdout)
            cake = next(q for q in qdiscs if q["kind"] == "cake")
            assert int(str(cake["options"].get("fwmark", "0")), 0) == 0
        run(TB, "exec", "host-laptop", "--", "ping", "-c", "1", "-W", "2", "10.78.0.10")
        heartbeat.write_text("resumed\n")
        deadline = time.monotonic() + .8
        while safe.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert not safe.exists()
    finally:
        proc.terminate()
        _, err = proc.communicate(timeout=5)
        assert proc.returncode == 0, err


def test_watchdog_retries_failed_recovery(network, tmp_path):
    """A failed command cannot publish success or disable later retries."""
    import shutil
    heartbeat, allowed = tmp_path / "heartbeat", tmp_path / "allow"
    safe = tmp_path / "heartbeat.safe"
    commands = tmp_path / "commands"
    commands.mkdir()
    wrapper = commands / "tc"
    wrapper.write_text(f'#!/bin/sh\n[ -f "{allowed}" ] || exit 17\nexec "{shutil.which("tc")}" "$@"\n')
    wrapper.chmod(0o755)
    config = tmp_path / "watchdog.json"
    config.write_text(json.dumps(dict(heartbeat=str(heartbeat), timeout_ms=500,
                                     safe_up_kbit=19000, safe_down_kbit=95000)))
    ctl("marking", "install")
    log = tmp_path / "watchdog.log"
    with log.open("w") as stderr:
        proc = subprocess.Popen([str(TB), "exec", "router", "--", str(REPO / "bin/aqos-watchdog-rs"),
                                 "-config", str(config)], stderr=stderr,
                                env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]})
        try:
            time.sleep(1.3)
            assert proc.poll() is None
            assert not safe.exists()
            assert "recovery incomplete; retrying" in log.read_text()
            allowed.touch()
            deadline = time.monotonic() + 3
            while not safe.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert safe.exists(), log.read_text()
            assert ctl("rate", "up").stdout.strip() == "19000"
            assert ctl("marking", "status").stdout.strip() == "not installed"
        finally:
            proc.terminate()
            proc.wait(timeout=5)

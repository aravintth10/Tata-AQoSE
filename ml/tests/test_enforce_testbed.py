"""The enforcer and the watchdog, on a real router. Replaces any running
testbed, so it runs only when asked: make testbed-check."""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from harness import REPO, TESTBED

pytestmark = pytest.mark.skipif(not os.environ.get("AQOS_INTEGRATION"), reason="set AQOS_INTEGRATION=1")
CTL = REPO / "bin" / "aqos-ctl"
TOOLS = REPO / "ml" / "tests" / "tools"
HOST4, SERVER4 = "10.77.0.10", "10.78.0.10"
HOST6, SERVER6 = "fd77:0:0:1::10", "fd77:0:0:4::2"
TINS = ["bulk", "best_effort", "video", "voice"]
ECHO_PORT = 7000


def tb(*args, check=True, env=None):
    result = subprocess.run([str(a) for a in (TESTBED, *args)], capture_output=True, text=True,
                            env={**os.environ, **(env or {})})
    if check:
        assert result.returncode == 0, f"{args}: {result.stdout}{result.stderr}"
    return result


def ctl(*args, check=True, env=None):
    return tb("exec", "router", "--", CTL, *args, check=check, env=env)


def tins():
    return json.loads(ctl("tins").stdout)


@pytest.fixture
def router():
    """A router with CAKE as in B2, an echo server behind it, and marking
    installed."""
    tb("up", REPO / "scenarios/S1.yaml")
    echo = None
    try:
        tb("exec", "router", "--", REPO / "configs/baselines/B2.sh", 100000, 20000)
        assert ctl("marking", "install").stdout.strip() == "installed"
        echo = subprocess.Popen([str(TESTBED), "exec", "server", "--", sys.executable, str(TOOLS / "udp_echo.py"), str(ECHO_PORT)])
        time.sleep(0.4)
        yield
    finally:
        if echo:
            echo.kill()
        tb("down", check=False)


def flow(port, count=250, server=SERVER4, tos=None):
    """Starts a flow from the laptop and returns once its conntrack entry
    exists."""
    args = [str(TESTBED), "exec", "host-laptop", "--", sys.executable, str(TOOLS / "udp_send.py"),
            server, str(ECHO_PORT), str(port), str(count)]
    if tos is not None:
        args.append(str(tos))
    proc = subprocess.Popen(args)
    time.sleep(0.3)
    return proc


def counted(before, after):
    """Packets each tin gained, as {direction: {tin: n}}."""
    return {d: {t: after[d][t] - before[d][t] for t in TINS} for d in ("up", "down")}


def test_a_label_puts_the_flow_in_its_tin(router):
    """The marking design rests on this: a mark of n selects tin n, counting
    Bulk, Best Effort, Video, Voice from 1, for upload and download."""
    for i, tin in enumerate(TINS):
        port = 41000 + i
        proc = flow(port)
        assert ctl("label", "udp", f"{HOST4}:{port}", f"{SERVER4}:{ECHO_PORT}", tin).stdout.strip() == tin
        before = tins()
        proc.wait(10)
        gained = counted(before, tins())
        for direction in ("up", "down"):
            assert gained[direction][tin] > 100, (tin, gained)
            others = sum(n for t, n in gained[direction].items() if t != tin)
            assert others <= 3, f"{tin} flow leaked into other tins {direction}: {gained[direction]}"


def test_ipv6_flows_are_labelled_too(router):
    port = 42000
    proc = flow(port, server=SERVER6)
    ctl("label", "udp", f"[{HOST6}]:{port}", f"[{SERVER6}]:{ECHO_PORT}", "voice")
    before = tins()
    proc.wait(10)
    gained = counted(before, tins())
    assert gained["up"]["voice"] > 100 and gained["down"]["voice"] > 100, gained


def test_an_unlabelled_flow_is_plain_cake(router):
    """Mark 0 falls through to CAKE's own DSCP handling: default traffic in
    Best Effort, EF in Voice, with marking installed and nothing labelled."""
    before = tins()
    flow(43000).wait(10)
    mid = tins()
    flow(43001, tos=0xB8).wait(10)  # DSCP EF
    assert counted(before, mid)["up"]["best_effort"] > 200
    assert counted(mid, tins())["up"]["voice"] > 200


def test_removing_marking_turns_the_system_off(router):
    port = 44000
    proc = flow(port, count=600)
    endpoints = (f"{HOST4}:{port}", f"{SERVER4}:{ECHO_PORT}")
    ctl("label", "udp", *endpoints, "voice")
    assert ctl("marking", "remove").stdout.strip() == "not installed"
    before = tins()
    time.sleep(1)
    gained = counted(before, tins())
    proc.kill()
    # The mark is still there, and no longer read.
    assert ctl("label", "udp", *endpoints).stdout.strip() == "voice"
    for direction in ("up", "down"):
        assert gained[direction]["best_effort"] > 20 and gained[direction]["voice"] == 0, gained


def test_relabelling_and_removing_a_label(router):
    port = 45000
    proc = flow(port, count=600)
    endpoints = (f"{HOST4}:{port}", f"{SERVER4}:{ECHO_PORT}")
    assert ctl("label", "udp", *endpoints).stdout.strip() == "none"
    for tin in ("voice", "bulk", "none"):
        assert ctl("label", "udp", *endpoints, tin).stdout.strip() == tin
    proc.kill()


def test_a_flow_that_has_ended_is_not_an_error(router):
    result = ctl("label", "udp", f"{HOST4}:46000", f"{SERVER4}:{ECHO_PORT}", "voice", check=False)
    assert result.returncode == 3 and "ended" in result.stderr


def test_rates_are_read_back(router):
    assert ctl("rate", "down").stdout.strip() == "95000"
    assert ctl("rate", "down", "50000").stdout.strip() == "50000"
    assert ctl("rate", "up", "7000").stdout.strip() == "7000"
    # Changing the rate must not disturb marking.
    assert ctl("marking", "status").stdout.strip() == "installed"
    assert ctl("rate", "down", "0", check=False).returncode == 1


def test_a_corrupted_apply_is_detected_and_restored(router):
    """Something changes the state between apply and read-back. The
    enforcer must notice, put the old value back, and say so."""
    ctl("rate", "down", "60000")
    result = ctl("rate", "down", "80000", check=False, env={"AQOS_ENFORCE_FAULT": "rate"})
    assert result.returncode == 4 and "restored" in result.stderr
    assert ctl("rate", "down").stdout.strip() == "60000"

    port = 47000
    proc = flow(port, count=600)
    endpoints = (f"{HOST4}:{port}", f"{SERVER4}:{ECHO_PORT}")
    ctl("label", "udp", *endpoints, "video")
    result = ctl("label", "udp", *endpoints, "voice", check=False, env={"AQOS_ENFORCE_FAULT": "label"})
    assert result.returncode == 4 and "restored" in result.stderr
    assert ctl("label", "udp", *endpoints).stdout.strip() == "video"
    proc.kill()


SAFE_UP, SAFE_DOWN = 15000, 70000
TRIALS = 10  # per way of dying


def test_watchdog_reaches_the_safe_state_within_three_seconds(router, tmp_path):
    """Chunk 4D's done-when: the controller is killed, and separately
    frozen, and each time the router is back to plain CAKE inside 3 s."""
    heartbeat = tmp_path / "heartbeat"
    config = tmp_path / "watchdog.json"
    config.write_text(json.dumps({"heartbeat": str(heartbeat), "safe_up_kbit": SAFE_UP, "safe_down_kbit": SAFE_DOWN}))
    watchdog = subprocess.Popen([str(TESTBED), "exec", "router", "--", str(REPO / "bin/aqos-watchdog"), "-config", str(config)],
                                stderr=subprocess.PIPE, text=True)
    safe_file = tmp_path / "heartbeat.safe"
    took = {"SIGKILL": [], "SIGSTOP": []}
    try:
        for trial in range(2 * TRIALS):
            how = "SIGKILL" if trial % 2 == 0 else "SIGSTOP"
            # A working controller: marking on, rates it chose itself.
            ctl("marking", "install")
            ctl("rate", "down", "90000")
            ctl("rate", "up", "18000")
            safe_file.unlink(missing_ok=True)
            # exec, so the signal reaches the beater itself.
            beater = subprocess.Popen([str(CTL), "heartbeat", str(heartbeat)])
            time.sleep(0.8)
            assert not safe_file.exists(), "the watchdog fired while the controller was alive"
            assert ctl("marking", "status").stdout.strip() == "installed"

            killed_ns = time.time_ns()
            beater.send_signal(getattr(signal, how))
            deadline = time.monotonic() + 5
            while not safe_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert safe_file.exists(), f"{how}: no safe state within 5 s"
            took[how].append((int(safe_file.read_text()) - killed_ns) / 1e9)

            assert ctl("marking", "status").stdout.strip() == "not installed"
            assert ctl("rate", "up").stdout.strip() == str(SAFE_UP)
            assert ctl("rate", "down").stdout.strip() == str(SAFE_DOWN)
            beater.kill()
            beater.wait()
    finally:
        watchdog.terminate()
        log = watchdog.communicate(timeout=5)[1]
    for how, times in took.items():
        print(f"\n{how}: safe state after {min(times):.2f} to {max(times):.2f} s over {len(times)} trials")
        assert max(times) < 3.0, (how, times)
        # Never before the timeout: it must not fire on a short stall.
        assert min(times) > 1.9, (how, times)
    assert log.count("safe state applied") == 2 * TRIALS
    # After the safe state the router still forwards.
    tb("exec", "host-laptop", "--", "ping", "-c", "1", "-W", "1", SERVER4)

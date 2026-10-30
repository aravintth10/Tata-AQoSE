"""Run clock, metric and flow logs, and the per-generator summary."""

import json
import os
import threading
import time
from pathlib import Path

GRACE_S = 1.0  # how long a receiver waits for late packets after the stop time


class RunClock:
    """Seconds since the start of the run, on the clock all namespaces share."""

    def __init__(self, t0_ns):
        self.t0_ns = t0_ns

    def now_s(self):
        return (time.monotonic_ns() - self.t0_ns) / 1e9

    def ns_at(self, t_s):
        return self.t0_ns + int(t_s * 1e9)

    def sleep_until(self, t_s, stop=None):
        """Returns False if stop was set first."""
        while True:
            left = t_s - self.now_s()
            if left <= 0:
                return True
            if stop is None:
                time.sleep(left)
            elif stop.wait(min(left, 0.2)):
                return False


class _AppendFile:
    """Whole lines appended with one write each, so several processes can
    share a file."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        self._lock = threading.Lock()
        self._lines = []

    def add(self, line):
        with self._lock:
            self._lines.append(line)
            if len(self._lines) >= 2000:
                self._flush()

    def flush(self):
        with self._lock:
            self._flush()

    def _flush(self):
        if self._lines:
            os.write(self._fd, ("\n".join(self._lines) + "\n").encode())
            self._lines = []

    def close(self):
        self.flush()
        os.close(self._fd)


class MetricWriter(_AppendFile):
    """Writes metric samples for one traffic entry to metrics.jsonl."""

    def __init__(self, path, run_id, plan):
        super().__init__(path)
        labels = {"host": plan.host, "traffic_id": plan.traffic_id,
                  "profile": plan.profile, "true_class": plan.true_class}
        self._head = '{"run_id": %s, "t_s": ' % json.dumps(run_id)
        self._labels = ", " + json.dumps(labels)[1:-1]
        self.count = 0

    def emit(self, metric, value, t_s, direction=None):
        line = '%s%.6f, "source": "generator", "metric": "%s", "value": %.6g%s' % (
            self._head, max(t_s, 0.0), metric, value, self._labels)
        self.add(line + (', "direction": "%s"}' % direction if direction else "}"))
        self.count += 1


class FlowLog(_AppendFile):
    """Ground truth for the dataset builder: which flows were which traffic.

    Addresses are as the client sees them, which is what the router's LAN
    side sees. This file stays in the run directory.
    """

    def __init__(self, path, run_id, plan, clock):
        super().__init__(path)
        self._base = {"run_id": run_id, "traffic_id": plan.traffic_id,
                      "profile": plan.profile, "true_class": plan.true_class, "host": plan.host}
        self._clock = clock
        self.count = 0

    def record(self, sock, protocol):
        src, dst = sock.getsockname(), sock.getpeername()
        self.add(json.dumps({
            **self._base, "t_s": round(max(self._clock.now_s(), 0.0), 6),
            "ip_version": 6 if ":" in src[0] else 4, "protocol": protocol,
            "src_ip": src[0], "src_port": src[1], "dst_ip": dst[0], "dst_port": dst[1],
        }))
        self.count += 1


class Counter:
    """A byte counter several threads add to and a ticker drains."""

    def __init__(self):
        self._n = 0
        self._lock = threading.Lock()

    def add(self, n):
        with self._lock:
            self._n += n

    def take(self):
        with self._lock:
            n, self._n = self._n, 0
        return n


class Ticker(threading.Thread):
    """Calls fn(k) at each whole run second k in (first_s, last_s]. A call
    covers the second that just ended."""

    def __init__(self, clock, first_s, last_s, fn):
        super().__init__(daemon=True)
        self._clock, self._fn = clock, fn
        self._next = int(first_s) + 1
        self._last = last_s
        self._halt = threading.Event()

    def run(self):
        while self._next <= self._last and self._clock.sleep_until(self._next, self._halt):
            self._fn(self._next)
            self._next += 1

    def finish(self):
        """Stops ticking and emits the seconds that are already over."""
        self._halt.set()
        self.join()
        while self._next <= min(self._last, self._clock.now_s()):
            self._fn(self._next)
            self._next += 1


def summary_path(out_dir, traffic_id, role):
    return Path(out_dir) / "gen" / f"{traffic_id}.{role}.json"


def write_summary(out_dir, traffic_id, role, summary):
    """Written last. A generator that died leaves no summary, which is how
    the runner knows the run is invalid."""
    path = summary_path(out_dir, traffic_id, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, indent=2) + "\n")
    tmp.rename(path)

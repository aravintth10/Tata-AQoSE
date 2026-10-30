"""UDP media profiles: voice_call, video_call, gaming, cloud_gaming.

Both ends run the same MediaFlow. Each sends its half of the plan and
measures the half it receives.
"""

import socket
import threading
import time

import numpy as np

from . import wire
from .metrics import GRACE_S, Ticker
from .plan import MAIN


class _Stream:
    def __init__(self):
        self.max_seq = -1
        self.seq_at_tick = -1
        self.transit_ns = None
        self.jitter_ns = 0.0


class MediaFlow:
    def __init__(self, plan, role, sock, clock, writer, peer=None):
        self.plan, self.clock, self.writer, self.sock = plan, clock, writer, sock
        self.is_server = role == "server"
        # The server learns where to send from the first datagram it gets,
        # as it would behind any NAT.
        self.peer = peer
        self.outgoing = plan.schedules["down" if self.is_server else "up"]
        incoming = plan.schedules["up" if self.is_server else "down"]
        self.direction = "up" if self.is_server else "down"
        self.stop = threading.Event()

        # Receive side.
        self.streams = {}
        self.received = 0  # written by the receiver only
        self.rx_bytes = 0
        self.seen_at_tick = (0, 0)  # written by the ticker only
        self.contact = False
        self.frame_contact = False
        seconds = np.floor(plan.start_s + incoming.t_s).astype(int)
        self.due_per_s = np.bincount(seconds, minlength=int(plan.stop_s) + 2)

        # Frames: a frame misses when its last packet arrives after the
        # frame's scheduled send time plus the deadline, or never.
        self.deadline_ns = int(plan.params["deadline_ms"] * 1e6)
        main = incoming.stream == MAIN
        first = main & (incoming.index == 0)
        self.frame_due_ns = (clock.ns_at(plan.start_s) + (incoming.t_s[first] * 1e9).astype(np.int64)
                             + self.deadline_ns).tolist()
        self.frame_count = incoming.count[first].tolist()
        self.frame_got = [0] * len(self.frame_count)
        self.frame_low = 0
        self.frame_miss = 0
        self.frames_missed = 0
        self.frame_lock = threading.Lock()

        # Send side.
        self.sent = 0
        self.skipped = 0
        self.send_errors = 0
        self.late_ns = []

    def run(self):
        """Blocks from the entry's start to its stop, then returns a summary."""
        plan = self.plan
        receiver = threading.Thread(target=self._receive, daemon=True)
        receiver.start()
        self.clock.sleep_until(plan.start_s)
        ticker = Ticker(self.clock, plan.start_s, plan.stop_s + GRACE_S, self._tick)
        ticker.start()
        self._send()
        self.clock.sleep_until(plan.stop_s + GRACE_S)
        self.stop.set()
        receiver.join()
        ticker.finish()

        late = np.array(self.late_ns or [0]) / 1000
        return {
            "sent": self.sent, "received": self.received,
            "skipped_before_contact": self.skipped, "send_errors": self.send_errors,
            "frames_missed": self.frames_missed,
            "send_late_us": {"mean": round(float(late.mean()), 1), "p99": round(float(np.percentile(late, 99)), 1),
                             "max": round(float(late.max()), 1)},
        }

    def _send(self):
        s = self.outgoing
        base_ns = self.clock.ns_at(self.plan.start_s)
        due = (base_ns + (s.t_s * 1e9).astype(np.int64)).tolist()
        sizes, streams = s.size.tolist(), s.stream.tolist()
        frames, indexes, counts = s.frame.tolist(), s.index.tolist(), s.count.tolist()
        buf = bytearray(max(sizes))
        view = memoryview(buf)
        seq = [0, 0]
        now_ns, sleep, late = time.monotonic_ns, time.sleep, self.late_ns
        send = self.sock.sendto if self.is_server else None

        started = False
        for i, target in enumerate(due):
            now = now_ns()
            if target > now:
                sleep((target - now) / 1e9)
                now = now_ns()
            if not started:
                # Start on a frame boundary, or the far end would get the
                # tail of a frame it can never complete.
                if self.peer is None or indexes[i] != 0:
                    self.skipped += 1
                    continue
                started = True
            st = streams[i]
            wire.pack_into(buf, st, seq[st], now, frames[i], indexes[i], counts[i])
            try:
                if send:
                    send(view[:sizes[i]], self.peer)
                else:
                    self.sock.send(view[:sizes[i]])
            except OSError:
                # A full socket buffer or an ICMP error from the far end.
                # The packet is lost, as it would be on the wire.
                self.send_errors += 1
            seq[st] += 1
            self.sent += 1
            late.append(now - target)

    def _receive(self):
        sock = self.sock
        sock.settimeout(0.2)
        emit = self.writer.emit
        t0_ns, direction = self.clock.t0_ns, self.direction
        check_frames = self.deadline_ns > 0
        while not self.stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except (TimeoutError, ConnectionError):
                continue
            now = time.monotonic_ns()
            head = wire.unpack(data)
            if head is None:
                continue
            stream, seq, ts_ns, frame, _, _ = head
            if self.is_server:
                self.peer = addr
            st = self.streams.get(stream)
            if st is None:
                st = self.streams[stream] = _Stream()

            transit = now - ts_ns
            emit("latency_ms", transit / 1e6, (now - t0_ns) / 1e9, direction)
            if st.transit_ns is not None:
                st.jitter_ns += (abs(transit - st.transit_ns) - st.jitter_ns) / 16  # RFC 3550
            st.transit_ns = transit
            if seq > st.max_seq:
                st.max_seq = seq
            self.received += 1
            self.rx_bytes += len(data)

            if stream == MAIN and check_frames and frame < len(self.frame_got):
                with self.frame_lock:
                    if not self.frame_contact:
                        # Frames scheduled before the far end could reach
                        # us were never sent; do not count them.
                        self.frame_low = frame
                        self.frame_contact = True
                    if frame >= self.frame_low and self.frame_got[frame] >= 0:
                        self.frame_got[frame] += 1
                        if self.frame_got[frame] == self.frame_count[frame]:
                            self.frame_got[frame] = -1  # judged
                            if now > self.frame_due_ns[frame]:
                                self.frame_miss += 1
            self.contact = True

    def _tick(self, k):
        """Emits the per-second metrics for the run second ending at k."""
        if not self.contact:
            return
        total = (self.received, self.rx_bytes)
        received, nbytes = total[0] - self.seen_at_tick[0], total[1] - self.seen_at_tick[1]
        self.seen_at_tick = total
        expected = 0
        for st in self.streams.values():
            expected += st.max_seq - st.seq_at_tick
            st.seq_at_tick = st.max_seq
        if received == 0 and k - 1 < len(self.due_per_s) and self.due_per_s[k - 1] > 0:
            loss = 1.0  # nothing at all arrived in a second that had traffic due
        else:
            loss = min(max(1 - received / expected, 0.0), 1.0) if expected > 0 else 0.0

        emit, d = self.writer.emit, self.direction
        main = self.streams.get(MAIN)
        emit("jitter_ms", main.jitter_ns / 1e6 if main else 0.0, k, d)
        emit("loss_ratio", loss, k, d)
        emit("throughput_kbit", nbytes * 8 / 1000, k, d)
        if self.deadline_ns > 0:
            now = time.monotonic_ns()
            with self.frame_lock:
                low, got, due = self.frame_low, self.frame_got, self.frame_due_ns
                while low < len(due) and due[low] < now:
                    if got[low] >= 0:
                        got[low] = -1
                        self.frame_miss += 1
                    low += 1
                self.frame_low = low
                misses, self.frame_miss = self.frame_miss, 0
            self.frames_missed += misses
            emit("frame_miss", misses, k, d)


def run(plan, role, clock, writer, flows, server_addr):
    family = socket.AF_INET6 if plan.params["ip_version"] == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    try:
        if role == "server":
            sock.bind(("", plan.server_port))
            flow = MediaFlow(plan, role, sock, clock, writer)
        else:
            sock.bind(("", plan.client_port))
            sock.connect((server_addr, plan.server_port))
            flows.record(sock, socket.IPPROTO_UDP)
            flow = MediaFlow(plan, role, sock, clock, writer, peer=True)
        summary = flow.run()
    finally:
        sock.close()
    if summary["received"] == 0:
        raise RuntimeError("no packets received from the far end")
    return summary

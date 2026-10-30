"""TCP and mixed profiles: abr_video, web_browsing, bulk_download,
game_update, backup_upload, p2p and flow_storm.

One server serves them all. A connection opens with one JSON line:
{"op": "send", "bytes": N} asks for N bytes (any negative N: until the
connection closes) and may be repeated on the same connection;
{"op": "recv"} says the client will upload until it closes.
"""

import heapq
import json
import selectors
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import wire
from .metrics import GRACE_S, Counter, Ticker

CHUNK = bytes(64 << 10)
IO_TIMEOUT_S = 1.0
UPLOADERS = {"backup_upload", "p2p"}
DOWNLOADERS = {"abr_video", "web_browsing", "bulk_download", "game_update", "p2p", "flow_storm"}


def _family(plan):
    return socket.AF_INET6 if plan.params["ip_version"] == 6 else socket.AF_INET


class Session:
    """State shared by the threads of one generator process."""

    def __init__(self, plan, clock, writer, flows, server_addr=None):
        self.plan, self.clock, self.writer, self.flows = plan, clock, writer, flows
        self.server = (server_addr, plan.server_port)
        self.stop = threading.Event()
        self.rx = Counter()
        self.errors = []
        self.stats = {}
        self._lock = threading.Lock()
        self._socks = set()

    def count(self, key, n=1):
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def fail(self, where, exc):
        """Records an error unless it is only the stop time closing sockets."""
        if not self.stop.is_set():
            with self._lock:
                self.errors.append(f"{where}: {type(exc).__name__}: {exc}")

    def track(self, sock):
        with self._lock:
            self._socks.add(sock)
        return sock

    def close(self, sock):
        with self._lock:
            self._socks.discard(sock)
        sock.close()

    def close_all(self):
        with self._lock:
            socks, self._socks = self._socks, set()
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def connect(self):
        sock = self.track(socket.socket(_family(self.plan), socket.SOCK_STREAM))
        sock.settimeout(10)
        sock.connect(self.server)
        sock.settimeout(IO_TIMEOUT_S)
        self.flows.record(sock, socket.IPPROTO_TCP)
        self.count("flows_opened")
        return sock

    def spawn(self, fn, *args):
        def guarded():
            try:
                fn(*args)
            except Exception as exc:
                self.fail(fn.__name__, exc)

        thread = threading.Thread(target=guarded, daemon=True)
        thread.start()
        return thread

    # Building blocks, used by both ends.

    def read_exactly(self, sock, n):
        """Reads n bytes, counting them. Returns False if stopped first."""
        buf = bytearray(256 << 10)
        while n > 0 and not self.stop.is_set():
            try:
                got = sock.recv_into(buf, min(n, len(buf)))
            except TimeoutError:
                continue
            if got == 0:
                raise ConnectionError("connection closed early")
            self.rx.add(got)
            n -= got
        return n == 0

    def read_until(self, sock, t_s):
        """Reads, counting, until the run's clock passes t_s."""
        buf = bytearray(256 << 10)
        while not self.stop.is_set() and self.clock.now_s() < t_s:
            try:
                got = sock.recv_into(buf)
            except TimeoutError:
                continue
            if got == 0:
                raise ConnectionError("connection closed early")
            self.rx.add(got)

    def read_all(self, sock):
        buf = bytearray(256 << 10)
        while not self.stop.is_set():
            try:
                got = sock.recv_into(buf)
            except TimeoutError:
                continue
            if got == 0:
                return
            self.rx.add(got)

    def write(self, sock, n):
        """Writes n bytes, or until stopped when n is negative."""
        while n != 0 and not self.stop.is_set():
            chunk = CHUNK if n < 0 or n >= len(CHUNK) else CHUNK[:n]
            try:
                sent = sock.send(chunk)
            except TimeoutError:
                continue
            if n > 0:
                n -= sent


def request(sock, op, nbytes=None, pad=0):
    msg = {"op": op} if nbytes is None else {"op": op, "bytes": nbytes}
    line = json.dumps(msg)
    if pad > len(line) + 12:
        msg["pad"] = "x" * (pad - len(line) - 12)
        line = json.dumps(msg)
    sock.sendall(line.encode() + b"\n")


# Server


def _serve_tcp(s, listener):
    listener.settimeout(0.2)
    while not s.stop.is_set():
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        conn.settimeout(IO_TIMEOUT_S)
        s.spawn(_serve_conn, s, s.track(conn))


def _serve_conn(s, conn):
    try:
        pending = b""
        while not s.stop.is_set():
            while b"\n" not in pending:
                try:
                    data = conn.recv(4096)
                except TimeoutError:
                    if s.stop.is_set():
                        return
                    continue
                if not data:
                    return
                pending += data
            line, _, pending = pending.partition(b"\n")
            req = json.loads(line)
            if req["op"] == "recv":
                s.rx.add(len(pending))
                s.read_all(conn)
                return
            s.write(conn, req["bytes"])
    except (ConnectionError, OSError) as exc:
        # Clients close connections whenever they like.
        if not isinstance(exc, ConnectionError):
            s.fail("serve", exc)
    finally:
        s.close(conn)


def _serve_udp(s, sock):
    """Answers flow_storm requests and echoes p2p datagrams."""
    sock.settimeout(0.2)
    while not s.stop.is_set():
        try:
            data, addr = sock.recvfrom(2048)
        except TimeoutError:
            continue
        except OSError:
            return
        s.rx.add(len(data))
        if data[:4] == wire.STORM_MAGIC:
            data = CHUNK[:wire.STORM.unpack_from(data)[1]]
        try:
            sock.sendto(data, addr)
        except OSError:
            pass


def run_server(s):
    plan = s.plan
    listener = s.track(socket.socket(_family(plan), socket.SOCK_STREAM))
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("", plan.server_port))
    listener.listen(1024)
    s.spawn(_serve_tcp, s, listener)
    if plan.profile in ("p2p", "flow_storm"):
        udp = s.track(socket.socket(_family(plan), socket.SOCK_DGRAM))
        udp.bind(("", plan.server_port))
        s.spawn(_serve_udp, s, udp)

    def tick(k):
        if plan.profile in UPLOADERS:
            s.writer.emit("throughput_kbit", s.rx.take() * 8 / 1000, k, "up")

    s.clock.sleep_until(plan.start_s)
    ticker = Ticker(s.clock, plan.start_s, plan.stop_s, tick)
    ticker.start()
    s.clock.sleep_until(plan.stop_s + GRACE_S)
    s.stop.set()
    s.close_all()
    ticker.finish()


# Clients


def _download(s):
    sock = s.connect()
    request(sock, "send", -1)
    start = s.plan.start_s
    for low, high in s.plan.script.get("pauses", []):
        # A paused download stops reading. The connection stays open and
        # the sender stalls against a full window, as when someone presses
        # pause in a browser or a game launcher.
        s.read_until(sock, start + low)
        s.clock.sleep_until(start + high, s.stop)
    s.read_all(sock)


def _upload(s):
    sock = s.connect()
    request(sock, "recv")
    s.write(sock, -1)


def _greedy(s):
    p = s.plan.params
    downs = p.get("tcp_down", 0) if s.plan.profile == "p2p" else p.get("connections", 0)
    ups = p.get("tcp_up", 0)
    if s.plan.profile == "backup_upload":
        downs, ups = 0, p["connections"]
    for _ in range(downs):
        s.spawn(_download, s)
    for _ in range(ups):
        s.spawn(_upload, s)
    if s.plan.profile == "p2p":
        s.spawn(_p2p_udp, s)


def _p2p_udp(s):
    """Each peer is its own UDP flow; the server echoes what it gets."""
    peers = s.plan.script["peers"]
    sel = selectors.DefaultSelector()
    socks = []
    for _ in peers:
        sock = s.track(socket.socket(_family(s.plan), socket.SOCK_DGRAM))
        sock.connect(s.server)
        sock.setblocking(False)
        s.flows.record(sock, socket.IPPROTO_UDP)
        s.count("flows_opened")
        sel.register(sock, selectors.EVENT_READ)
        socks.append(sock)
    start = time.monotonic()
    queue = [(start + i / max(len(peers), 1) / p["pps"], i) for i, p in enumerate(peers)]
    heapq.heapify(queue)
    while not s.stop.is_set():
        due, i = queue[0]
        for key, _ in sel.select(max(min(due - time.monotonic(), 0.2), 0)):
            try:
                s.rx.add(len(key.fileobj.recv(2048)))
            except OSError:
                pass
        if time.monotonic() >= due:
            try:
                socks[i].send(CHUNK[:peers[i]["payload"]])
            except OSError:
                pass
            heapq.heapreplace(queue, (due + 1 / peers[i]["pps"], i))


class _Player:
    """Playback buffer of the video client."""

    def __init__(self):
        self.buffer_s = 0.0
        self.playing = False
        self.stall_s = 0.0
        self.kbit = 0
        self._at = time.monotonic()
        self.lock = threading.Lock()

    def advance(self):
        """Plays up to now. Time with an empty buffer is a stall, except
        before the first segment has arrived."""
        now = time.monotonic()
        elapsed, self._at = now - self._at, now
        if self.playing:
            played = min(self.buffer_s, elapsed)
            self.buffer_s -= played
            self.stall_s += elapsed - played


def _abr_video(s, player):
    p, script = s.plan.params, s.plan.script
    ladder, scale = script["ladder_kbit"], script["segment_scale"]
    sock = s.connect()
    segment = 0
    while not s.stop.is_set():
        with player.lock:
            player.advance()
            level = player.buffer_s
        if level > p["buffer_max_s"] - p["segment_s"]:
            s.stop.wait(0.1)
            continue
        # Buffer-based: lowest rung inside the reservoir, highest above the
        # cushion, linear in between.
        fill = min(max((level - p["reservoir_s"]) / p["cushion_s"], 0.0), 1.0)
        kbit = ladder[round(fill * (len(ladder) - 1))]
        with player.lock:
            player.kbit = kbit
        nbytes = int(kbit * 1000 / 8 * p["segment_s"] * scale[segment % len(scale)])
        request(sock, "send", nbytes)
        if not s.read_exactly(sock, nbytes):
            return
        with player.lock:
            player.advance()
            player.buffer_s += p["segment_s"]
            player.playing = True
        segment += 1
        s.count("segments")


def _fetch(s, nbytes, pad=0):
    sock = s.connect()
    try:
        request(sock, "send", nbytes, pad)
        s.read_exactly(sock, nbytes)
    finally:
        s.close(sock)


def _web_browsing(s):
    with ThreadPoolExecutor(s.plan.params["parallel"]) as pool:
        for page in s.plan.script["pages"]:
            if s.stop.wait(page["think_s"]):
                return
            started, complete = s.clock.now_s(), True
            for result in [pool.submit(_fetch, s, size) for size in page["sizes"]]:
                try:
                    result.result()
                except Exception as exc:
                    s.fail("fetch", exc)
                    complete = False
            if complete and not s.stop.is_set():
                now = s.clock.now_s()
                s.writer.emit("page_load_ms", (now - started) * 1000, now)
            s.count("pages")


def _storm_udp(s, request_size, response_size):
    sock = socket.socket(_family(s.plan), socket.SOCK_DGRAM)
    try:
        sock.settimeout(IO_TIMEOUT_S)
        sock.connect(s.server)
        s.flows.record(sock, socket.IPPROTO_UDP)
        s.count("flows_opened")
        head = wire.STORM.pack(wire.STORM_MAGIC, response_size)
        sock.send(head + CHUNK[:max(request_size - len(head), 0)])
        try:
            s.rx.add(len(sock.recv(2048)))
        except TimeoutError:
            s.count("flows_unanswered")
    finally:
        sock.close()


def _flow_storm(s):
    """Opens the scripted flows at their times. A flow that fails is
    counted, not fatal: under a storm some will."""
    def one(proto, request_size, response_size):
        try:
            if proto == "udp":
                _storm_udp(s, request_size, response_size)
            else:
                _fetch(s, response_size, pad=request_size)
        except Exception:
            if not s.stop.is_set():
                s.count("flows_failed")

    with ThreadPoolExecutor(64) as pool:
        for start, proto, request_size, response_size in s.plan.script["flows"]:
            if not s.clock.sleep_until(s.plan.start_s + start, s.stop):
                return
            late = s.clock.now_s() - (s.plan.start_s + start)
            s.stats["start_late_max_ms"] = max(s.stats.get("start_late_max_ms", 0), round(late * 1000, 2))
            pool.submit(one, proto, request_size, response_size)


def run_client(s):
    plan = s.plan
    player = _Player() if plan.profile == "abr_video" else None

    def tick(k):
        if plan.profile in DOWNLOADERS:
            s.writer.emit("throughput_kbit", s.rx.take() * 8 / 1000, k, "down")
        if player:
            with player.lock:
                player.advance()
                stall, player.stall_s = player.stall_s, 0.0
                kbit = player.kbit
            s.count("stall_ms", round(stall * 1000))
            s.writer.emit("video_stall_ms", stall * 1000, k)
            s.writer.emit("video_bitrate_kbit", kbit, k)

    s.clock.sleep_until(plan.start_s)
    ticker = Ticker(s.clock, plan.start_s, plan.stop_s, tick)
    ticker.start()
    if plan.profile == "abr_video":
        s.spawn(_abr_video, s, player)
    elif plan.profile == "web_browsing":
        s.spawn(_web_browsing, s)
    elif plan.profile == "flow_storm":
        s.spawn(_flow_storm, s)
    else:
        _greedy(s)
    s.clock.sleep_until(plan.stop_s)
    s.stop.set()
    s.close_all()
    ticker.finish()


def run(plan, role, clock, writer, flows, server_addr):
    s = Session(plan, clock, writer, flows, server_addr)
    (run_server if role == "server" else run_client)(s)
    if s.errors:
        raise RuntimeError("; ".join(s.errors[:3]))
    if role == "client" and plan.profile != "flow_storm" and s.stats.get("flows_opened", 0) == 0:
        raise RuntimeError("no connection was opened")
    return dict(s.stats)

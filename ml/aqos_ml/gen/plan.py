"""Everything a generator will do, decided before the run starts.

A plan is a pure function of the scenario, the traffic entry and the run
seed. The client and the server each compute it and get the same answer, so
neither has to tell the other what to send, and the same seed gives the same
traffic on every QoS configuration.

Sizes here are UDP or TCP payload bytes. Times are seconds from the entry's
own start.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .wire import HEADER_SIZE

TAXONOMY = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "taxonomy.json").read_text()
)

UDP_PROFILES = {"voice_call", "video_call", "gaming", "cloud_gaming"}
MAX_PAYLOAD = 1200
# Packets of one frame leave this far apart, not as one burst.
FRAME_PACING_S = 0.0003
PORT_RANGE = (20000, 60000)

# Stream ids inside one UDP flow.
MAIN, AUDIO = 0, 1

# Bitrate ladder for abr_video, kbit/s.
LADDER_KBIT = [235, 375, 750, 1750, 3000, 4300, 5800, 8000]


def _rng(seed, *names):
    digest = hashlib.sha256(":".join([str(seed), *names]).encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def allocate_ports(seed, traffic_ids):
    """Gives every entry a server port and a client port, all distinct.

    Ports change with the seed so a model cannot learn them.
    """
    ids = sorted(traffic_ids)
    low, high = PORT_RANGE
    ports = _rng(seed, "ports").choice(np.arange(low, high), size=2 * len(ids), replace=False)
    return {tid: (int(ports[2 * i]), int(ports[2 * i + 1])) for i, tid in enumerate(ids)}


@dataclass
class Schedule:
    """Packets one side of a UDP flow sends, sorted by time."""

    t_s: np.ndarray  # float64, seconds from the entry's start
    size: np.ndarray  # payload bytes
    stream: np.ndarray  # MAIN or AUDIO
    frame: np.ndarray  # frame number within the stream
    index: np.ndarray  # position of the packet in its frame
    count: np.ndarray  # packets in the frame

    def __len__(self):
        return len(self.t_s)

    def digest(self):
        h = hashlib.sha256()
        for name in ("t_s", "size", "stream", "frame", "index", "count"):
            h.update(np.ascontiguousarray(getattr(self, name)).tobytes())
        return h.hexdigest()


@dataclass
class Plan:
    traffic_id: str
    profile: str
    true_class: str
    host: str
    start_s: float
    stop_s: float
    server_port: int
    client_port: int
    params: dict
    # UDP profiles: what each side sends. Keys "up" (client) and "down" (server).
    schedules: dict = field(default_factory=dict)
    # TCP and mixed profiles: profile-specific script, plain JSON types.
    script: dict = field(default_factory=dict)

    @property
    def duration_s(self):
        return self.stop_s - self.start_s

    def digest(self):
        """Identifies the whole plan. Equal digests mean identical traffic."""
        h = hashlib.sha256()
        head = [self.traffic_id, self.profile, self.start_s, self.stop_s,
                self.server_port, self.client_port, self.params, self.script]
        h.update(json.dumps(head, sort_keys=True).encode())
        for direction in sorted(self.schedules):
            h.update(direction.encode())
            h.update(self.schedules[direction].digest().encode())
        return h.hexdigest()


def _draw(rng, overrides, name, low, high, integer=False):
    """Draws a parameter, then lets the scenario override it.

    The draw happens even when overridden, so pinning one parameter does not
    shift the random values of the others.
    """
    value = int(rng.integers(low, high + 1)) if integer else float(rng.uniform(low, high))
    if name in overrides:
        value = type(value)(overrides[name])
    return value


def _packet_stream(rng, duration_s, rate_hz, size, spread, stream):
    """One packet per tick, each its own frame."""
    n = max(int(duration_s * rate_hz), 1)
    t = np.arange(n) / rate_hz
    sizes = np.clip(rng.normal(size, size * spread, n).round(), HEADER_SIZE, MAX_PAYLOAD)
    return t, sizes.astype(np.int32), np.full(n, stream, np.int8), np.arange(n, dtype=np.int64), \
        np.zeros(n, np.int32), np.ones(n, np.int32)


def _frame_stream(rng, duration_s, fps, kbit, keyframe_s):
    """Video frames split into packets of at most MAX_PAYLOAD."""
    n_frames = max(int(duration_s * fps), 1)
    mean = kbit * 1000 / 8 / fps
    frame_bytes = mean * rng.lognormal(-0.02, 0.2, n_frames)
    # A keyframe is several times a normal frame; the rest shrink to keep
    # the average at the target bitrate.
    period = max(int(keyframe_s * fps), 1)
    key = np.arange(n_frames) % period == 0
    frame_bytes[key] *= 4
    frame_bytes *= mean * n_frames / frame_bytes.sum()
    frame_bytes = np.maximum(frame_bytes.round().astype(np.int64), HEADER_SIZE)

    counts = -(-frame_bytes // MAX_PAYLOAD)
    total = int(counts.sum())
    frame = np.repeat(np.arange(n_frames, dtype=np.int64), counts)
    first = np.repeat(np.cumsum(counts) - counts, counts)
    index = (np.arange(total) - first).astype(np.int32)
    count = np.repeat(counts, counts).astype(np.int32)
    # Equal split, with the remainder spread over the first packets.
    base = np.repeat(frame_bytes // counts, counts)
    extra = (index < np.repeat(frame_bytes % counts, counts)).astype(np.int64)
    size = np.maximum(base + extra, HEADER_SIZE).astype(np.int32)
    pacing = np.minimum(FRAME_PACING_S, 0.5 / fps / np.maximum(count, 1))
    t = frame / fps + index * pacing
    return t, size, np.full(total, MAIN, np.int8), frame, index, count


def _merge(*streams):
    cols = [np.concatenate(c) for c in zip(*streams)]
    order = np.argsort(cols[0], kind="stable")
    return Schedule(*(c[order] for c in cols))


# A replayed call (aqos_ml.trace). Repeats of the trace skip its first
# seconds, since a call sets up once. Payloads under AUDIO_BELOW are audio,
# keep-alives and control, judged by no deadline; video packets closer
# together than FRAME_GAP_S were sent as one frame.
REPLAY_LOOP_FROM_S = 5.0
AUDIO_BELOW = 200
FRAME_GAP_S = 0.003
# Replayed packets keep their recorded size up to what one IPv6 packet on a
# 1500-byte link carries.
REPLAY_MAX_PAYLOAD = 1452


def _replay_stream(side, trace_s, duration_s):
    """One direction of a recorded call, looped to the entry's length, as
    (t, size, stream, frame, index, count) like the other streams."""
    t0, size0 = np.asarray(side["t_s"], np.float64), np.asarray(side["size"], np.int64)
    body = t0 >= REPLAY_LOOP_FROM_S
    ts, sizes, offset = [t0], [size0], trace_s
    while offset < duration_s and body.any():
        ts.append(t0[body] - REPLAY_LOOP_FROM_S + offset)
        sizes.append(size0[body])
        offset += trace_s - REPLAY_LOOP_FROM_S
    t, size = np.concatenate(ts), np.concatenate(sizes)
    keep = t < duration_s
    t, size = t[keep], np.clip(size[keep], HEADER_SIZE, REPLAY_MAX_PAYLOAD).astype(np.int32)
    stream = np.where(size < AUDIO_BELOW, AUDIO, MAIN).astype(np.int8)
    frame = np.zeros(len(t), np.int64)
    index = np.zeros(len(t), np.int32)
    count = np.ones(len(t), np.int32)
    audio = stream == AUDIO
    frame[audio] = np.arange(audio.sum())
    main = np.flatnonzero(~audio)
    if len(main):
        starts = np.concatenate([[True], np.diff(t[main]) > FRAME_GAP_S])
        ids = np.cumsum(starts) - 1
        first = np.flatnonzero(starts)
        counts = np.diff(np.append(first, len(main)))
        frame[main] = ids
        index[main] = np.arange(len(main)) - first[ids]
        count[main] = counts[ids]
        frames = int(ids[-1]) + 1
    else:
        frames = 0
    return (t, size, stream, frame, index, count), frames / duration_s


# What a scenario's traffic_param events may change, per profile: the
# parameter, its two values (normal first), and the value an entry starts
# with unless its params say otherwise.
CHANGES = {
    "video_call": ("camera", ("on", "off")),
    "gaming": ("phase", ("match", "lobby")),
    "bulk_download": ("paused", (False, True)),
    "game_update": ("paused", (False, True)),
}
# Packets per second a game sends while it sits in the lobby.
LOBBY_HZ = 5


def _changes(scenario, entry, start_s, stop_s):
    """The stretches of an entry's life, in seconds from its start, during
    which its changeable parameter has its second value: camera off, in the
    lobby, paused. They come from the entry's starting value and the
    scenario's traffic_param events, so both ends know them before the run
    and no generator is told anything while it runs."""
    profile = entry["profile"]
    events = sorted((e for e in scenario.get("events") or []
                     if e["type"] == "traffic_param" and (e.get("args") or {}).get("traffic") == entry["id"]),
                    key=lambda e: e["at_s"])
    if profile not in CHANGES:
        if events:
            raise ValueError(f"{profile} has no parameter a traffic_param event can change")
        return None, None, []
    name, (normal, other) = CHANGES[profile]
    state = (entry.get("params") or {}).get(name, normal)
    if state not in (normal, other):
        raise ValueError(f"{profile}: {name} is {normal!r} or {other!r}, not {state!r}")
    initial, spans, since = state, [], 0.0
    for event in events:
        args = event["args"]
        if args.get("param") != name or args.get("value") not in (normal, other):
            raise ValueError(f"traffic_param for {entry['id']!r}: {profile} takes {name} = {normal!r} or {other!r}")
        at = min(max(float(event["at_s"]) - start_s, 0.0), stop_s - start_s)
        if state == other and args["value"] == normal:
            spans.append([round(since, 3), round(at, 3)])
        elif state == normal and args["value"] == other:
            since = at
        state = args["value"]
    if state == other:
        spans.append([round(since, 3), round(stop_s - start_s, 3)])
    return name, initial, [span for span in spans if span[1] > span[0]]


def _within(t, spans):
    inside = np.zeros(len(t), bool)
    for low, high in spans:
        inside |= (t >= low) & (t < high)
    return inside


def _thin(schedule, drop):
    """A schedule without the packets marked in drop, its main stream's
    frames renumbered so that they still count up without gaps."""
    keep = ~drop
    cols = [getattr(schedule, name)[keep] for name in ("t_s", "size", "stream", "frame", "index", "count")]
    main = cols[2] == MAIN
    if main.any():
        cols[3] = cols[3].copy()
        cols[3][main] = np.unique(cols[3][main], return_inverse=True)[1]
    return Schedule(*cols)


def _apply_changes(profile, params, schedules, spans):
    """Removes from a UDP entry's schedules what it does not send while its
    parameter has its second value. With the camera off a call is audio
    only, both ways. In the lobby a game sends a few packets a second."""
    if not spans:
        return schedules
    out = {}
    for direction, schedule in schedules.items():
        if profile == "video_call":
            # A frame goes or stays whole, by the time it is due.
            drop = (schedule.stream == MAIN) & _within(schedule.frame / params["fps"], spans)
        else:
            step = max(round(params["tick_hz"] / LOBBY_HZ), 1)
            drop = _within(schedule.t_s, spans) & (schedule.frame % step != 0)
        out[direction] = _thin(schedule, drop)
    return out


def _udp_plan(profile, rng, p, duration_s):
    """Returns (params, schedules) for a UDP profile."""
    if profile == "voice_call":
        params = {
            "ptime_ms": 20,
            "payload_up": _draw(rng, p, "payload_up", 100, 200, integer=True),
            "payload_down": _draw(rng, p, "payload_down", 100, 200, integer=True),
            "deadline_ms": 0,
        }
        rate = 1000 / params["ptime_ms"]
        return params, {
            d: _merge(_packet_stream(rng, duration_s, rate, params["payload_" + d], 0.05, MAIN))
            for d in ("up", "down")
        }

    if profile == "video_call" and "trace" in p:
        from .. import trace as traces
        recorded = traces.load(p["trace"])
        params, schedules = {"trace": p["trace"], "deadline_ms": float(p.get("deadline_ms", 100))}, {}
        for d in ("up", "down"):
            columns, fps = _replay_stream(recorded[d], recorded["duration_s"], duration_s)
            schedules[d] = _merge(columns)
            params["fps_" + d] = round(fps, 3)
        return params, schedules

    if profile == "video_call":
        params = {
            "fps": 30,
            "kbit_up": _draw(rng, p, "kbit_up", 1000, 2500),
            "kbit_down": _draw(rng, p, "kbit_down", 1000, 2500),
            "audio_payload": _draw(rng, p, "audio_payload", 100, 200, integer=True),
            "keyframe_s": _draw(rng, p, "keyframe_s", 5, 15),
            "deadline_ms": float(p.get("deadline_ms", 100)),
        }
        return params, {
            d: _merge(
                _frame_stream(rng, duration_s, params["fps"], params["kbit_" + d], params["keyframe_s"]),
                _packet_stream(rng, duration_s, 50, params["audio_payload"], 0.05, AUDIO),
            )
            for d in ("up", "down")
        }

    if profile == "gaming":
        params = {
            "tick_hz": _draw(rng, p, "tick_hz", 30, 128, integer=True),
            "payload_up": _draw(rng, p, "payload_up", 50, 200, integer=True),
            "payload_down": _draw(rng, p, "payload_down", 50, 200, integer=True),
            "deadline_ms": float(p.get("deadline_ms", 50)),
        }
        return params, {
            d: _merge(_packet_stream(rng, duration_s, params["tick_hz"], params["payload_" + d], 0.2, MAIN))
            for d in ("up", "down")
        }

    if profile == "cloud_gaming":
        params = {
            "fps": 60,
            "kbit_down": _draw(rng, p, "kbit_down", 10000, 25000),
            "input_hz": 125,
            "payload_up": _draw(rng, p, "payload_up", 40, 100, integer=True),
            "keyframe_s": _draw(rng, p, "keyframe_s", 2, 10),
            "deadline_ms": float(p.get("deadline_ms", 50)),
        }
        return params, {
            "up": _merge(_packet_stream(rng, duration_s, params["input_hz"], params["payload_up"], 0.2, MAIN)),
            "down": _merge(_frame_stream(rng, duration_s, params["fps"], params["kbit_down"], params["keyframe_s"])),
        }
    raise ValueError(profile)


def _tcp_plan(profile, rng, p, duration_s):
    """Returns (params, script) for a TCP or mixed profile."""
    if profile == "bulk_download":
        return {"connections": _draw(rng, p, "connections", 1, 4, integer=True)}, {}
    if profile == "game_update":
        return {"connections": _draw(rng, p, "connections", 4, 8, integer=True)}, {}
    if profile == "backup_upload":
        return {"connections": _draw(rng, p, "connections", 1, 2, integer=True)}, {}

    if profile == "abr_video":
        params = {
            "segment_s": _draw(rng, p, "segment_s", 2, 4, integer=True),
            "max_kbit": _draw(rng, p, "max_kbit", 3000, 8000),
            "buffer_max_s": float(p.get("buffer_max_s", 30)),
            "reservoir_s": float(p.get("reservoir_s", 5)),
            "cushion_s": float(p.get("cushion_s", 20)),
        }
        n = int(duration_s / params["segment_s"]) + 2
        # Encoded segments vary around their rung's nominal size.
        scale = np.clip(rng.normal(1.0, 0.15, n), 0.6, 1.5).round(4)
        ladder = [r for r in LADDER_KBIT if r <= params["max_kbit"]] or LADDER_KBIT[:1]
        return params, {"ladder_kbit": ladder, "segment_scale": scale.tolist()}

    if profile == "web_browsing":
        params = {
            "think_s": _draw(rng, p, "think_s", 5, 15),
            "parallel": int(p.get("parallel", 6)),
        }
        pages, t = [], 0.0
        while t < duration_s:
            think = float(np.clip(rng.exponential(params["think_s"]), 1, 4 * params["think_s"]))
            if not pages:
                # Someone who opens a browser loads a page at once; the
                # thinking comes between pages. Without this a short entry
                # could end before its first page.
                think = 0.0
            objects = int(rng.integers(5, 25))
            sizes = np.clip(rng.lognormal(10.0, 1.3, objects), 500, 4_000_000).astype(int)
            pages.append({"think_s": round(think, 3), "sizes": sizes.tolist()})
            t += think + 1
        return params, {"pages": pages}

    if profile == "p2p":
        params = {
            "tcp_down": _draw(rng, p, "tcp_down", 4, 10, integer=True),
            "tcp_up": _draw(rng, p, "tcp_up", 4, 10, integer=True),
            "udp_peers": _draw(rng, p, "udp_peers", 4, 12, integer=True),
        }
        peers = [
            {"pps": round(float(rng.uniform(5, 50)), 2), "payload": int(rng.integers(100, MAX_PAYLOAD + 1))}
            for _ in range(params["udp_peers"])
        ]
        return params, {"peers": peers}

    if profile == "flow_storm":
        params = {
            "flows_per_s": _draw(rng, p, "flows_per_s", 20, 100),
            "udp_share": float(p.get("udp_share", 0.5)),
        }
        n = max(int(duration_s * params["flows_per_s"]), 1)
        starts = np.sort(rng.uniform(0, duration_s, n)).round(4)
        udp = rng.random(n) < params["udp_share"]
        request = rng.integers(40, 400, n)
        response = np.where(udp, rng.integers(60, 500, n), rng.integers(500, 20000, n))
        return params, {
            "flows": [
                [float(s), "udp" if u else "tcp", int(q), int(r)]
                for s, u, q, r in zip(starts, udp, request, response)
            ]
        }
    raise ValueError(profile)


def build(scenario, traffic_id, seed):
    """Builds the plan for one traffic entry of a scenario (a parsed YAML dict)."""
    entries = {t["id"]: t for t in scenario["traffic"]}
    if traffic_id not in entries:
        raise KeyError(f"scenario {scenario['id']} has no traffic entry {traffic_id!r}")
    entry = entries[traffic_id]
    profile = entry["profile"]
    run_end = scenario["warmup_s"] + scenario["duration_s"]
    stop_s = entry.get("stop_s")
    stop_s = run_end if stop_s is None else min(float(stop_s), run_end)
    start_s = float(entry["start_s"])
    if stop_s <= start_s:
        raise ValueError(f"traffic entry {traffic_id!r} stops before it starts")

    overrides = dict(entry.get("params") or {})
    rng = _rng(seed, "traffic", traffic_id)
    duration_s = stop_s - start_s
    changing, initial, spans = _changes(scenario, entry, start_s, stop_s)
    overrides.pop(changing, None)
    schedules, script = {}, {}
    if profile in UDP_PROFILES:
        params, schedules = _udp_plan(profile, rng, overrides, duration_s)
        if spans and "trace" in params:
            raise ValueError(f"traffic entry {traffic_id!r} replays a recorded call; its camera cannot be switched")
        schedules = _apply_changes(profile, params, schedules, spans)
    else:
        params, script = _tcp_plan(profile, rng, overrides, duration_s)
        if spans:
            script["pauses"] = spans
    if changing:
        params[changing] = initial
    params["ip_version"] = int(overrides.get("ip_version", 4))
    unknown = set(overrides) - set(params)
    if unknown:
        raise ValueError(f"{profile} does not take params {sorted(unknown)}")

    server_port, client_port = allocate_ports(seed, entries)[traffic_id]
    return Plan(
        traffic_id=traffic_id, profile=profile, true_class=TAXONOMY["profiles"][profile],
        host=entry["host"], start_s=start_s, stop_s=stop_s,
        server_port=server_port, client_port=client_port,
        params=params, schedules=schedules, script=script,
    )

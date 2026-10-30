"""Packet traces of real calls, for replaying them in the testbed.

A trace is the main UDP flow of an own capture reduced to what the
generator needs and nothing else: for each direction, when each packet was
sent and how large its UDP payload was. No addresses, ports or payload
bytes are kept, so a trace can live in the repository.

    python -m aqos_ml.trace data/raw/<session>.pcap scenarios/traces/<name>.json.gz

The replayed call then runs as a `video_call` entry with `params: {trace:
<name>}`: the same sizes and timing, the same first packets the classifier
sees, sent between testbed hosts instead of over the internet.
"""

import argparse
import gzip
import ipaddress
import json
import struct
import sys
from pathlib import Path

import numpy as np

from . import capture

TRACES = Path(__file__).resolve().parents[2] / "scenarios" / "traces"


def _packets(pcap, flow):
    """(time s, direction, UDP payload bytes) of every packet of one flow;
    direction 0 is the side that sent first."""
    data = Path(pcap).read_bytes()
    endian = "<" if data[:4] in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
    nanos = data[:4] in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack_from(endian + "I", data, 20)[0] & 0xFFFF
    record, pos, out = struct.Struct(endian + "IIII"), 24, []
    while pos + 16 <= len(data):
        sec, frac, caplen, origlen = record.unpack_from(data, pos)
        frame = data[pos + 16:pos + 16 + caplen]
        pos += 16 + caplen
        ip = capture._ip_offset(linktype, frame)
        if ip is None or len(frame) < ip + 28:
            continue
        if frame[ip] >> 4 == 4:
            if frame[ip + 9] != 17:
                continue
            l4, src, dst = ip + (frame[ip] & 0xF) * 4, frame[ip + 12:ip + 16], frame[ip + 16:ip + 20]
        elif frame[ip] >> 4 == 6 and len(frame) >= ip + 48:
            if frame[ip + 6] != 17:
                continue
            l4, src, dst = ip + 40, frame[ip + 8:ip + 24], frame[ip + 24:ip + 40]
        else:
            continue
        sport, dport = struct.unpack_from("!HH", frame, l4)
        a = f"{ipaddress.ip_address(src)}:{sport}"
        b = f"{ipaddress.ip_address(dst)}:{dport}"
        if (a, b) == (flow.a, flow.b):
            direction = 0
        elif (b, a) == (flow.a, flow.b):
            direction = 1
        else:
            continue
        # origlen is the frame on the wire; the payload is what follows the
        # UDP header, whatever the snap length kept.
        out.append((sec + frac / (1e9 if nanos else 1e6), direction, origlen - (l4 + 8)))
    return out


def extract(pcap, session=None):
    """The trace of a capture's largest UDP flow, as a plain dict. "up" is
    the direction the capturing host sent."""
    flows = [f for f in capture.read_flows(pcap) if f.protocol == 17]
    if not flows:
        raise ValueError(f"{pcap}: no UDP flow")
    flow = flows[0]
    local = ipaddress.ip_address(flow.a.rsplit(":", 1)[0]).is_private
    packets = _packets(pcap, flow)
    t0 = packets[0][0]
    trace = {"schema_version": 1, "session": session or Path(pcap).stem, "up": {"t_s": [], "size": []},
             "down": {"t_s": [], "size": []}}
    for t, direction, size in packets:
        side = "up" if (direction == 0) == local else "down"
        trace[side]["t_s"].append(round(t - t0, 6))
        trace[side]["size"].append(int(size))
    trace["duration_s"] = round(packets[-1][0] - t0, 3)
    return trace


def load(name):
    """A trace from scenarios/traces by name (without .json.gz)."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"trace {name!r}: a name, not a path")
    with gzip.open(TRACES / f"{name}.json.gz", "rt") as f:
        return json.load(f)


def summary(trace):
    out = {}
    for side in ("up", "down"):
        t, size = np.array(trace[side]["t_s"]), np.array(trace[side]["size"])
        span = max(trace["duration_s"], 1e-9)
        out[side] = {"packets": len(t), "kbit_s": round(float(size.sum()) * 8 / span / 1000, 1),
                     "packets_s": round(len(t) / span, 1)}
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m aqos_ml.trace", description=__doc__.split("\n\n")[0])
    parser.add_argument("pcap")
    parser.add_argument("out")
    args = parser.parse_args(argv)
    trace = extract(args.pcap)
    with gzip.open(args.out, "wt") as f:
        json.dump(trace, f, separators=(",", ":"))
    print(json.dumps({"session": trace["session"], "duration_s": trace["duration_s"], **summary(trace)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Real-app capture sessions (chunk 2C).

One session is one activity on one device: one capture, one label.

  python -m aqos_ml.capture record --activity video_call --app "Google Meet" \\
        --device laptop --network home-wifi --iface wlp2s0 [--seconds 120]
  python -m aqos_ml.capture review data/raw/<session>.json
  python -m aqos_ml.capture accept data/raw/<session>.json [--keep 0,1 | --drop 3,7]

record runs tcpdump with headers only and writes the session file beside
the capture. Start recording first and the activity second: the classifier
works from a flow's first 8 packets, so a flow already running when the
capture began is useless. review lists the capture's flows, largest first,
and says which ones are unusable or look like background traffic (DNS,
updates, sync). accept marks the session as usable for the dataset and
records the flows to leave out.

Captures and session files stay in data/raw/, which git ignores. Only
feature records, which carry no addresses, are shared.
"""

import argparse
import datetime
import ipaddress
import json
import re
import signal
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .gen.plan import TAXONOMY

SNAPLEN = 128  # headers only
MIN_PACKETS = 8  # shorter flows are never classified, so never used
# Synthetic profiles have no real application to capture.
ACTIVITIES = {p: c for p, c in TAXONOMY["profiles"].items() if p != "flow_storm"}
# Ports that are almost always background traffic, whatever the activity.
BACKGROUND_PORTS = {53: "dns", 67: "dhcp", 68: "dhcp", 123: "ntp", 137: "netbios", 1900: "ssdp",
                    5353: "mdns", 5355: "llmnr"}
RAW_DIR = Path("data/raw")
MTU = 1500
# A UDP flow seen this soon after the capture began was probably already
# running. TCP is judged by its SYN instead.
EARLY_S = 1.0


def tcpdump_command(iface, pcap, seconds=None):
    cmd = ["tcpdump", "-i", iface, "-s", str(SNAPLEN), "-n", "-w", str(pcap), "ip or ip6"]
    if seconds:
        cmd = ["timeout", "--signal=INT", str(seconds), *cmd]
    return cmd


def new_session(activity, app, device, network, iface, now=None, notes=""):
    if activity not in ACTIVITIES:
        raise ValueError(f"unknown activity {activity!r}; one of: {', '.join(sorted(ACTIVITIES))}")
    now = now or datetime.datetime.now().astimezone()
    slug = re.sub(r"[^a-z0-9]+", "-", f"{activity}-{app}-{device}".lower()).strip("-")
    session_id = f"{now:%Y%m%d-%H%M%S}-{slug}"
    return {
        "schema_version": 1, "session": session_id,
        "activity": activity, "label": ACTIVITIES[activity],
        "app": app, "device": device, "network": network, "iface": iface,
        "pcap": session_id + ".pcap", "snaplen": SNAPLEN,
        "started_at": now.isoformat(timespec="seconds"), "ended_at": None,
        "accepted": False, "dropped_flows": [], "notes": notes,
    }


@dataclass
class Flow:
    protocol: int
    a: str  # the side that sent the first packet, "address:port"
    b: str
    packets: int = 0
    bytes: int = 0
    first_s: float = 0.0
    last_s: float = 0.0
    offset_s: float = 0.0  # first packet, from the start of the capture
    syn: bool = False  # TCP: the first packet seen opened the connection
    oversize: int = 0  # packets larger than any link carries

    @property
    def from_its_start(self):
        """False when the capture missed the flow's first packets."""
        return self.syn if self.protocol == 6 else self.offset_s >= EARLY_S

    @property
    def key(self):
        return {"protocol": self.protocol, "a": self.a, "b": self.b}

    @property
    def port(self):
        return int(self.b.rsplit(":", 1)[1])


_LINK_HEADER = {1: 14, 113: 16, 276: 20, 101: 0, 12: 0}


def _ip_offset(linktype, frame):
    """Offset of the IP header, or None if the frame carries no IP."""
    if linktype == 1:
        ethertype, offset = frame[12:14], 14
        while ethertype in (b"\x81\x00", b"\x88\xa8"):  # VLAN tags
            ethertype, offset = frame[offset + 2:offset + 4], offset + 4
        return offset if ethertype in (b"\x08\x00", b"\x86\xdd") else None
    if linktype == 113:
        return 16 if frame[14:16] in (b"\x08\x00", b"\x86\xdd") else None
    if linktype == 276:
        return 20 if frame[0:2] in (b"\x08\x00", b"\x86\xdd") else None
    return 0


def read_flows(pcap):
    """Groups a capture's TCP and UDP packets into flows. Only headers are
    read; a capture taken with a larger snap length gives the same answer."""
    data = Path(pcap).read_bytes()
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian = ">"
    else:
        raise ValueError(f"{pcap}: not a pcap file (pcapng is not supported; use tcpdump -w)")
    nanos = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    linktype = struct.unpack_from(endian + "I", data, 20)[0] & 0xFFFF
    if linktype not in _LINK_HEADER:
        raise ValueError(f"{pcap}: link type {linktype} is not supported")

    flows, pos, record, began = {}, 24, struct.Struct(endian + "IIII"), None
    while pos + 16 <= len(data):
        sec, frac, caplen, origlen = record.unpack_from(data, pos)
        frame = data[pos + 16:pos + 16 + caplen]
        pos += 16 + caplen
        ip = _ip_offset(linktype, frame)
        if ip is None or len(frame) < ip + 20:
            continue
        version = frame[ip] >> 4
        if version == 4:
            protocol, l4 = frame[ip + 9], ip + (frame[ip] & 0xF) * 4
            src, dst = frame[ip + 12:ip + 16], frame[ip + 16:ip + 20]
        elif version == 6 and len(frame) >= ip + 40:
            protocol, l4 = frame[ip + 6], ip + 40
            src, dst = frame[ip + 8:ip + 24], frame[ip + 24:ip + 40]
        else:
            continue
        if protocol not in (6, 17) or len(frame) < l4 + 4:
            continue
        sport, dport = struct.unpack_from("!HH", frame, l4)
        src = f"{ipaddress.ip_address(src)}:{sport}"
        dst = f"{ipaddress.ip_address(dst)}:{dport}"
        t = sec + frac / (1e9 if nanos else 1e6)
        began = t if began is None else began
        flow = flows.get((protocol, src, dst)) or flows.get((protocol, dst, src))
        if flow is None:
            syn = protocol == 6 and len(frame) > l4 + 13 and frame[l4 + 13] & 0x12 == 0x02
            flow = flows[protocol, src, dst] = Flow(protocol, src, dst, first_s=t, offset_s=t - began, syn=syn)
        flow.packets += 1
        flow.bytes += origlen - ip
        flow.oversize += origlen - ip > MTU
        flow.last_s = t
    return sorted(flows.values(), key=lambda f: -f.bytes)


def review_table(flows, dropped=(), total=None):
    """The table a reviewer reads, with a hint on flows that look like
    background traffic."""
    total = total or sum(f.bytes for f in flows) or 1
    lines = [f"{'#':>3}  {'proto':5} {'initiator':>28}    {'responder':<28} {'pkts':>7} {'bytes':>11} {'share':>6} {'secs':>6}  note"]
    for i, f in enumerate(flows):
        notes = []
        if f.key in dropped:
            notes.append("DROPPED")
        if f.packets < MIN_PACKETS:
            notes.append(f"under {MIN_PACKETS} packets, never used")
        elif not f.from_its_start:
            notes.append("UNUSABLE: began before the capture")
        elif f.oversize:
            notes.append(f"UNUSABLE: {f.oversize} packets over {MTU} bytes, receive offload was on")
        elif f.port in BACKGROUND_PORTS:
            notes.append(f"{BACKGROUND_PORTS[f.port]}: background?")
        elif f.bytes / total < 0.01:
            notes.append("small: background?")
        lines.append(f"{i:>3}  {'tcp' if f.protocol == 6 else 'udp':5} {f.a:>28} -> {f.b:<28} {f.packets:>7} "
                     f"{f.bytes:>11} {f.bytes / total:>6.1%} {f.last_s - f.first_s:>6.1f}  {', '.join(notes)}")
    return "\n".join(lines)


def _load(path):
    path = Path(path)
    session = json.loads(path.read_text())
    return path, session, read_flows(path.with_name(session["pcap"]))


def _save(path, session):
    Path(path).write_text(json.dumps(session, indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aqos_ml.capture", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("record")
    p.add_argument("--activity", required=True, choices=sorted(ACTIVITIES))
    p.add_argument("--app", required=True)
    p.add_argument("--device", required=True)
    p.add_argument("--network", required=True)
    p.add_argument("--iface", required=True)
    p.add_argument("--seconds", type=int, help="stop by itself; otherwise stop with Ctrl-C")
    p.add_argument("--notes", default="", help="what exactly was done, for example the game and its kind")
    p.add_argument("--dir", default=RAW_DIR, type=Path)
    p.add_argument("--no-sudo", action="store_true", help="tcpdump already has the capability to capture")
    p = sub.add_parser("review")
    p.add_argument("session")
    p.add_argument("--top", type=int, help="show only the largest N flows")
    p = sub.add_parser("accept")
    p.add_argument("session")
    p.add_argument("--drop", default="", help="flow numbers from review to leave out, comma separated")
    p.add_argument("--keep", default="", help="keep only these flow numbers and drop every other")
    p.add_argument("--force", action="store_true", help="keep flows that review marks UNUSABLE")
    p.add_argument("--notes", default=None)
    args = parser.parse_args(argv)

    if args.command == "record":
        session = new_session(args.activity, args.app, args.device, args.network, args.iface, notes=args.notes)
        args.dir.mkdir(parents=True, exist_ok=True)
        cmd = tcpdump_command(args.iface, args.dir / session["pcap"], args.seconds)
        if not args.no_sudo:
            cmd = ["sudo", *cmd]
        print(f"Recording {args.activity} ({session['label']}). Do only this activity on the device.")
        print("Stop with Ctrl-C." if not args.seconds else f"Stops after {args.seconds} s.")
        # Ctrl-C reaches tcpdump through the terminal; this process waits for it.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        code = subprocess.run(cmd).returncode
        if not (args.dir / session["pcap"]).exists():
            print(f"tcpdump wrote no capture (exit {code})", file=sys.stderr)
            return 1
        session["ended_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        path = args.dir / (session["session"] + ".json")
        _save(path, session)
        print(f"Wrote {path}. Next: python -m aqos_ml.capture review {path}")
        return 0

    path, session, flows = _load(args.session)
    if args.command == "review":
        print(f"{session['session']}: {session['activity']} ({session['label']}), {session['app']} on "
              f"{session['device']}, accepted: {session['accepted']}")
        if session.get("notes"):
            print(f"notes: {session['notes']}")
        shown = flows[:args.top] if args.top else flows
        print(review_table(shown, session["dropped_flows"], total=sum(f.bytes for f in flows)))
        if len(shown) < len(flows):
            rest = sum(f.bytes for f in flows[len(shown):]) / (sum(f.bytes for f in flows) or 1)
            print(f"     ... and {len(flows) - len(shown)} smaller flows, {rest:.1%} of the bytes together")
        return 0
    drop = [int(n) for n in args.drop.split(",") if n]
    keep = [int(n) for n in args.keep.split(",") if n]
    if any(n < 0 or n >= len(flows) for n in drop + keep):
        print(f"flow numbers go from 0 to {len(flows) - 1}", file=sys.stderr)
        return 1
    if keep:
        drop = sorted(set(drop) | (set(range(len(flows))) - set(keep)))
    bad = [i for i, f in enumerate(flows)
           if i not in drop and f.packets >= MIN_PACKETS and (not f.from_its_start or f.oversize)]
    if bad and not args.force:
        print(f"Not accepted: flows {','.join(map(str, bad))} are UNUSABLE (see review). "
              "Drop them, or record again.", file=sys.stderr)
        return 1
    session["dropped_flows"] = [flows[n].key for n in drop]
    session["accepted"] = True
    if args.notes is not None:
        session["notes"] = args.notes
    _save(path, session)
    kept = [f for i, f in enumerate(flows) if i not in drop and f.packets >= MIN_PACKETS]
    print(f"Accepted {session['session']}: {len(kept)} flows labelled {session['label']}, {len(drop)} dropped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

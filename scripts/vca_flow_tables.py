#!/usr/bin/env python3
"""Reduces the Purdue video-conferencing dataset (PAM 2026) to a flow table.

    .venv/bin/python -I scripts/vca_flow_tables.py data/ext/purdue-vca/pam2026.zip data/public/purdue-vca

The dataset is not packet captures but one CSV row per packet, so it needs
this reader of its own. The output has the same form as the tables
kaggle/pcap_flow_tables.py makes, and goes through the same converter.

What is read, and why:

  - Meet, Teams, Zoom and Webex calls recorded under a fixed or a changing
    bandwidth. The runs with competing background traffic are left out:
    their extra flows are test traffic, not something to learn from.
  - Two of each run's four files: what the client sent before the emulated
    link, and what it received after it. Together they are what a home
    router between the client and the link would see.
  - UDP only. The calls' media is UDP in every run; the CSV has no TCP
    flags, so whether a TCP flow was seen from its start cannot be known.

The `size` column counts the Ethernet header, so 14 is taken off to get the
IP length every other source uses.
"""

import csv
import hashlib
import heapq
import importlib.util
import io
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("pcap_flow_tables", HERE.parent / "kaggle" / "pcap_flow_tables.py")
tables = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tables)  # no --local argument and no /kaggle: it only defines things

SOURCE = "purdue-vca-2026"
CONDITIONS = ("ConstantBandwidth", "ChangeBandwidth")
SENT, RECEIVED = "outbound_traffic_buffer_in.csv", "inbound_traffic_buffer_out.csv"
ETHERNET = 14
BACKGROUND_PORTS = {53, 67, 68, 123, 137, 138, 1900, 5353, 5355}


def packets(archive, member, outbound, opened):
    """Yields (time, outbound, src, sport, dst, dport, IP length) for the
    UDP packets of one CSV. The time of the file's first row, whatever its
    protocol, is appended to opened: that is when the capture began."""
    with archive.open(member) as raw:
        for row in csv.DictReader(io.TextIOWrapper(raw, newline="")):
            if len(opened) < 2 and row.get("time"):
                try:
                    if not opened or opened[-1][0] != member:
                        opened.append((member, float(row["time"])))
                except ValueError:
                    pass
            if row["protocol"] != "17":
                continue
            try:
                yield (float(row["time"]), outbound, row["src"], int(row["src_port"]), row["dst"],
                       int(row["dst_port"]), int(row["size"]) - ETHERNET)
            except (ValueError, TypeError):
                continue  # a row cut short


def read_run(archive, folder, run):
    """Returns (rows, audit) for one call."""
    prefix = f"{folder}/{run}/"
    opened = []
    merged = heapq.merge(packets(archive, prefix + SENT, True, opened),
                         packets(archive, prefix + RECEIVED, False, opened))
    live, ended, count = {}, None, 0
    for t, outbound, src, sport, dst, dport, size in merged:
        if sport in BACKGROUND_PORTS or dport in BACKGROUND_PORTS or size < 28:
            continue
        count += 1
        ended = t
        key, back = (src, sport, dst, dport), (dst, dport, src, sport)
        flow, forward = live.get(key), True
        if flow is None:
            flow = live.get(back)
            forward = flow is None
        if flow is None:
            flow = live[key] = tables.Flow(key, t, False, outbound)  # .a: the client opened it
        flow.add(t, size, forward)

    # A flow counts as seen from its start if it began a second or more
    # after the capture did. The capture began with its first packet of any
    # kind, which is usually not one of the call's.
    began = min((t for _, t in opened), default=None)
    salt = hashlib.sha256(run.encode()).digest()
    rows = []
    for flow in live.values():
        if flow.packets < tables.MIN_PACKETS:
            continue
        src, sport, dst, dport = flow.key
        offset = flow.t0 - began
        rows.append({
            "source": SOURCE, "file": run, "folder": folder.split("/", 1)[1],
            "protocol": 17, "ip_version": 4, "src_port": sport, "dst_port": dport,
            "src_host": hashlib.sha256(salt + src.encode()).hexdigest()[:8],
            "dst_host": hashlib.sha256(salt + dst.encode()).hexdigest()[:8],
            # The dataset renumbered the servers into private ranges, so the
            # addresses cannot say which side is the home. The file can.
            "src_private": flow.a, "dst_private": not flow.a,
            "start_s": round(offset, 6), "duration_s": round(flow.last - flow.t0, 6),
            "packets": flow.packets, "bytes": flow.bytes, "oversize": flow.oversize,
            "from_start": offset >= tables.EARLY_S,
            "first": flow.first,
            "seconds": [[s, *flow.seconds[s]] for s in sorted(flow.seconds)],
        })
    rows.sort(key=lambda r: -r["bytes"])
    total = sum(r["bytes"] for r in rows) or 1
    top = rows[0] if rows else None
    audit = {
        "source": SOURCE, "folder": folder.split("/", 1)[1], "file": run, "packets": count,
        "minutes": round((ended - began) / 60, 1) if began is not None and ended is not None else 0,
        "flows_8plus": len(rows), "usable": sum(1 for r in rows if r["from_start"] and not r["oversize"]),
        "top_flow": f"udp:{top['dst_port']}" if top else "",
        "top_flow_share": round(top["bytes"] / total, 3) if top else 0,
        "top_flow_usable": bool(top and top["from_start"] and not top["oversize"]),
        "top_flow_kbit": round(top["bytes"] * 8 / 1000 / max(top["duration_s"], 1), 1) if top else 0,
        "top_flow_pps": round(top["packets"] / max(top["duration_s"], 1), 1) if top else 0,
    }
    return rows, audit


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    archive, out = zipfile.ZipFile(argv[1]), tables.Output(argv[2], SOURCE)
    runs = {}
    for name in archive.namelist():
        parts = name.split("/")
        if len(parts) == 5 and parts[2] in CONDITIONS and parts[4] in (SENT, RECEIVED):
            runs.setdefault(("/".join(parts[:3]), parts[3]), set()).add(parts[4])
    done = 0
    for (folder, run), files in sorted(runs.items()):
        if files != {SENT, RECEIVED}:
            out.audits.append({"source": SOURCE, "folder": folder, "file": run, "error": "a file is missing"})
            continue
        started = time.time()
        try:
            rows, audit = read_run(archive, folder, run)
        except Exception as exc:  # one bad run must not lose the rest
            rows, audit = [], {"source": SOURCE, "folder": folder, "file": run,
                               "error": f"{type(exc).__name__}: {exc}"}
        for row in rows:
            out.flows.write(tables.json.dumps(row, separators=(",", ":")) + "\n")
        out.audits.append(audit)
        done += 1
        if done % 50 == 0 or "error" in audit:
            print(f"{done}/{len(runs)} {run[-44:]:44} flows {audit.get('flows_8plus', 0):3} "
                  f"top {audit.get('top_flow', ''):9} {audit.get('top_flow_kbit', 0):8} kbit/s "
                  f"{audit.get('error', '')} ({time.time() - started:.1f}s)", flush=True)
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

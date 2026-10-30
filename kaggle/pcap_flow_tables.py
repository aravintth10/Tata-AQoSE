"""Turns large public pcap datasets into small per-flow tables.

Made to be pasted into one Kaggle notebook cell (Internet: On), where the
download takes minutes instead of a night. It also runs locally:

    python kaggle/pcap_flow_tables.py --local <dir> --out <dir> [--source NAME]

which reads the captures and archives under <dir> and leaves them as they
are.

For every flow it keeps what the feature extractor needs and nothing else:
the first packets (time and signed size) and per-second packet and byte
counts in each direction. No payload and no addresses are kept. Two files
come out:

    flows-<dataset>.jsonl.gz   one line per flow of 8 packets or more
    audit-<dataset>.csv        one line per capture file: is it usable?

A flow starts at the first packet of a 5-tuple and ends after IDLE_S
without a packet, or when the same TCP tuple opens again with a new SYN.
Sizes are IP packet lengths. A positive size goes from the side that sent
the first packet.
"""

import csv
import gzip
import hashlib
import ipaddress
import json
import os
import re
import shutil
import struct
import sys
import time
import zipfile
from pathlib import Path

# ---- what to fetch (Kaggle mode) -------------------------------------------

CIC = "https://cicresearch.ca/CICDataset/"
SOURCES = [
    # Labayen et al. 2020, CC BY 4.0: browsing, bulk downloads, video, on a
    # real access line. No registration.
    # Mendeley answers Kaggle's machines with 403, so this one is fetched
    # at home instead (scripts/datasets.tsv) and read with --local.
    {"source": "labayen-2020", "kind": "mendeley", "dataset": "5pmnkshffm", "version": 1,
     "want": r"^pcap_.*\.tar\.gz$", "run": False},
    # UNB Tor / non-Tor 2016: browsing, P2P, streaming, calls. Tor traffic
    # is tunnelled and looks alike whatever the application, so only the
    # non-Tor captures are kept.
    {"source": "iscx-tor-nontor-2016", "kind": "cic", "name": "ISCX-Tor-NonTor-2017",
     "want": r"pcap", "skip": r"tor(?!rent)", "unless": r"non[-_ ]?tor"},
    # UNB VPN / non-VPN 2016. Already done once; set "run" to True to redo.
    {"source": "iscx-vpn-nonvpn-2016", "kind": "cic", "name": "ISCX-VPN-NonVPN-2016",
     "want": r"NonVPN-PCAPs", "run": False},
    # UNSW 2025 (Lyu et al., "Games Are Not Equal"): cloud gaming on GeForce
    # NOW, Xbox, Luna and PS5, from many devices. The whole set is about
    # 800 GB, far more than is useful, so a sample is taken: every capture
    # of a session starting, then a few per game and platform up to a
    # budget. No registration.
    {"source": "unsw-cloud-gaming-2025", "kind": "sharepoint",
     "share": "https://unsw-my.sharepoint.com/:f:/g/personal/z5025089_ad_unsw_edu_au/"
              "EqaMbRpsxKBGlzSvV16uNvcBj6FnYZCQOIAJkjOMMNxsNw?e=AVUJ6E",
     "take": [
         {"under": "example", "per_folder": 0, "budget_gb": 40},
         {"under": "context/pcap", "per_folder": 2, "budget_gb": 90},
         {"under": "adaptability", "per_folder": 1, "budget_gb": 40},
     ]},
]
# The UNB sets above were done on 2026-10-11; only the last source runs now.
for _spec in SOURCES[:-1]:
    _spec["run"] = False
# The UNB site serves only visitors who have filled in its registration
# form. The notebook fills it in with these details, the same six fields the
# form asks for. In the notebook they are set in the cell above this one.
# Keep the notebook private: these are your details.
REGISTRATION = globals().get("REGISTRATION") or {
    "first_name": "", "last_name": "", "email": "",
    "institution": "", "job_title": "", "country": "",
}

# ---- what to keep ----------------------------------------------------------

MIN_PACKETS = 8      # shorter flows are never classified
FIRST_PACKETS = 64   # packets kept one by one, per flow
IDLE_S = 120         # a silent 5-tuple this long is a new flow next time
MTU = 1500           # anything larger was merged by receive offload
EARLY_S = 1.0        # a UDP flow seen this soon was probably already running

_LINK_IP_OFFSET = {101: 0, 12: 0, 228: 0, 229: 0, 113: 16, 276: 20}


class Flow:
    __slots__ = ("key", "t0", "last", "syn", "packets", "bytes", "oversize", "first", "seconds", "a")

    def __init__(self, key, t, syn, a):
        self.key, self.t0, self.last, self.syn, self.a = key, t, t, syn, a
        self.packets = self.bytes = self.oversize = 0
        self.first = []
        self.seconds = {}

    def add(self, t, size, forward):
        self.packets += 1
        self.bytes += size
        self.last = t
        if size > MTU:
            self.oversize += 1
        if len(self.first) < FIRST_PACKETS:
            self.first.append([round((t - self.t0) * 1e6), size if forward else -size])
        second = int(t - self.t0)
        row = self.seconds.get(second)
        if row is None:
            row = self.seconds[second] = [0, 0, 0, 0]
        if forward:
            row[0] += 1
            row[2] += size
        else:
            row[1] += 1
            row[3] += size


def _packets(path):
    """Yields (time, link type, frame, original length) from a pcap or
    pcapng file."""
    with open(path, "rb") as f:
        magic = f.read(4)
        f.seek(0)
        if magic == b"\x0a\x0d\x0d\x0a":
            yield from _pcapng(f)
            return
        if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
            endian = "<"
        elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
            endian = ">"
        else:
            raise ValueError("not a capture file")
        scale = 1e9 if magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d") else 1e6
        head = f.read(24)
        link = struct.unpack(endian + "I", head[20:24])[0] & 0xFFFF
        record = struct.Struct(endian + "IIII")
        while True:
            raw = f.read(16)
            if len(raw) < 16:
                return
            sec, frac, caplen, origlen = record.unpack(raw)
            frame = f.read(caplen)
            if len(frame) < caplen:
                return
            yield sec + frac / scale, link, frame, origlen


def _pcapng(f):
    endian, links = "<", []
    while True:
        head = f.read(8)
        if len(head) < 8:
            return
        if head[:4] == b"\x0a\x0d\x0d\x0a":
            order = f.read(4)
            endian = "<" if order == b"\x4d\x3c\x2b\x1a" else ">"
            length = struct.unpack(endian + "I", head[4:])[0]
            f.read(length - 12)
            links = []
            continue
        kind, length = struct.unpack(endian + "II", head)
        body = f.read(length - 8)
        if len(body) < length - 8:
            return
        if kind == 1:  # interface description
            link = struct.unpack_from(endian + "H", body, 0)[0]
            resolution, pos = 1e6, 8
            while pos + 4 <= len(body) - 4:
                code, size = struct.unpack_from(endian + "HH", body, pos)
                if code == 0:
                    break
                if code == 9 and size == 1:  # if_tsresol
                    value = body[pos + 4]
                    resolution = float(2 ** (value & 0x7F)) if value & 0x80 else float(10 ** value)
                pos += 4 + (size + 3) // 4 * 4
            links.append((link, resolution))
        elif kind == 6 and links:  # enhanced packet
            iface, high, low, caplen, origlen = struct.unpack_from(endian + "IIIII", body, 0)
            link, resolution = links[iface] if iface < len(links) else links[0]
            yield ((high << 32) | low) / resolution, link, body[20:20 + caplen], origlen
        elif kind == 3 and links:  # simple packet: no timestamp, unusable
            continue


def _ip_offset(link, frame):
    if link == 1:
        ethertype, offset = frame[12:14], 14
        while ethertype in (b"\x81\x00", b"\x88\xa8"):
            ethertype, offset = frame[offset + 2:offset + 4], offset + 4
        return offset if ethertype in (b"\x08\x00", b"\x86\xdd") else None
    return _LINK_IP_OFFSET.get(link)


def read_capture(path, source="local", folder=""):
    """Returns (flows, audit row) for one capture file. folder is where the
    file sat in its archive, which is often the only label it has."""
    live, done = {}, []
    began = ended = None
    packets = ip_packets = 0
    for t, link, frame, origlen in _packets(path):
        packets += 1
        ip = _ip_offset(link, frame)
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
        ip_packets += 1
        if began is None:
            began = t
        ended = t
        sport, dport = struct.unpack_from("!HH", frame, l4)
        flags = frame[l4 + 13] if protocol == 6 and len(frame) > l4 + 13 else 0
        syn = flags & 0x12 == 0x02
        key, back = (protocol, src, sport, dst, dport), (protocol, dst, dport, src, sport)
        flow = live.get(key)
        forward = True
        if flow is None:
            flow = live.get(back)
            forward = flow is None
        if flow is not None and (t - flow.last > IDLE_S or (syn and flow.packets > 3 and t - flow.last > 1)):
            done.append(flow)
            del live[flow.key]
            flow, forward = None, True
        if flow is None:
            flow = live[key] = Flow(key, t, syn, src)
        flow.add(t, origlen - ip, forward)
    done.extend(live.values())

    name = Path(path).name
    salt = hashlib.sha256(name.encode()).digest()
    rows = []
    for flow in done:
        if flow.packets < MIN_PACKETS:
            continue
        protocol, src, sport, dst, dport = flow.key
        offset = flow.t0 - began
        rows.append({
            "source": source, "file": name, "folder": folder,
            "protocol": protocol, "ip_version": 6 if len(src) == 16 else 4,
            "src_port": sport, "dst_port": dport,
            # Enough to tell hosts apart inside one capture, not to name them.
            "src_host": hashlib.sha256(salt + src).hexdigest()[:8],
            "dst_host": hashlib.sha256(salt + dst).hexdigest()[:8],
            "src_private": ipaddress.ip_address(src).is_private,
            "dst_private": ipaddress.ip_address(dst).is_private,
            "start_s": round(offset, 6), "duration_s": round(flow.last - flow.t0, 6),
            "packets": flow.packets, "bytes": flow.bytes, "oversize": flow.oversize,
            "from_start": flow.syn if protocol == 6 else offset >= EARLY_S,
            "first": flow.first,
            "seconds": [[s, *flow.seconds[s]] for s in sorted(flow.seconds)],
        })
    rows.sort(key=lambda r: -r["bytes"])
    total = sum(r["bytes"] for r in rows) or 1
    top = rows[0] if rows else None
    audit = {
        "source": source, "folder": folder, "file": name, "size_mb": round(os.path.getsize(path) / 1e6, 1),
        "packets": packets, "tcp_udp_packets": ip_packets,
        "minutes": round((ended - began) / 60, 1) if began is not None else 0,
        "flows": len(done), "flows_8plus": len(rows),
        "usable": sum(1 for r in rows if r["from_start"] and not r["oversize"]),
        "began_before_capture": sum(1 for r in rows if not r["from_start"]),
        "with_oversize": sum(1 for r in rows if r["oversize"]),
        "top_flow": f"{'tcp' if top['protocol'] == 6 else 'udp'}:{top['dst_port']}" if top else "",
        "top_flow_share": round(top["bytes"] / total, 3) if top else 0,
        "top_flow_usable": bool(top and top["from_start"] and not top["oversize"]),
        "top_flow_kbit": round(top["bytes"] * 8 / 1000 / max(top["duration_s"], 1), 1) if top else 0,
        "top_flow_pps": round(top["packets"] / max(top["duration_s"], 1), 1) if top else 0,
    }
    return rows, audit


class Output:
    """flows-<source>.jsonl.gz and audit-<source>.csv in out_dir."""

    def __init__(self, out_dir, source):
        self.dir, self.source = Path(out_dir), source
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flows = gzip.open(self.dir / f"flows-{source}.jsonl.gz", "wt")
        self.audits = []

    def add(self, path, folder=""):
        started = time.time()
        try:
            rows, audit = read_capture(path, self.source, folder)
        except Exception as exc:  # one bad capture must not lose the rest
            audit = {"source": self.source, "folder": folder, "file": Path(path).name,
                     "error": f"{type(exc).__name__}: {exc}"}
            rows = []
        for row in rows:
            self.flows.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.audits.append(audit)
        shown = f"{folder}/{audit['file']}" if folder else audit["file"]
        print(f"{shown[-58:]:58} {audit.get('minutes', 0):7.1f} min  flows {audit.get('flows_8plus', 0):6}  "
              f"usable {audit.get('usable', 0):6}  top {audit.get('top_flow', ''):10} "
              f"{audit.get('error', '')}  ({time.time() - started:.0f}s)", flush=True)

    def close(self):
        self.flows.close()
        fields = []
        for audit in self.audits:
            fields += [k for k in audit if k not in fields]
        with open(self.dir / f"audit-{self.source}.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.audits)
        ok = [a for a in self.audits if "error" not in a]
        print(f"\n{self.source}: {len(ok)} captures read, {len(self.audits) - len(ok)} failed, "
              f"{sum(a['flows_8plus'] for a in ok)} flows kept, {sum(a['usable'] for a in ok)} usable.", flush=True)


def is_capture(name):
    return name.lower().endswith((".pcap", ".pcapng", ".cap"))


# ---- fetching --------------------------------------------------------------

ARCHIVE = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".7z", ".rar")


def register(name):
    """Submits a UNB dataset's registration form and returns the session
    that is then allowed to download it."""
    missing = [k for k, v in REGISTRATION.items() if not str(v).strip()]
    if missing:
        raise SystemExit(f"Fill in REGISTRATION in the first cell; empty: {', '.join(missing)}")
    import requests

    session = requests.Session()
    # The form posts multipart data, as a browser's FormData does.
    reply = session.post(f"{CIC}{name}/insert.php", timeout=60,
                         files={k: (None, str(v).strip()) for k, v in REGISTRATION.items()})
    try:
        answer = reply.json()
    except ValueError:
        raise RuntimeError(f"registration failed: HTTP {reply.status_code}, {reply.text[:200]!r}")
    if not answer.get("ok"):
        raise RuntimeError(f"registration refused: {answer.get('message', answer)}")
    print(f"registered for {name}: {answer.get('message', 'ok')}", flush=True)
    return session


def cic_files(session, name):
    """Walks a UNB dataset's file browser and returns [(path, url)]."""
    from html import unescape
    from urllib.parse import parse_qs, urljoin, urlparse

    base = f"{CIC}{name}/"
    found, seen, queue = [], set(), [base + "browse.php"]
    while queue and len(seen) < 200:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        page = session.get(url, timeout=60).text
        for href in re.findall(r'href=["\']([^"\']+)["\']', page):
            link = urljoin(url, unescape(href))
            query = parse_qs(urlparse(link).query)
            if "download.php" in link and query.get("file"):
                found.append((query["file"][0], link))
            elif "browse.php" in link and any(k != "t" for k in query):
                # A link back up has a shorter path than the page it is on.
                if link not in seen and len(link) > len(url) - 3:
                    queue.append(link)
    return sorted(set(found))


def sharepoint_files(session, share):
    """Lists every capture under a SharePoint folder shared by link, as
    [(path below the shared folder, download url, size)]. Opening the link
    gives the session the cookie that the listing and the downloads need."""
    from urllib.parse import parse_qs, quote, urlparse

    session.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    landing = session.get(share, timeout=60)
    landing.raise_for_status()
    root = parse_qs(urlparse(landing.url).query).get("id", [None])[0]
    if not root:
        raise RuntimeError(f"the share link did not lead to a folder: {landing.url[:120]}")
    site = landing.url.split("/_layouts/")[0]

    def api(call, path):
        return f"{site}/_api/web/{call}(decodedurl='{quote(path.replace(chr(39), chr(39) * 2))}')"

    found, queue = [], [root]
    while queue:
        folder = queue.pop()
        reply = None
        for attempt in range(4):
            try:
                reply = session.get(api("GetFolderByServerRelativePath", folder), timeout=60,
                                    params={"$expand": "Folders,Files",
                                            "$select": "Folders/ServerRelativeUrl,Files/Name,Files/Length,Files/ServerRelativeUrl"},
                                    headers={"Accept": "application/json;odata=nometadata"})
                break
            except Exception as exc:
                print(f"  listing {folder[len(root):]}: {type(exc).__name__}, trying again", flush=True)
                time.sleep(5)
        if reply is None or reply.status_code != 200:
            print(f"  could not list {folder[len(root):]}: {reply.status_code if reply is not None else 'no answer'}", flush=True)
            continue
        listing = reply.json()
        queue += [f["ServerRelativeUrl"] for f in listing.get("Folders", [])]
        for f in listing.get("Files", []):
            if is_capture(f["Name"]):
                found.append((f["ServerRelativeUrl"][len(root):].lstrip("/"),
                              api("GetFileByServerRelativePath", f["ServerRelativeUrl"]) + "/$value",
                              int(f["Length"])))
    return sorted(found)


def choose(files, take):
    """Picks a sample from [(path, url, size)]. Each rule takes, from the
    folders under a prefix, the smallest per_folder captures of each folder
    (0 means all of them), smallest folders' picks first, until its budget
    is spent. Small captures first buys the most sessions per gigabyte."""
    chosen = []
    for rule in take:
        prefix = rule["under"].rstrip("/") + "/"
        by_folder = {}
        for path, url, size in files:
            if path.startswith(prefix):
                by_folder.setdefault(path.rsplit("/", 1)[0], []).append((size, path, url))
        picks = []
        for folder in sorted(by_folder):
            ranked = sorted(by_folder[folder])
            picks += ranked[:rule["per_folder"]] if rule["per_folder"] else ranked
        budget = rule["budget_gb"] * 1e9
        for size, path, url in sorted(picks):
            if size > budget:
                break
            budget -= size
            chosen.append((path, url, size))
    return chosen


def mendeley_files(dataset, version):
    import requests

    reply = requests.get(f"https://data.mendeley.com/public-api/datasets/{dataset}/files",
                         params={"folder_id": "root", "version": version}, timeout=60,
                         headers={"Accept": "application/vnd.mendeley-public-dataset.1+json"})
    reply.raise_for_status()
    return sorted((f["filename"], f["content_details"]["download_url"]) for f in reply.json())


def download(url, dest, session, renew=None):
    """Fetches url to dest, continuing a partial file after any error. A
    registration that has run out is renewed and the download carries on."""
    for attempt in range(1, 41):
        have = dest.stat().st_size if dest.exists() else 0
        try:
            with session.get(url, headers={"Range": f"bytes={have}-"}, stream=True, timeout=60) as r:
                if r.status_code in (401, 403) and renew:
                    print(f"  {dest.name}: registration ran out; registering again", flush=True)
                    session.cookies.update(renew().cookies)
                    continue
                if r.status_code == 416:
                    return
                r.raise_for_status()
                if have and r.status_code == 200:
                    have = 0  # the server ignored the range: start again
                total = have + int(r.headers.get("Content-Length", 0))
                with open(dest, "ab" if have else "wb") as f:
                    shown = time.time()
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        have += len(chunk)
                        if time.time() - shown > 30:
                            shown = time.time()
                            print(f"  {dest.name}: {have / 1e9:.2f} of {total / 1e9:.2f} GB", flush=True)
                if have >= total:
                    return
        except Exception as exc:
            print(f"  {dest.name}: attempt {attempt} stopped ({type(exc).__name__}); continuing", flush=True)
            time.sleep(10)
    raise RuntimeError(f"could not download {url}")


def unpack(archive, into):
    """Extracts an archive. Large zips made on Windows use Deflate64, which
    Python's zipfile cannot read, so the system tools go first."""
    import subprocess

    into.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
        attempts = [["tar", "-xf", str(archive), "-C", str(into)]]
    elif name.endswith(".zip"):
        attempts = [["unzip", "-q", "-o", str(archive), "-d", str(into)],
                    ["7z", "x", "-y", "-bd", f"-o{into}", str(archive)]]
    else:
        attempts = [["7z", "x", "-y", "-bd", f"-o{into}", str(archive)],
                    ["unrar", "x", "-o+", "-idq", str(archive), str(into) + "/"]]
    for cmd in attempts:
        if shutil.which(cmd[0]) is None:
            continue
        result = subprocess.run(cmd, capture_output=True, text=True)
        # unzip exits 1 for warnings that lose nothing.
        if result.returncode == 0 or (cmd[0] == "unzip" and result.returncode == 1):
            return cmd[0]
        print(f"  {cmd[0]} failed ({result.returncode}): {result.stderr.strip()[-300:]}", flush=True)
    if name.endswith(".zip"):
        try:
            import zipfile_deflate64  # noqa: F401  teaches zipfile the method
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zipfile-deflate64"], check=False)
            import zipfile_deflate64  # noqa: F401
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
        return "zipfile"
    raise RuntimeError(f"no tool could unpack {archive.name}")


def skipped(path, spec):
    """True when some part of the path matches spec's "skip" and that same
    part does not match its "unless". Judged part by part, so a Tor folder
    inside a folder named Tor-NonTor is still left out."""
    skip, unless = spec.get("skip"), spec.get("unless")
    if not skip:
        return False
    return any(re.search(skip, part, re.I) and not (unless and re.search(unless, part, re.I))
               for part in Path(path).parts)


def add_tree(out, folder, spec, depth=0, keep=False):
    """Reads every capture under folder, unpacking archives found inside
    archives. Archives are deleted once read unless keep is set."""
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        relative = str(path.relative_to(folder))
        if skipped(relative, spec):
            continue
        if is_capture(path.name):
            out.add(path, str(Path(relative).parent).strip("."))
        elif path.name.lower().endswith(ARCHIVE) and depth < 2:
            inner = path.with_name(path.name + ".d")
            try:
                unpack(path, inner)
                if not keep:
                    path.unlink()
                add_tree(out, inner, spec, depth + 1)
            except Exception as exc:
                print(f"  could not unpack {relative}: {exc}", flush=True)
            finally:
                if keep:
                    shutil.rmtree(inner, ignore_errors=True)


def fetch_source(spec, out_dir, tmp):
    """Downloads one dataset archive by archive and reduces it to flow
    tables. Nothing here may stop the other datasets."""
    import requests

    source = spec["source"]
    print(f"\n######## {source}", flush=True)
    renew = None
    if spec["kind"] == "sharepoint":
        session = requests.Session()
        listed = sharepoint_files(session, spec["share"])
        print(f"captures offered: {len(listed)}, {sum(f[2] for f in listed) / 1e9:.0f} GB", flush=True)
        picked = choose(listed, spec["take"])
        print(f"taking {len(picked)}, {sum(f[2] for f in picked) / 1e9:.1f} GB:", flush=True)
        for path, _, size in picked:
            print(f"    {size / 1e6:9.1f} MB  {path}", flush=True)
        wanted = [(path, url) for path, url, _ in picked]
    else:
        if spec["kind"] == "cic":
            session = register(spec["name"])
            renew = lambda: register(spec["name"])  # noqa: E731
            files = cic_files(session, spec["name"])
        else:
            session = requests.Session()
            files = mendeley_files(spec["dataset"], spec["version"])
        print(f"files offered ({len(files)}):", flush=True)
        for path, _ in files:
            print(f"    {path}", flush=True)
        wanted = [(p, u) for p, u in files
                  if re.search(spec["want"], p, re.I) and not skipped(p, spec)]
        print(f"taking {len(wanted)}: {[p for p, _ in wanted]}", flush=True)

    out = Output(out_dir, source)
    try:
        for path, url in wanted:
            name = Path(path).name
            archive, folder = tmp / name, tmp / (name + ".d")
            done = len(out.audits)
            print(f"\n== {path}", flush=True)
            try:
                download(url, archive, session, renew)
                print(f"  downloaded {archive.stat().st_size / 1e9:.2f} GB", flush=True)
                if is_capture(name):
                    out.add(archive, str(Path(path).parent).strip("."))
                    # The table is useful as far as it has got: keep it on
                    # disk in case the session is cut off.
                    if (done + 1) % 20 == 0:
                        out.flows.flush()
                    continue
                tool = unpack(archive, folder)
                archive.unlink()
                print(f"  unpacked with {tool}", flush=True)
                add_tree(out, folder, spec)
            except Exception as exc:
                print(f"  FAILED {path}: {type(exc).__name__}: {exc}", flush=True)
                out.audits.append({"source": source, "file": name, "error": f"{type(exc).__name__}: {exc}"})
            finally:
                shutil.rmtree(folder, ignore_errors=True)
                archive.unlink(missing_ok=True)
    finally:
        out.close()


def fetch_all(out_dir, tmp, sources):
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    print(f"scratch disk: {shutil.disk_usage(tmp).free / 1e9:.0f} GB free", flush=True)
    for spec in sources:
        if not spec.get("run", True):
            continue
        try:
            fetch_source(spec, out_dir, tmp)
        except BaseException as exc:
            print(f"######## {spec['source']} FAILED: {type(exc).__name__}: {exc}", flush=True)
            if isinstance(exc, KeyboardInterrupt):
                raise
    print(f"\nDownload everything in {out_dir} from the Output tab.", flush=True)


def local_main(argv):
    src = Path(argv[argv.index("--local") + 1])
    source = argv[argv.index("--source") + 1] if "--source" in argv else "local"
    out = Output(argv[argv.index("--out") + 1] if "--out" in argv else "flow-tables", source)
    add_tree(out, src, {}, keep=True)
    out.close()


if "--local" in sys.argv:
    local_main(sys.argv)
elif os.path.isdir("/kaggle/working"):
    fetch_all("/kaggle/working", "/kaggle/tmp", SOURCES)
elif __name__ == "__main__":
    print(__doc__)

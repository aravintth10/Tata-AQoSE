"""Builds the dataset from public flow tables and from testbed runs.

Every row is a feature record plus:

    label     the true class
    session   the capture or run it came from; splits never divide one
    source    "testbed", or the public dataset's name
    activity  what was being done, finer than the class
    split     train, calibration, validation or test
"""

import gzip
import hashlib
import json
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from . import labels

REPO = Path(__file__).resolve().parents[3]
EXTRACT = REPO / "bin" / "aqos-extract"
WINDOW_EVERY = 5
SPLITS = ("train", "calibration", "validation", "test")
# Public sessions of one class are dealt out in this order, so a class with
# only four sessions still appears in every part. Over a long list it comes
# to six parts train, two validation, one calibration, one test.
_DEAL = ["train", "test", "validation", "calibration", "train", "train", "validation", "train", "train", "train"]
_RECORD_KEYS = ["schema_version", "flow_id", "host_id", "kind", "ts_ns", "ip_version", "protocol",
                "src_port", "dst_port", "pkt_sizes", "pkt_gaps_us", "window"]


def _order(name, seed):
    return hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()


def public_rows(flow_table):
    """Rows from one public flow table, labelled, background flows left out.
    Returns (rows, what was left out and why)."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl") as out:
        result = subprocess.run([EXTRACT, "-flow-table", flow_table, "-out", out.name,
                                 "-window-every", str(WINDOW_EVERY)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        raw = [json.loads(line) for line in open(out.name)]

    # Some sets name every capture alike and tell them apart by folder
    # (UNSW has an ex1.pcapng in each of its folders). For those the folder
    # is part of the capture's name. For the others it is left out, so
    # that their session names stay what they have always been.
    folders = defaultdict(set)
    for row in raw:
        folders[row["source"], row["file"]].add(row.get("folder", ""))
    reused = {source for (source, _), seen in folders.items() if len(seen) > 1}
    where = lambda row: row.get("folder", "") if row["source"] in reused else ""  # noqa: E731

    file_bytes = defaultdict(int)
    for row in raw:
        if row["kind"] == "initial":
            file_bytes[row["source"], where(row), row["file"]] += row["flow_bytes"]
    rows, left_out = [], Counter()
    for row in raw:
        labelled = labels.label_for(row["source"], row["file"])
        if labelled is None:
            left_out["file has no usable label"] += row["kind"] == "initial"
            continue
        activity, cls = labelled
        if not labels.is_activity_flow(activity, row, file_bytes[row["source"], where(row), row["file"]]):
            left_out["background flow"] += row["kind"] == "initial"
            continue
        rows.append({**{k: row[k] for k in _RECORD_KEYS}, "label": cls, "activity": activity,
                     "session": labels.session_for(row["source"], row["file"], where(row)),
                     "source": row["source"]})
    return rows, left_out


def testbed_rows(run_dir):
    """Rows from one run that had the live extractor on. The label comes
    from the generators' own log of the flows they opened."""
    run_dir = Path(run_dir)
    if not (run_dir / "manifest.json").exists():
        # Still running, or killed part way: the runner will do it again.
        return [], Counter({"run not finished": 1})
    manifest = json.loads((run_dir / "manifest.json").read_text())
    if not manifest["valid"]:
        return [], Counter({"run was invalid": 1})
    truth = {}
    for line in (run_dir / "flows.jsonl").read_text().splitlines():
        f = json.loads(line)
        truth[f["protocol"], f["src_ip"], f["src_port"], f["dst_ip"], f["dst_port"]] = f
    by_flow = {}
    for line in (run_dir / "keys.jsonl").read_text().splitlines():
        k = json.loads(line)
        by_flow[k["flow_id"]] = truth.get((k["protocol"], k["src_ip"], k["src_port"], k["dst_ip"], k["dst_port"]))
    rows, left_out = [], Counter()
    for line in (run_dir / "features.jsonl").read_text().splitlines():
        record = json.loads(line)
        flow = by_flow.get(record["flow_id"])
        if flow is None:
            left_out["flow no generator opened"] += record["kind"] == "initial"
            continue
        rows.append({**record, "label": flow["true_class"], "activity": flow["profile"],
                     "session": f"testbed/{manifest['run_id']}", "source": "testbed"})
    return rows, left_out


def own_rows(session_file):
    """Rows from one of our own capture sessions, if it was accepted. Only
    the flows the reviewer kept take the session's label."""
    from .. import capture

    session_file = Path(session_file)
    session = json.loads(session_file.read_text())
    if not session.get("accepted"):
        return [], Counter({"own capture not accepted": 1})
    pcap = session_file.with_name(session["pcap"])
    dropped = {(d["protocol"], d["a"], d["b"]) for d in session["dropped_flows"]}
    kept = set()
    for flow in capture.read_flows(pcap):
        if (flow.protocol, flow.a, flow.b) not in dropped and flow.from_its_start and not flow.oversize:
            kept.add((flow.protocol, flow.a, flow.b))
    with tempfile.TemporaryDirectory() as tmp:
        out, keys = Path(tmp) / "features.jsonl", Path(tmp) / "keys.jsonl"
        result = subprocess.run([EXTRACT, "-pcap", pcap, "-out", out, "-keys", keys,
                                 "-window-every", str(WINDOW_EVERY)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        wanted = set()
        for line in keys.read_text().splitlines():
            k = json.loads(line)
            a, b = f"{k['src_ip']}:{k['src_port']}", f"{k['dst_ip']}:{k['dst_port']}"
            if (k["protocol"], a, b) in kept or (k["protocol"], b, a) in kept:
                wanted.add(k["flow_id"])
        rows, left_out = [], Counter()
        for line in out.read_text().splitlines():
            record = json.loads(line)
            if record["flow_id"] not in wanted:
                left_out["background flow"] += record["kind"] == "initial"
                continue
            rows.append({**record, "label": session["label"], "activity": session["activity"],
                         "session": f"own/{session['session']}", "source": "own-captures"})
    return rows, left_out


# Our own captures are the only data from a real home line and today's
# applications, so the test part is served first.
_DEAL_OWN = ["test", "train", "validation", "test", "calibration", "train"]


def assign_splits(rows, seed=1):
    """Gives every session one split and returns {session: split}.

    By session, never by flow: flows of one capture resemble each other,
    and dividing them would let the model see its test data. The test part
    takes real captures only, never testbed runs.
    """
    classes = defaultdict(set)
    own = defaultdict(set)
    testbed = set()
    for row in rows:
        if row["source"] == "testbed":
            testbed.add(row["session"])
        elif row["source"] == "own-captures":
            own[row["label"]].add(row["session"])
        else:
            classes[row["label"]].add(row["session"])
    split = {}
    for cls in sorted(own):
        for i, session in enumerate(sorted(own[cls], key=lambda s: _order(s, seed))):
            split.setdefault(session, _DEAL_OWN[i % len(_DEAL_OWN)])
    for cls in sorted(classes):
        for i, session in enumerate(sorted(classes[cls], key=lambda s: _order(s, seed))):
            split.setdefault(session, _DEAL[i % len(_DEAL)])
    for i, session in enumerate(sorted(testbed, key=lambda s: _order(s, seed))):
        split[session] = ("train", "train", "train", "validation", "train", "calibration", "train")[i % 7]
    return split


def summarise(rows):
    """{class: {split: [sessions, flows, window records]}}."""
    table = defaultdict(lambda: defaultdict(lambda: [set(), 0, 0]))
    for row in rows:
        cell = table[row["label"]][row["split"]]
        cell[0].add(row["session"])
        cell[1 if row["kind"] == "initial" else 2] += 1
    return {cls: {s: [len(c[0]), c[1], c[2]] for s, c in parts.items()} for cls, parts in table.items()}


def problems(rows):
    """Why the dataset is not ready, in the build plan's words: every class
    needs flows from more than one session, and no session may sit in two
    splits."""
    found = []
    sessions = defaultdict(set)
    splits_of = defaultdict(set)
    for row in rows:
        sessions[row["label"]].add(row["session"])
        splits_of[row["session"]].add(row["split"])
    from ..evaluate import CLASSES
    for cls in CLASSES:
        if len(sessions[cls]) < 2:
            found.append(f"{cls}: flows from {len(sessions[cls])} session(s), need more than one")
    for session, parts in splits_of.items():
        if len(parts) > 1:
            found.append(f"session {session} is in {sorted(parts)}")
    return found


def build(flow_tables, run_dirs, out_dir, name="v1", seed=1, report=print, own_sessions=()):
    """Writes <name>.jsonl.gz (train, calibration, validation), and
    <name>.test.jsonl.gz, which is written once and then left alone until
    the report. Returns the summary that is also written beside them."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, left_out = [], Counter()
    for table in flow_tables:
        got, why = public_rows(table)
        report(f"{table}: {sum(r['kind'] == 'initial' for r in got)} flows, {len(got)} records")
        rows += got
        left_out += why
    runs = 0
    for run_dir in run_dirs:
        got, why = testbed_rows(run_dir)
        rows += got
        left_out += why
        runs += bool(got)
    for session_file in own_sessions:
        got, why = own_rows(session_file)
        if got:
            report(f"{session_file}: {sum(r['kind'] == 'initial' for r in got)} flows, {len(got)} records")
        rows += got
        left_out += why
    if run_dirs:
        report(f"{runs} testbed runs: {sum(r['kind'] == 'initial' and r['source'] == 'testbed' for r in rows)} flows")

    split = assign_splits(rows, seed)
    for row in rows:
        row["split"] = split[row["session"]]

    test_path = out_dir / f"{name}.test.jsonl.gz"
    test_rows = [r for r in rows if r["split"] == "test"]
    test_hash = hashlib.sha256("\n".join(sorted({r["session"] for r in test_rows})).encode()).hexdigest()
    meta_path = out_dir / f"{name}.json"
    if test_path.exists() and meta_path.exists():
        old = json.loads(meta_path.read_text())
        if old["test_sessions_sha256"] != test_hash:
            raise RuntimeError(
                f"{test_path} exists and was built from different sessions. The test set is written once: "
                f"build under a new name, or delete it on purpose.")
    else:
        with gzip.open(test_path, "wt") as f:
            for row in test_rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
    with gzip.open(out_dir / f"{name}.jsonl.gz", "wt") as f:
        for row in rows:
            if row["split"] != "test":
                f.write(json.dumps(row, separators=(",", ":")) + "\n")

    extractor = subprocess.run(["sha256sum", EXTRACT], capture_output=True, text=True).stdout.split()[0]
    meta = {
        "name": name, "seed": seed, "rows": len(rows),
        "extractor_sha256": extractor, "window_every_s": WINDOW_EVERY,
        "sources": dict(Counter(r["source"] for r in rows if r["kind"] == "initial")),
        "left_out": dict(left_out),
        "summary": summarise(rows),
        "sessions": {s: sorted(k for k, v in split.items() if v == s) for s in SPLITS},
        "test_sessions_sha256": test_hash,
        "problems": problems(rows),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def load(path, splits=None):
    """Reads dataset rows, optionally only some splits."""
    with gzip.open(path, "rt") as f:
        for line in f:
            row = json.loads(line)
            if splits is None or row["split"] in splits:
                yield row

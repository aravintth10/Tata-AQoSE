import gzip
import importlib.util
import json
import subprocess

import pytest
import yaml
from jsonschema import Draft202012Validator

from aqos_ml.dataset import build, labels, scenarios
from aqos_ml.evaluate import CLASSES

from harness import REPO
from test_extract import frame, write_pcap

SCENARIO_SCHEMA = Draft202012Validator(json.loads((REPO / "contracts/schema/scenario.schema.json").read_text()))
RECORD = Draft202012Validator(json.loads((REPO / "contracts/schema/feature_record.schema.json").read_text()))


@pytest.fixture(scope="module", autouse=True)
def extractor():
    subprocess.run(["make", "build"], cwd=REPO, check=True)


def test_file_names_map_to_classes():
    cases = {
        "skype_video1a.pcap": ("video_call", "multimedia_conferencing"),
        "facebook_audio2b.pcapng": ("voice_call", "telephony"),
        "hangouts_audio1a.pcap": ("voice_call", "telephony"),
        "voipbuster_4b.pcap": ("voice_call", "telephony"),
        "netflix1.pcap": ("video_streaming", "multimedia_streaming"),
        "youtubeHTML5_1.pcap": ("video_streaming", "multimedia_streaming"),
        "spotify3.pcap": ("audio_streaming", "multimedia_streaming"),
        # A file sent over Skype is a transfer, not a call or a chat.
        "skype_file3.pcap": ("file_transfer", "high_throughput_data"),
        "scpDown1.pcap": ("file_transfer", "high_throughput_data"),
        "ftps_up_2a.pcap": ("file_transfer", "high_throughput_data"),
        "skype_chat1a.pcap": ("chat", "standard"),
        "AIMchat1.pcapng": ("chat", "standard"),
        "email2b.pcap": ("email", "standard"),
    }
    for file, want in cases.items():
        assert labels.label_for("iscx-vpn-nonvpn-2016", file) == want, file
    assert labels.label_for("labayen-2020", "web_2page_01.pcap") == ("web_browsing", "standard")
    assert labels.label_for("labayen-2020", "bulk_xs_04.pcap") == ("bulk_download", "high_throughput_data")
    assert labels.label_for("labayen-2020", "idle_none_18.pcap") is None
    assert labels.label_for("unknown-set", "skype_video1a.pcap") is None
    for _, _, cls in labels._ISCX + labels._LABAYEN + labels._TOR:
        assert cls is None or cls in CLASSES

    tor = "iscx-tor-nontor-2016"
    assert labels.label_for(tor, "p2p_vuze.pcap") == ("p2p", "low_priority_data")
    assert labels.label_for(tor, "browsing_ara.pcap") == ("web_browsing", "standard")
    assert labels.label_for(tor, "SSL_Browsing.pcap") == ("web_browsing", "standard")
    assert labels.label_for(tor, "FTP_filetransfer.pcap") == ("file_transfer", "high_throughput_data")
    assert labels.label_for(tor, "Email_IMAP_filetransfer.pcap") == ("email", "standard")
    assert labels.label_for(tor, "Youtube_HTML5_Workstation.pcap") == ("video_streaming", "multimedia_streaming")
    # This set's calls were forced through TCP by its gateway: not used.
    assert labels.label_for(tor, "Skype_Audio.pcap") is None
    assert labels.label_for(tor, "Hangouts_voice_Workstation.pcap") is None
    # Copies of other files under a second name.
    assert labels.label_for(tor, "p2p_vuze2-1.pcap") is None
    assert labels.label_for(tor, "browsing2-2.pcap") is None
    assert labels.label_for(tor, "browsing2.pcap") is not None


def test_both_ends_of_a_call_are_one_session():
    a = labels.session_for("iscx-vpn-nonvpn-2016", "facebook_audio2a.pcap")
    b = labels.session_for("iscx-vpn-nonvpn-2016", "facebook_audio2b.pcapng")
    assert a == b == "iscx-vpn-nonvpn-2016/facebook_audio2"
    assert labels.session_for("iscx-vpn-nonvpn-2016", "skype_audio3.pcapng") != labels.session_for("iscx-vpn-nonvpn-2016", "skype_audio4.pcapng")
    # Not every trailing letter is an a/b pair.
    assert labels.session_for("x", "netflix1.pcap") == "x/netflix1"
    assert labels.session_for("x", "scpUp2.pcap") == "x/scpup2"
    # A set that reuses file names needs the folder to tell captures apart.
    one = labels.session_for("unsw", "ex1.pcapng", "context/pcap/xbox/edge/UNO/balanced/ex1")
    two = labels.session_for("unsw", "ex1.pcapng", "context/pcap/ios/safari/UNO/balanced/ex1")
    assert one != two and one == "unsw/context/pcap/xbox/edge/uno/balanced/ex1/ex1"


def test_only_the_activitys_own_flows_take_the_label():
    flow = lambda nbytes, port=3478: {"flow_bytes": nbytes, "src_port": 50000, "dst_port": port}  # noqa: E731
    assert labels.is_activity_flow("video_call", flow(9_000_000), 10_000_000)
    assert not labels.is_activity_flow("video_call", flow(100_000), 10_000_000), "a small flow beside a call"
    assert not labels.is_activity_flow("video_call", flow(9_000_000, port=53), 10_000_000)
    # Browsing and chat are many small flows; all count, except background ports.
    assert labels.is_activity_flow("web_browsing", flow(2_000, port=443), 10_000_000)
    assert not labels.is_activity_flow("chat", flow(2_000, port=5353), 10_000_000)


def rows_for(sessions):
    """sessions: (name, source, label) -> one initial row each."""
    return [{"session": name, "source": source, "label": label, "kind": "initial"} for name, source, label in sessions]


def test_splits_keep_sessions_whole_and_testbed_out_of_the_test_set():
    sessions = [(f"iscx/call{i}", "iscx", "telephony") for i in range(23)]
    sessions += [(f"iscx/video{i}", "iscx", "multimedia_conferencing") for i in range(4)]
    sessions += [(f"testbed/run{i}", "testbed", "telephony") for i in range(40)]
    rows = rows_for(sessions) * 3
    split = build.assign_splits(rows)
    assert set(split) == {name for name, _, _ in sessions}
    for row in rows:
        row["split"] = split[row["session"]]
    assert build.problems(rows)[:0] == [] and not any("is in" in p for p in build.problems(rows))
    assert not any(v == "test" for k, v in split.items() if k.startswith("testbed/"))
    # A class with four real sessions is in every part.
    assert sorted(split[f"iscx/video{i}"] for i in range(4)) == sorted(build.SPLITS)
    by = lambda part: sum(v == part for k, v in split.items() if k.startswith("iscx/call"))  # noqa: E731
    assert by("train") > by("validation") >= by("calibration") >= 1 and by("test") >= 2
    assert build.assign_splits(rows) == split, "the split must not change from one build to the next"
    assert build.assign_splits(rows, seed=2) != split


def test_problems_name_thin_classes_and_divided_sessions():
    rows = rows_for([("a", "iscx", "telephony"), ("b", "iscx", "telephony"), ("c", "iscx", "standard")])
    for row in rows:
        row["split"] = "train"
    found = build.problems(rows)
    assert "standard: flows from 1 session(s), need more than one" in found
    assert not any(p.startswith("telephony") for p in found)
    rows.append({**rows[0], "split": "test"})
    assert "session a is in ['test', 'train']" in build.problems(rows)


def test_training_scenarios_are_valid_and_repeatable():
    made = [scenarios.make(i) for i in range(30)]
    for scenario in made:
        assert [e.message for e in SCENARIO_SCHEMA.iter_errors(scenario)] == []
        assert {t["host"] for t in scenario["traffic"]} <= set(scenario["hosts"])
        assert len({t["id"] for t in scenario["traffic"]}) == len(scenario["traffic"])
    assert scenarios.make(7) == made[7]
    assert scenarios.make(7, seed=2) != made[7]
    assert len({s["id"] for s in made}) == 30
    assert any(len(s["traffic"]) == 1 for s in made), "some flows must be seen alone on an idle link"
    assert {t["profile"] for s in made for t in s["traffic"]} == set(scenarios.WEIGHTS)


def capture(tmp_path, name, packets):
    folder = tmp_path / "pcaps"
    folder.mkdir(exist_ok=True)
    write_pcap(folder / name, packets)
    return folder


def steady(port, seconds, up_bytes, down_bytes, host="192.168.1.20", far="203.0.113.9"):
    """25 packets up and 50 down in every second, from second 2. A lone
    packet at time zero shows the capture was running before the flow
    began, as a capture must be for the flow's first packets to count."""
    packets = [(0.0, frame(host, "192.168.1.1", 17, 5000 + port % 1000, 53, 70))]
    for second in range(2, seconds + 2):
        for n in range(25):
            packets.append((second + n / 25, frame(host, far, 17, port, 3478, up_bytes)))
        for n in range(50):
            packets.append((second + n / 50 + 0.002, frame(far, host, 17, 3478, port, down_bytes)))
    return packets


def flow_table(tmp_path, folder):
    """Runs the Kaggle script over a folder of captures."""
    spec = importlib.util.spec_from_file_location("flow_tables", REPO / "kaggle/pcap_flow_tables.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    out = script.Output(tmp_path / "tables", "iscx-vpn-nonvpn-2016")
    script.add_tree(out, folder, {})
    out.close()
    return tmp_path / "tables" / "flows-iscx-vpn-nonvpn-2016.jsonl.gz"


def test_public_data_goes_through_the_same_extractor(tmp_path):
    """A capture reduced to a flow table and then to records must match the
    same capture read directly: same first-packet features, and window
    features that agree."""
    folder = capture(tmp_path, "skype_video1a.pcap", steady(40000, 14, 300, 1100))
    table = flow_table(tmp_path, folder)
    rows, left_out = build.public_rows(table)
    direct = tmp_path / "direct.jsonl"
    subprocess.run([build.EXTRACT, "-pcap", folder / "skype_video1a.pcap", "-out", direct, "-window-every", "5"],
                   check=True, capture_output=True)
    direct = [json.loads(line) for line in direct.read_text().splitlines()]

    assert [r["kind"] for r in rows] == [r["kind"] for r in direct] == ["initial", "window", "window"]
    for ours, theirs in zip(rows, direct):
        record = {k: ours[k] for k in theirs}
        assert [e.message for e in RECORD.iter_errors(record)] == []
        assert ours["pkt_sizes"] == theirs["pkt_sizes"]
        # The flow table keeps times as microseconds from the flow's start,
        # so a gap can differ by one from the capture's own.
        assert ours["pkt_gaps_us"] == pytest.approx(theirs["pkt_gaps_us"], abs=1)
        assert (ours["src_port"], ours["dst_port"], ours["protocol"]) == (theirs["src_port"], theirs["dst_port"], 17)
    for ours, theirs in zip(rows[1:], direct[1:]):
        for name, value in theirs["window"].items():
            assert ours["window"][name] == pytest.approx(value, rel=0.02, abs=0.02), name
    assert rows[0]["label"] == "multimedia_conferencing" and rows[0]["activity"] == "video_call"
    assert rows[0]["session"] == "iscx-vpn-nonvpn-2016/skype_video1"
    assert left_out == {}


def test_flows_that_cannot_give_honest_features_are_left_out(tmp_path):
    # Already running when the capture began: its first packets are missing.
    late = [(t - 2, f) for t, f in steady(40001, 6, 300, 1100)[1:]]
    # A packet larger than any link carries among the first eight.
    merged = steady(40002, 6, 300, 1100)
    merged[2] = (merged[2][0], frame("203.0.113.9", "192.168.1.20", 17, 3478, 40002, 9000))
    # Fine at the start, merged packets later: first-packet features are
    # sound, per-second packet counts are not.
    later = steady(40003, 12, 300, 1100)
    later[400] = (later[400][0], frame("203.0.113.9", "192.168.1.20", 17, 3478, 40003, 9000))
    folder = capture(tmp_path, "skype_video2a.pcap", late + merged + later)
    table = flow_table(tmp_path, folder)
    out = tmp_path / "rows.jsonl"
    result = subprocess.run([build.EXTRACT, "-flow-table", table, "-out", out, "-window-every", "5"],
                            check=True, capture_output=True, text=True)
    assert "1 began before the capture" in result.stderr and "1 with merged first packets" in result.stderr
    assert "1 kept without window records" in result.stderr
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [(r["kind"], r["src_port"]) for r in rows] == [("initial", 40003)]


def fake_run(tmp_path, run_id, valid=True):
    """A run directory as the runner leaves it with the extractor on."""
    run = tmp_path / "results" / run_id
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"run_id": run_id, "valid": valid}))
    record = json.loads((REPO / "contracts/examples/feature_record.window.json").read_text())
    flows, keys, features = [], [], []
    for i, (profile, cls) in enumerate([("gaming", "realtime_interactive"), ("backup_upload", "low_priority_data"),
                                        ("cloud_gaming", "realtime_interactive")]):
        tup = {"protocol": 17, "src_ip": "10.77.0.10", "src_port": 40000 + i, "dst_ip": "10.78.0.10", "dst_port": 5000 + i}
        flows.append({**tup, "traffic_id": f"t{i}", "profile": profile, "true_class": cls})
        keys.append({**tup, "flow_id": f"{i:016x}"})
        features.append({**record, "flow_id": f"{i:016x}", "kind": "initial", "window": None})
        features.append({**record, "flow_id": f"{i:016x}"})
    # A flow the extractor saw that no generator opened, such as a ping.
    keys.append({"protocol": 17, "src_ip": "10.77.0.10", "src_port": 1, "dst_ip": "10.78.0.10", "dst_port": 2, "flow_id": "f" * 16})
    features.append({**record, "flow_id": "f" * 16, "kind": "initial", "window": None})
    for name, lines in (("flows.jsonl", flows), ("keys.jsonl", keys), ("features.jsonl", features)):
        (run / name).write_text("".join(json.dumps(line) + "\n" for line in lines))
    return run


def test_testbed_rows_take_their_label_from_the_generators(tmp_path):
    rows, left_out = build.testbed_rows(fake_run(tmp_path, "S1000-B2-s0001"))
    assert len(rows) == 6 and left_out == {"flow no generator opened": 1}
    assert {(r["label"], r["activity"]) for r in rows} == {
        ("realtime_interactive", "gaming"), ("low_priority_data", "backup_upload"), ("realtime_interactive", "cloud_gaming")}
    assert {r["session"] for r in rows} == {"testbed/S1000-B2-s0001"} and {r["source"] for r in rows} == {"testbed"}
    assert build.testbed_rows(fake_run(tmp_path, "S1001-B2-s0001", valid=False)) == ([], {"run was invalid": 1})


def test_build_writes_the_dataset_and_guards_the_test_set(tmp_path):
    names = [f"skype_video{i}a.pcap" for i in range(1, 5)] + [f"facebook_audio{i}a.pcap" for i in range(1, 5)]
    for i, name in enumerate(names):
        folder = capture(tmp_path, name, steady(41000 + i, 7, 200 + 10 * i, 900))
    table = flow_table(tmp_path, folder)
    runs = [fake_run(tmp_path, f"S10{i:02d}-B2-s0001") for i in range(4)]
    out = tmp_path / "dataset"
    meta = build.build([table], runs, out, report=lambda *a: None)

    assert meta["sources"] == {"iscx-vpn-nonvpn-2016": 8, "testbed": 12}
    assert meta["left_out"] == {"flow no generator opened": 4}
    rows = list(build.load(out / "v1.jsonl.gz"))
    test_rows = list(build.load(out / "v1.test.jsonl.gz"))
    assert {r["split"] for r in rows} == {"train", "calibration", "validation"} and {r["split"] for r in test_rows} == {"test"}
    assert len(rows) + len(test_rows) == meta["rows"]
    assert not {r["session"] for r in rows} & {r["session"] for r in test_rows}
    assert {r["source"] for r in test_rows} == {"iscx-vpn-nonvpn-2016"}
    assert all(r["label"] in CLASSES for r in rows + test_rows)
    assert len(list(build.load(out / "v1.jsonl.gz", splits={"train"}))) < len(rows)
    # Four of the seven classes have no data here, and the summary says so.
    assert len(meta["problems"]) == 3 and all("need more than one" in p for p in meta["problems"])
    assert json.loads((out / "v1.json").read_text())["test_sessions_sha256"] == meta["test_sessions_sha256"]

    # Building again from the same sessions is fine and leaves the test file alone.
    before = (out / "v1.test.jsonl.gz").stat().st_mtime_ns
    build.build([table], runs, out, report=lambda *a: None)
    assert (out / "v1.test.jsonl.gz").stat().st_mtime_ns == before
    # New sessions would change the test set: refused under the same name.
    capture(tmp_path, "skype_video9a.pcap", steady(42000, 7, 250, 900))
    capture(tmp_path, "skype_video8a.pcap", steady(42001, 7, 250, 900))
    bigger = flow_table(tmp_path, tmp_path / "pcaps")
    try:
        build.build([bigger], runs, out, report=lambda *a: None)
        changed = json.loads((out / "v1.json").read_text())["test_sessions_sha256"] != meta["test_sessions_sha256"]
        assert not changed, "the test set changed without anyone being told"
    except RuntimeError as exc:
        assert "written once" in str(exc)
    assert build.build([bigger], runs, out, name="v2", report=lambda *a: None)["name"] == "v2"

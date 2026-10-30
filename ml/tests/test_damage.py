"""Damage table scoring: from raw measurements to the table. The runs
themselves need the testbed; these check the arithmetic on made-up cells."""

import json

import pytest
import yaml
from jsonschema import Draft202012Validator

from aqos_ml import damage, evaluate
from aqos_ml.run import runner

SCORING = yaml.safe_load(damage.SCORING.read_text())
GOOD = {"latency_p95_ms": 20.0, "jitter_ms": 1.0, "loss_ratio": 0.0, "frame_miss_ratio": 0.0}


def call(**worse):
    return {"profile": "video_call", "class": "multimedia_conferencing",
            "up": dict(GOOD), "down": {**GOOD, **worse}}


def download(kbit):
    return {"profile": "bulk_download", "class": "high_throughput_data", "throughput_kbit": kbit}


def cell(profile, seed, tin, subject, others):
    return {"subject_profile": profile, "seed": seed, "tin": tin,
            "entries": {runner.SUBJECT: subject, **others}}


def test_ramp():
    assert damage.ramp(100, [150, 400]) == 0
    assert damage.ramp(275, [150, 400]) == pytest.approx(0.5)
    assert damage.ramp(900, [150, 400]) == 1
    assert damage.ramp(None, [150, 400]) == 1  # nothing arrived to be measured


def test_cost_is_the_worst_aspect_in_the_worse_direction():
    assert damage.cost(call(), SCORING) == 0
    assert damage.cost(call(latency_p95_ms=275), SCORING) == pytest.approx(0.5)
    assert damage.cost(call(latency_p95_ms=275, loss_ratio=0.10), SCORING) == 1
    silent = call()
    silent["down"] = {"latency_p95_ms": None, "jitter_ms": None, "loss_ratio": None}
    assert damage.cost(silent, SCORING) == 1


def test_cost_of_streaming_web_and_data():
    stream = {"profile": "abr_video", "stall_fraction": 0.0, "bitrate_kbit": 4000, "bitrate_top_kbit": 8000}
    assert damage.cost(stream, SCORING) == pytest.approx(0.25)  # half the bitrate, at half weight
    assert damage.cost({**stream, "stall_fraction": 0.05}, SCORING) == pytest.approx(0.5)
    page = {"profile": "web_browsing", "page_load_median_ms": 3000}
    assert damage.cost(page, SCORING) == pytest.approx(0.5)
    assert damage.cost({"profile": "web_browsing", "page_load_median_ms": None}, SCORING) == 1
    # Light pages: 900 ms is under every absolute threshold, and still
    # three times slower than usual.
    slow, usual = {"profile": "web_browsing", "page_load_median_ms": 900}, {"page_load_median_ms": 300}
    assert damage.cost(slow, SCORING) == 0
    assert damage.cost(slow, SCORING, usual) == pytest.approx((3 - 1.5) / 3.5)
    # A download is fine down to three quarters of its usual speed.
    assert damage.cost(download(60_000), SCORING, download(60_000)) == 0
    assert damage.cost(download(45_000), SCORING, download(60_000)) == 0
    assert damage.cost(download(22_500), SCORING, download(60_000)) == pytest.approx(0.5)
    assert damage.cost(download(90_000), SCORING, download(60_000)) == 0  # faster than usual earns nothing
    backup = {"profile": "backup_upload", "class": "low_priority_data", "throughput_kbit": 5_000}
    assert damage.cost(backup, SCORING, {"throughput_kbit": 10_000}) == 0  # half speed: what Bulk is for
    assert damage.cost({**backup, "throughput_kbit": 1_250}, SCORING, {"throughput_kbit": 10_000}) == pytest.approx(0.5)


def cells_for_a_download():
    """A download that is fastest in Voice, where it ruins the call beside
    it; slowed to a quarter in Bulk, where the call is untouched."""
    cells = {}
    for seed in (1, 2):
        cells["bulk_download", "alone", seed] = {
            "voice": cell("bulk_download", seed, "voice", download(90_000), {"call": call(latency_p95_ms=400)}),
            "video": cell("bulk_download", seed, "video", download(90_000), {"call": call(latency_p95_ms=275)}),
            "best_effort": cell("bulk_download", seed, "best_effort", download(80_000), {"call": call()}),
            "bulk": cell("bulk_download", seed, "bulk", download(20_000), {"call": call()}),
        }
    return cells


def test_table_adds_harm_to_self_damage():
    table, rows, by_link = damage.score(cells_for_a_download(), SCORING)
    assert by_link == {"alone": table}
    row = table["high_throughput_data"]
    # Raw: voice 0 + 1, video 0 + 0.5, best effort 0, bulk (0.75 - 0.25) / 0.75 + 0.
    assert min(row, key=row.get) == "best_effort"
    assert row["voice"] == 10 and row["best_effort"] == 0
    assert row["video"] == 5
    assert row["bulk"] == 6.5  # 6.67, to the table's resolution of a half
    voice = next(r for r in rows if r["tin"] == "voice")
    assert (voice["self_damage"], voice["harm"], voice["seeds"]) == (0, 1, 2)
    bulk = next(r for r in rows if r["tin"] == "bulk")
    assert (bulk["self_damage"], bulk["harm"]) == (pytest.approx(0.6667, abs=1e-4), 0)


def test_incomplete_seeds_do_not_count():
    cells = cells_for_a_download()
    del cells["bulk_download", "alone", 2]["bulk"]
    _, rows, _ = damage.score(cells, SCORING)
    assert {r["seeds"] for r in rows} == {1}


def test_harm_can_be_negative_and_the_table_is_not():
    """Moving a flow out of the way helps the others; the table still
    starts at zero for each class."""
    cells = cells_for_a_download()
    for by_tin in cells.values():
        by_tin["best_effort"]["entries"]["call"] = call(latency_p95_ms=200)
    table, rows, _ = damage.score(cells, SCORING)
    assert next(r for r in rows if r["tin"] == "bulk")["harm"] < 0
    assert min(table["high_throughput_data"].values()) == 0
    assert all(v >= 0 for v in table["high_throughput_data"].values())


def test_scenarios_are_valid_and_cover_every_class():
    schema = json.loads((evaluate.REPO / "contracts" / "schema" / "scenario.schema.json").read_text())
    classes = set()
    for profile, link in [(p, l) for p in damage.SUBJECTS for l in damage.CONDITIONS]:
        doc = damage.scenario(profile, link)
        assert damage.condition_of(doc["id"]) == link and doc["link"] == damage.CONDITIONS[link]["link"]
        # Company, when there is some, is on the subject's own device.
        company = [e for e in doc["traffic"] if e["host"] == "subject" and e["id"] != "subject"]
        assert len(company) == (0 if link in ("alone", "tight") else 1)
        assert not [e.message for e in Draft202012Validator(schema).iter_errors(doc)]
        for tin in damage.TINS:
            tins = runner.entry_tins(doc, tin)
            assert tins["subject"] == tin
            # Something else is in Video, Best Effort and Bulk, whatever the subject does.
            assert {t for tid, t in tins.items() if tid != "subject"} >= {"video", "best_effort", "bulk"}
        classes.add(evaluate.TAXONOMY["profiles"][profile])
    assert classes == set(evaluate.CLASSES)
    ids = {damage.scenario(p, l)["id"] for p in damage.SUBJECTS for l in damage.CONDITIONS}
    assert len(ids) == len(damage.SUBJECTS) * len(damage.CONDITIONS)


def test_forced_run_marks_each_entry_by_host_and_port():
    doc = damage.scenario("gaming")
    doc["traffic"][0]["id"] = "player"
    with pytest.raises(runner.RunError):
        runner.entry_tins(doc, "voice")
    # Two entries on one host, in different tins.
    doc = damage.scenario("gaming")
    doc["traffic"].append({"id": "update", "host": "subject", "profile": "game_update", "start_s": 0, "stop_s": None})
    tins = runner.entry_tins(doc, "voice")
    assert (tins["subject"], tins["update"]) == ("voice", "best_effort")
    hosts = [{"name": h, "ip4": f"10.77.0.{10 + i}", "ip6": f"fd77::{10 + i}"} for i, h in enumerate(doc["hosts"])]
    rules = runner.force_rules(doc, tins, hosts, seed=3)
    from aqos_ml.gen import plan
    ports = plan.allocate_ports(3, [e["id"] for e in doc["traffic"]])
    assert f"ip saddr 10.77.0.10 meta l4proto {{ tcp, udp }} th dport {ports['subject'][0]} ct mark set 4" in rules
    assert f"ip saddr 10.77.0.10 meta l4proto {{ tcp, udp }} th dport {ports['update'][0]} ct mark set 2" in rules
    assert rules.count("ct mark set") == 2 * len(doc["traffic"])  # IPv4 and IPv6 for each entry


def test_written_table_loads_as_a_damage_table(tmp_path):
    results = tmp_path / "results"
    for (profile, _, seed), by_tin in cells_for_a_download().items():
        for tin, c in by_tin.items():
            run_dir = results / f"{profile}-{tin}-{seed}"
            run_dir.mkdir(parents=True)
            (run_dir / "cell.json").write_text(json.dumps(c))
            (run_dir / "manifest.json").write_text(json.dumps({"valid": True}))
    with pytest.raises(SystemExit):  # one class of seven is not a table
        damage.write_table(results, tmp_path / "t.yaml", tmp_path / "m.csv")
    assert len(damage.load_cells(results)) == 2


def test_table_is_the_mean_of_its_conditions_on_one_scale():
    """On a crowded device the download in Voice ruins the call; alone it
    does nothing. The table says half of that, and the tables by condition
    can be read against each other. A condition outside the table is
    reported and not averaged in."""
    cells = {}
    for condition, hurt in (("alone", 20.0), ("crowded", 400.0), ("tight", 400.0)):
        cells["bulk_download", condition, 1] = {
            tin: cell("bulk_download", 1, tin, download(80_000),
                      {"call": call(latency_p95_ms=hurt if tin == "voice" else 20.0)}) for tin in damage.TINS}
    table, rows, by = damage.score(cells, SCORING)
    assert by["crowded"]["high_throughput_data"]["voice"] == 10
    assert by["alone"]["high_throughput_data"]["voice"] == 0
    assert table["high_throughput_data"]["voice"] == 5
    assert by["tight"]["high_throughput_data"]["voice"] == 10 and {r["condition"] for r in rows} == set(by)
    assert set(damage.COUNTED) == {"alone", "crowded", "uplink"} and "tight" not in damage.COUNTED
    # In the uplink condition the only call is the one on the subject's device.
    calls = [e for e in damage.scenario("backup_upload", "uplink")["traffic"] if e["profile"] == "video_call"]
    assert [(e["id"], e["host"]) for e in calls] == [("meeting", "subject")]


def test_old_cells_are_read_as_alone(tmp_path):
    for name, extra in (("a", {}), ("b", {"link": "roomy"}), ("c", {"link": "tight"}), ("d", {"condition": "crowded"})):
        run_dir = tmp_path / name
        run_dir.mkdir()
        (run_dir / "cell.json").write_text(json.dumps({"subject_profile": name, "seed": 1, "tin": "bulk", **extra}))
        (run_dir / "manifest.json").write_text(json.dumps({"valid": True}))
    assert sorted(damage.load_cells(tmp_path)) == [("a", "alone", 1), ("b", "alone", 1), ("c", "tight", 1), ("d", "crowded", 1)]


def test_a_flow_that_works_nowhere_is_left_out():
    broken = {**call(), "down": {"latency_p95_ms": 900.0, "jitter_ms": 1.0, "loss_ratio": 0.8, "frame_miss_ratio": 1.0}}
    cells = {("cloud_gaming", "tight", 1): {tin: cell("cloud_gaming", 1, tin, broken, {"call": call(latency_p95_ms=400 if tin == "bulk" else 20)})
                                            for tin in damage.TINS}}
    left_out = []
    table, rows, _ = damage.score(cells, SCORING, left_out)
    assert left_out == [("cloud_gaming", "tight", 1)] and rows == [] and table == {}

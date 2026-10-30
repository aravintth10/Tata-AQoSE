"""The dashboard file is what build.py produces, lays out without overlap,
and asks only for metrics and labels the contracts define."""

import importlib.util
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
spec = importlib.util.spec_from_file_location("dashboard_build", HERE / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

SCHEMA = json.loads((REPO / "contracts" / "schema" / "metric_sample.schema.json").read_text())
METRICS = set(SCHEMA["properties"]["metric"]["enum"])
LABELS = set(SCHEMA["properties"]) - {"run_id", "t_s", "source", "metric", "value"}
# Series the exporter adds itself, and the histogram's parts.
EXPORTER = {"aqos_run_info", "aqos_samples_read", "aqos_latency_ms_bucket"}


def panels():
    return json.loads(build.OUT.read_text())["panels"]


def test_committed_file_is_current():
    assert build.OUT.read_text() == build.render(), "run: python dashboards/build.py"


def test_layout():
    cells = set()
    for panel in panels():
        pos = panel["gridPos"]
        assert pos["x"] + pos["w"] <= build.WIDTH, panel["title"]
        for x in range(pos["x"], pos["x"] + pos["w"]):
            for y in range(pos["y"], pos["y"] + pos["h"]):
                assert (x, y) not in cells, f"{panel['title']} overlaps another panel"
                cells.add((x, y))
    ids = [p["id"] for p in panels()]
    assert len(ids) == len(set(ids))


def test_queries_use_only_what_the_contract_defines():
    seen = set()
    for panel in panels():
        for query in panel.get("targets", []):
            for name in re.findall(r"\baqos_\w+", query["expr"]):
                assert name in EXPORTER or name.removeprefix("aqos_") in METRICS, f"{panel['title']}: {name}"
                seen.add(name)
            for label in re.findall(r"\{\{(\w+)\}\}", query["legendFormat"]):
                assert label in LABELS | {"run_id"}, f"{panel['title']}: label {label}"
            for group in re.findall(r"by \(([^)]*)\)", query["expr"]):
                assert set(re.split(r",\s*", group)) <= LABELS | {"le"}, f"{panel['title']}: {group}"
    # The six metric families the specification asks to be live.
    for name in ("latency_ms_bucket", "jitter_ms", "loss_ratio", "throughput_kbit", "queue_delay_ms",
                 "queue_backlog_bytes", "shaper_rate_kbit", "capacity_true_kbit"):
        assert "aqos_" + name in seen, name


def test_exporter_down_is_visible():
    exporter = next(p for p in panels() if p["title"] == "Exporter")
    assert "vector(0)" in exporter["targets"][0]["expr"]  # a value even when the series is gone
    assert exporter["fieldConfig"]["defaults"]["mappings"][0]["options"]["0"]["text"] == "EXPORTER DOWN"


def test_every_panel_explains_itself():
    for panel in panels():
        if panel["type"] != "row":
            assert panel.get("description"), panel["title"]

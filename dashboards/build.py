"""Writes the Grafana dashboard, provisioning/dashboards/aqos.json.

    python dashboards/build.py

The dashboard is generated so that the panels stay uniform and a change to
one query is one line here. The JSON is committed too: Grafana loads it at
start, and `make check` fails if it is out of date with this file.

Every query reads `aqos_<metric>` as the exporter serves it (contracts
README, "Metrics"). Scrapes are 1 s apart, so rates are taken over 5 s.
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "provisioning" / "dashboards" / "aqos.json"
DATASOURCE = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
# Shown on every graph: what the controller did and why (chunk 6A posts
# these to Grafana's annotation API with these tags).
ACTION_TAGS = ["aqos"]
WIDTH = 24


def target(expr, legend, ref="A"):
    return {"datasource": DATASOURCE, "expr": expr, "legendFormat": legend, "refId": ref,
            "editorMode": "code", "range": True}


def graph(title, targets, unit, description, w=12, h=8, **custom):
    return {
        "type": "timeseries", "title": title, "description": description, "datasource": DATASOURCE,
        "gridPos": {"w": w, "h": h},
        "targets": [target(expr, legend, chr(ord("A") + i)) for i, (expr, legend) in enumerate(targets)],
        "fieldConfig": {
            "defaults": {
                "unit": unit, "min": 0,
                "custom": {"lineWidth": 1, "fillOpacity": 0, "showPoints": "never", "spanNulls": False, **custom},
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(title, expr, legend, description, w, mappings=(), thresholds=None, unit="none", text="value"):
    return {
        "type": "stat", "title": title, "description": description, "datasource": DATASOURCE,
        "gridPos": {"w": w, "h": 4},
        "targets": [{**target(expr, legend), "instant": True, "range": False}],
        "fieldConfig": {
            "defaults": {
                "unit": unit, "mappings": list(mappings), "noValue": "no data",
                "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "text", "value": None}]},
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "background" if thresholds else "none", "graphMode": "none", "textMode": text,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def row(title):
    return {"type": "row", "title": title, "collapsed": False, "gridPos": {"w": WIDTH, "h": 1}, "panels": []}


def quantile(q):
    return (f"histogram_quantile({q}, sum by (le, traffic_id, direction) "
            "(rate(aqos_latency_ms_bucket[5s])))")


PER_HOST = 'sum by (host) (aqos_throughput_kbit{direction="down"})'
ENTRY = "{{traffic_id}} {{direction}}"
TIN = "{{tin}} {{direction}}"

PANELS = [
    stat("Exporter", 'up{job="aqos"} or on() vector(0)', "", w=5,
         description="Red means Prometheus cannot reach aqos-exporter. Every graph below is then "
                     "stale or empty, and that says nothing about the network.",
         mappings=[{"type": "value", "options": {
             "0": {"text": "EXPORTER DOWN", "color": "red", "index": 0},
             "1": {"text": "live", "color": "green", "index": 1}}}],
         thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
    stat("Run", "aqos_run_info", "{{run_id}}", w=7, text="name",
         description="The run whose metrics.jsonl the exporter is following."),
    stat("Samples per second", "rate(aqos_samples_read[5s])", "", w=4, unit="short",
         description="Lines of metrics.jsonl read per second. Zero while the exporter is up "
                     "means no run is in progress.",
         thresholds=[{"color": "orange", "value": None}, {"color": "text", "value": 1}]),
    stat("Fairness (Jain's index)",
         f"sum({PER_HOST}) ^ 2 / (count({PER_HOST}) * sum(({PER_HOST}) ^ 2))", "", w=4, unit="none",
         description="1 means every device gets the same download throughput; 1/n means one device "
                     "gets everything. Devices ask for different amounts, so this is not expected "
                     "to be 1.",
         thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": 0.4},
                     {"color": "text", "value": 0.6}]),
    stat("Flows without a label", "aqos_flows_unlabelled", "", w=4, unit="short",
         description="Flows the controller has left to plain CAKE: too short, over the "
                     "classification cap, or the classifier did not answer. From chunk 6A on."),

    row("What the applications feel"),
    graph("Latency, 95th percentile", [(quantile(0.95), ENTRY)], "ms",
          "One-way delay of each traffic entry's packets over the last 5 s."),
    graph("Latency, median and 99th percentile",
          [(quantile(0.5), "p50 " + ENTRY), (quantile(0.99), "p99 " + ENTRY)], "ms",
          "The same delays at the middle and at the tail."),
    graph("Jitter", [("aqos_jitter_ms", ENTRY)], "ms", "RFC 3550 jitter per traffic entry.", w=8),
    graph("Loss", [("aqos_loss_ratio", ENTRY)], "percentunit", "Packets lost per second, per traffic entry.", w=8),
    graph("Late frames", [("aqos_frame_miss", ENTRY)], "short",
          "Frames that arrived after their deadline, per second (calls and games).", w=8),
    graph("Video stalls", [("aqos_video_stall_ms", "{{traffic_id}}")], "ms",
          "Time the streaming player spent stalled in each second.", w=8),
    graph("Page load time", [("aqos_page_load_ms", "{{traffic_id}}")], "ms",
          "Time from a page's first request to its last byte, latest page per entry.", w=8),
    graph("Video quality", [("aqos_video_bitrate_kbit", "{{traffic_id}}")], "kbits",
          "The bitrate rung the streaming player chose.", w=8, lineInterpolation="stepAfter"),
    graph("Throughput per device", [("sum by (host, direction) (aqos_throughput_kbit)", "{{host}} {{direction}}")],
          "kbits", "All of a device's traffic entries added up.", w=8),

    row("What CAKE is doing"),
    graph("Queue delay per tin", [("aqos_queue_delay_ms", TIN)], "ms",
          "CAKE's average delay for packets in each tin."),
    graph("Backlog per tin", [("aqos_queue_backlog_bytes", TIN)], "bytes",
          "Bytes waiting in each tin."),
    graph("Shaper rate against true capacity",
          [("aqos_shaper_rate_kbit", "shaper {{direction}}"), ("aqos_capacity_true_kbit", "link {{direction}}")],
          "kbits", "The rate CAKE shapes to, read back from CAKE, against the emulated link. The "
                   "shaper should sit just under the link.", lineInterpolation="stepAfter"),
    graph("Drops per tin", [("aqos_tin_drops", TIN)], "short", "Packets CAKE dropped in each tin, per second."),

    row("What the controller is doing"),
    {
        "type": "annolist", "title": "Action log", "gridPos": {"w": 12, "h": 9},
        "description": "Every change the safety shield accepted, clamped or rejected, with its reason. "
                       "The same entries are the markers on the graphs. From chunk 6A on.",
        "options": {"limit": 40, "tags": ACTION_TAGS, "onlyFromThisDashboard": False, "onlyInTimeRange": True,
                    "showTags": True, "showTime": True, "showUser": False, "navigateBefore": "10s",
                    "navigateAfter": "10s", "navigateToPanel": False},
    },
    graph("Labels", [("aqos_flows_labelled", "labelled"), ("aqos_flows_unlabelled", "unlabelled"),
                     ("aqos_reclassifications", "reclassified")], "short",
          "Flows with and without a label, and label changes per second.", w=6, h=9),
    graph("Cost", [("aqos_cpu_percent", "{{component}}")], "percent",
          "CPU used by each component, as a percentage of one core.", w=6, h=9),
]


def dashboard():
    panels, x, y, height = [], 0, 0, 0
    for number, panel in enumerate(PANELS, start=1):
        size = panel["gridPos"]
        if x + size["w"] > WIDTH:
            x, y, height = 0, y + height, 0
        panels.append({**panel, "id": number, "gridPos": {"x": x, "y": y, **size}})
        x, height = x + size["w"], max(height, size["h"])
    return {
        "uid": "aqos-live", "title": "Adaptive QoS: live run", "tags": ["aqos"], "schemaVersion": 41,
        "editable": False, "graphTooltip": 1, "timezone": "browser",
        "refresh": "1s", "time": {"from": "now-5m", "to": "now"},
        "timepicker": {"refresh_intervals": ["1s", "5s", "30s"]},
        "templating": {"list": [{
            "name": "DS_PROMETHEUS", "label": "Data source", "type": "datasource", "query": "prometheus",
            "current": {}, "hide": 2,
        }]},
        "annotations": {"list": [{
            "name": "Controller actions", "datasource": {"type": "grafana", "uid": "-- Grafana --"},
            "enable": True, "iconColor": "orange", "type": "tags",
            "target": {"type": "tags", "tags": ACTION_TAGS, "matchAny": True, "limit": 200},
        }]},
        "panels": panels,
    }


def render():
    return json.dumps(dashboard(), indent=2) + "\n"


if __name__ == "__main__":
    OUT.write_text(render())
    print(f"wrote {OUT}")

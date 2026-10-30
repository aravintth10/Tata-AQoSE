"""The whole pipeline in the real testbed: extractor, ML service, shield,
enforcer, CAKE. Replaces any running testbed, so it runs only when asked:
make testbed-check."""

import json
import os

import pytest
from jsonschema import Draft202012Validator

from aqos_ml import rules
from aqos_ml.run import runner, summary

from harness import REPO

pytestmark = pytest.mark.skipif(not os.environ.get("AQOS_INTEGRATION"), reason="set AQOS_INTEGRATION=1")
SCENARIO = REPO / "ml/tests/scenarios/s1_short.yaml"


def validator(name):
    return Draft202012Validator(json.loads((REPO / f"contracts/schema/{name}.schema.json").read_text()))


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def check_run(out, manifest, version):
    """What every controller run must show, whatever decides the labels."""
    assert manifest["valid"], manifest["invalid_reason"]
    assert [e.message for e in validator("run_manifest").iter_errors(manifest)] == []
    assert manifest["versions"]["model_version"] == version
    print("\n" + "\n".join(summary.summarise(out)))

    report = json.loads((out / "controller.json").read_text())
    print(json.dumps(report))
    assert report["model_version"] == version
    flows = report["flows"]
    assert flows["records"] >= 2 and flows["labelled"] >= 2  # the call and the download, at least
    assert flows["mismatch"] == flows["errors"] == 0
    assert report["extractor"]["overflows"] == 0

    # Every action is a contract proposal with a contract verdict.
    actions = lines(out / "actions.jsonl")
    proposal, verdict = validator("policy_proposal"), validator("policy_verdict")
    for action in actions:
        assert [e.message for e in proposal.iter_errors(action["proposal"])] == [], action
        assert [e.message for e in verdict.iter_errors(action["verdict"])] == [], action
    assert sum(a["outcome"] == "applied" for a in actions) == flows["labelled"]

    # The controller's samples follow the metric contract like everyone's.
    sample, mine = validator("metric_sample"), set()
    for s in lines(out / "metrics.jsonl"):
        if s["source"] == "controller":
            assert [e.message for e in sample.iter_errors(s)] == [], s
            mine.add(s["metric"])
    assert mine == {"flows_labelled", "flows_unlabelled", "reclassifications", "inference_us"}
    return actions, lines(out / "metrics.jsonl")


def test_rules_in_the_controller_send_the_download_to_bulk(tmp_path):
    """B4. A window record gives the rules a rate; the download's is far
    over the threshold, and CAKE's Bulk tin fills."""
    manifest = runner.run_one(SCENARIO, "B4", 7, results=tmp_path)
    out = tmp_path / manifest["run_id"]
    actions, metrics = check_run(out, manifest, rules.RULES_VERSION)
    applied = [a for a in actions if a["outcome"] == "applied"]
    assert {a["proposal"]["flow_label"]["decision_rule"] for a in applied} == {"rules"}
    moved = [a for a in applied if a["verdict"]["applied"]["tin"] == "bulk"]
    assert moved, "no flow was sent to Bulk"
    assert all(a["proposal"]["source"] == "reclassifier" for a in moved)  # only a window has a rate
    # CAKE obeyed: Bulk carried a queue after the label, and none before.
    first = min(a["t_s"] for a in moved)
    bulk = [(s["t_s"], s["value"]) for s in metrics
            if s["metric"] == "queue_backlog_bytes" and s.get("tin") == "bulk" and s.get("direction") == "down"]
    assert max(v for t, v in bulk if t > first + 1) > 0
    assert max((v for t, v in bulk if t < first - 1), default=0) == 0


def test_model_in_the_controller(tmp_path):
    """O2 and O1, with whichever trained model is there."""
    try:
        version = runner.model_version()
    except runner.RunError as exc:
        pytest.skip(str(exc))
    for config, rule in (("O2", "expected_damage"), ("O1", "top_label")):
        manifest = runner.run_one(SCENARIO, config, 7, results=tmp_path)
        actions, _ = check_run(tmp_path / manifest["run_id"], manifest, version)
        rules_used = {a["proposal"]["flow_label"]["decision_rule"] for a in actions}
        assert rules_used <= {rule, "fallback"} and rule in rules_used
        for action in actions[:6]:
            print(f"  {action['t_s']:6.1f} s  {action['outcome']:9} {action['proposal']['reason']}")


def test_default_rust_runtime_handles_capacity_drop_and_intent(tmp_path, monkeypatch):
    """Exercise probes, rate enforcement, the intent HTTP API and verification together."""
    import yaml
    monkeypatch.delenv("AQOS_RUNTIME", raising=False)
    monkeypatch.delenv("AQOS_SERVICE", raising=False)
    scenario = yaml.safe_load(SCENARIO.read_text())
    scenario.update(id="S902", name="Rust capacity and intent integration", duration_s=32, warmup_s=4)
    scenario["traffic"][1]["start_s"] = 4
    scenario["events"] = [
        {"at_s": 6, "type": "intent_lease", "args": {"host": "laptop", "minutes": 1}},
        {"at_s": 10, "type": "link_rate", "args": {"direction": "down", "rate_kbit": 20000}},
    ]
    path = tmp_path / "capacity-intent.yaml"
    path.write_text(yaml.safe_dump(scenario))
    manifest = runner.run_one(path, "O2", 19, results=tmp_path / "runs")
    assert manifest["valid"], manifest["invalid_reason"]
    out = tmp_path / "runs" / manifest["run_id"]
    env = json.loads((out / "environment.json").read_text())
    assert env["runtime"] == env["service"] == "rust"
    report = json.loads((out / "controller.json").read_text())
    assert report["flows"]["errors"] == report["flows"]["mismatch"] == report["extractor"]["overflows"] == 0
    assert report["flows"]["rate_changes"] > 0
    actions = lines(out / "actions.jsonl")
    for action in actions:
        validator("policy_proposal").validate(action["proposal"])
        validator("policy_verdict").validate(action["verdict"])
    cuts = [a for a in actions if a["outcome"] == "applied"
            and a["proposal"]["kind"] == "shaper_rate"
            and a["proposal"]["shaper_rate"]["direction"] == "down"
            and a["verdict"]["applied"]["rate_kbit"] < 50000]
    assert cuts, "capacity drop never reached the shaper"
    lease_events = [line for line in (out / "runner.log").read_text().splitlines() if "intent_lease" in line]
    assert len(lease_events) == 1 and '"id"' in lease_events[0], lease_events
    assert lines(out / "verifier.jsonl"), "risky changes were never verified"

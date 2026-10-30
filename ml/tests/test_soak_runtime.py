"""Keep soak failures from being reported as successful long-run validation."""
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("soak_runtime", REPO / "scripts/soak-runtime.py")
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)


@pytest.mark.parametrize("seconds", [60, 21600])
def test_scenario_has_capacity_changes_within_run(seconds):
    scenario = soak.scenario(seconds)
    Draft202012Validator(json.loads((REPO / "contracts/schema/scenario.schema.json").read_text())).validate(scenario)
    assert scenario["duration_s"] == seconds
    assert {e["args"]["rate_kbit"] for e in scenario["events"]} == {20000, 100000}
    assert all(0 < e["at_s"] < seconds + scenario["warmup_s"] for e in scenario["events"])


def test_missing_evidence_and_processes_cannot_pass(tmp_path):
    reasons = soak.outcome(tmp_path, 23, {})
    assert any("missing or invalid run evidence" in r for r in reasons)
    assert sum("no resource samples" in r for r in reasons) == len(soak.RUNTIMES)


def test_errors_watchdog_and_restarts_fail_even_with_valid_manifest(tmp_path):
    run = tmp_path / "runs" / "S903-O2-s0023"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"valid": True}))
    (run / "environment.json").write_text(json.dumps({"runtime": "rust", "service": "rust"}))
    flows = dict.fromkeys(["errors", "mismatch", "service_down", "timeouts", "service_busy"], 0)
    flows.update(errors=1, labelled=5, rate_changes=2)
    (run / "controller.json").write_text(json.dumps({"flows": flows, "extractor": {"overflows": 0}}))
    (run / "heartbeat.safe").touch()
    resources = {name: {"process_identities": 1} for name in soak.RUNTIMES}
    resources["aqosd-rs"]["process_identities"] = 2
    reasons = soak.outcome(tmp_path, 23, resources)
    assert any("controller errors: 1" in r for r in reasons)
    assert any("watchdog entered safe state" in r for r in reasons)
    assert any("changed process identity" in r for r in reasons)


def test_resource_summary_detects_pid_reuse(tmp_path):
    path = tmp_path / "resources.jsonl"
    path.write_text("\n".join(json.dumps({"processes": [{
        "binary": "aqosd-rs", "pid": 42, "start_ticks": start,
        "rss_bytes": rss, "fds": fds}]}) for start, rss, fds in [(1, 4096, 8), (2, 8192, 9)]))
    result = soak.resource_summary(path)["aqosd-rs"]
    assert result["process_identities"] == 2
    assert result["rss_peak_bytes"] == 8192
    assert result["fd_peak"] == 9


def test_low_disk_refuses_before_launch(tmp_path, monkeypatch):
    from collections import namedtuple
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(soak.shutil, "disk_usage", lambda path: Usage(100, 100, 0))
    monkeypatch.setattr(soak.subprocess, "Popen", lambda *a, **kw: pytest.fail("launched on full disk"))
    with pytest.raises(SystemExit):
        soak.main(["--results", str(tmp_path / "new"), "--seconds", "60"])
    assert not (tmp_path / "new").exists()


def test_process_sampling_follows_wrappers_but_excludes_other_runs(tmp_path):
    def process(pid, parent, binary):
        directory = tmp_path / str(pid)
        directory.mkdir()
        fields = ["0"] * 22
        fields[0], fields[1] = "S", str(parent)
        fields[11], fields[12], fields[19], fields[21] = "10", "20", "123", "2"
        (directory / "stat").write_text(f"{pid} (name with ) spaces) " + " ".join(fields))
        (directory / "exe").symlink_to(binary)
        (directory / "fd").mkdir()
        (directory / "fd" / "0").touch()
    binary = tmp_path / "aqosd-rs"
    binary.touch()
    wrapper = tmp_path / "aqos-testbed"
    wrapper.touch()
    process(10, 1, wrapper)
    process(20, 10, wrapper)
    process(30, 20, binary)
    process(40, 1, binary)
    rows = soak.sample_processes(10, tmp_path)
    assert len(rows) == 1
    assert rows[0]["pid"] == 30
    assert rows[0]["start_ticks"] == 123
    assert rows[0]["fds"] == 1


def test_spawn_failure_is_saved_as_failure(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("injected process launch failure")
    monkeypatch.setattr(soak.subprocess, "Popen", fail)
    out = tmp_path / "failed"
    assert soak.main(["--results", str(out), "--seconds", "60"]) == 1
    report = json.loads((out / "soak.json").read_text())
    assert report["status"] == "failed"
    assert any("injected process launch failure" in reason for reason in report["problems"])

"""Opt-in differential tests for the real Rust and Go Unix-socket services."""
import contextlib
import copy
import gzip
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from aqos_ml.run import runner

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "internal/classify/testdata/fixture.json.gz"


def test_rust_backend_selection_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("AQOS_SERVICE", "rust")
    monkeypatch.delenv("AQOS_SERVE_ARGS", raising=False)
    monkeypatch.setattr(runner, "CLASSIFY_RUST", tmp_path / "missing-rust")
    with pytest.raises(runner.RunError, match="make build-rust"):
        runner.service_command("O2", tmp_path / "ml.sock", [], "missing")
    # The threshold-rules baseline has no Rust implementation.
    assert "aqos_ml.serve" in runner.service_command("B4", tmp_path / "ml.sock", ["--rules"], "rules")
    monkeypatch.setenv("AQOS_SERVE_ARGS", "--no-bulk-guard")
    with pytest.raises(runner.RunError, match="not supported"):
        runner.service_command("O2", tmp_path / "ml.sock", [], "missing")


def test_cannot_reuse_go_result_for_rust_trial(tmp_path, monkeypatch):
    run = runner._Run(REPO / "ml/tests/scenarios/s1_short.yaml", "O2", 7, 1, None, tmp_path)
    run.out.mkdir()
    run.manifest("2026-10-21T17:00:00Z", [])
    monkeypatch.setenv("AQOS_SERVICE", "rust")
    with pytest.raises(runner.RunError, match="separate --results"):
        runner.run_until_valid(REPO / "ml/tests/scenarios/s1_short.yaml", "O2", 7, results=tmp_path)


@contextlib.contextmanager
def service(binary, directory, model, mode="expected_damage"):
    path = directory / (binary.name + ".sock")
    with (directory / (binary.name + ".log")).open("w+") as log:
        proc = subprocess.Popen([str(binary), "-socket", str(path), "-model", str(model),
                                 "-mode", mode, "-damage", str(REPO / "configs/damage_table.yaml")],
                                cwd=REPO, stderr=log)
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(5)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    conn.connect(str(path))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if proc.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise AssertionError(log.read())
                    time.sleep(0.01)
            with conn.makefile("rwb", buffering=0) as stream:
                def request(value):
                    stream.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
                    return json.loads(stream.readline())
                yield request, conn, proc
        finally:
            conn.close()
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


integration = pytest.mark.skipif(not os.environ.get("AQOS_RUST_TEST"), reason="make test-rust builds both services")


@integration
@pytest.mark.parametrize("mode", ["expected_damage", "top_label"])
def test_full_service_matches_go_and_contracts(tmp_path, mode):
    with gzip.open(FIXTURE) as f:
        fixture = json.load(f)
    model = REPO / "models" / fixture["model_version"] / "trees.json"
    validators = {name: Draft202012Validator(json.loads((REPO / "contracts/schema" / f"{name}.schema.json").read_text()))
                  for name in ["classification", "policy_proposal"]}
    with service(REPO / "bin/go/aqos-classify", tmp_path, model, mode) as (go, _, _), \
            service(REPO / "bin/aqos-classify-rs", tmp_path, model, mode) as (rust, _, _):
        assert rust({"op": "health", "id": 0}) == go({"op": "health", "id": 0})
        for i, case in enumerate(fixture["cases"]):
            req = {"op": "classify", "id": i + 1, "record": case["record"],
                   "previous_tin": [None, "voice", "video", "best_effort", "bulk"][i % 5],
                   "leased_classes": [[], ["telephony"], ["realtime_interactive", "multimedia_conferencing"],
                                      ["standard"], ["telephony", "telephony"]][i % 5]}
            before = time.monotonic_ns()
            a, b = go(req), rust(req)
            assert a["ok"] and b["ok"], (a, b)
            assert b["id"] == req["id"]
            assert before <= b["proposal"]["ts_ns"] <= time.monotonic_ns()
            assert b["classification"]["probs"] == pytest.approx(a["classification"]["probs"], abs=1e-9)
            assert b["classification"]["confidence"] == pytest.approx(a["classification"]["confidence"], abs=1e-9)
            assert b["classification"]["flow_id"] == a["classification"]["flow_id"]
            assert b["classification"]["model_version"] == a["classification"]["model_version"]
            assert b["proposal"]["flow_label"] == a["proposal"]["flow_label"]
            assert b["proposal"]["source"] == a["proposal"]["source"]
            assert b["proposal"]["reason"] == a["proposal"]["reason"]
            validators["classification"].validate(b["classification"])
            validators["policy_proposal"].validate(b["proposal"])


@integration
def test_malformed_requests_fail_closed_and_service_recovers(tmp_path):
    with gzip.open(FIXTURE) as f:
        fixture = json.load(f)
    model = REPO / "models" / fixture["model_version"] / "trees.json"
    good = {"op": "classify", "id": 1, "record": fixture["cases"][0]["record"]}
    with service(REPO / "bin/aqos-classify-rs", tmp_path, model) as (request, conn, proc):
        bad = []
        for key, value in [("pkt_sizes", [1]*9), ("pkt_gaps_us", [-1]*7), ("schema_version", 2),
                           ("src_port", 65536), ("flow_id", "bad"), ("ip_version", 5), ("kind", "unknown")]:
            req = copy.deepcopy(good)
            req["record"][key] = value
            bad.append(req)
        bad.extend([{"op": "health"}, {"op": "classify", "id": 2}, {"op": "unknown", "id": 3}])
        for req in bad:
            response = request(req)
            assert not response["ok"] and "proposal" not in response
        assert request(good)["ok"]
        conn.sendall(b"x" * ((1 << 20) + 1) + b"\n")
        try:
            assert conn.recv(1) == b""
        except ConnectionResetError:
            pass
        assert proc.poll() is None


@integration
def test_model_validation_failure_never_binds_socket(tmp_path):
    invalid = tmp_path / "bad-model.json"
    invalid.write_text('{"features_version":1}')
    path = tmp_path / "never.sock"
    result = subprocess.run([str(REPO / "bin/aqos-classify-rs"), "-model", str(invalid), "-socket", str(path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and not path.exists()

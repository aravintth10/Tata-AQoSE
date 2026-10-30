import json

import pytest
import yaml
from jsonschema import Draft202012Validator

from aqos_ml.run import runner, validity

from harness import REPO

SCENARIO = yaml.safe_load((REPO / "ml/tests/scenarios/s1_short.yaml").read_text())
MANIFEST = Draft202012Validator(json.loads((REPO / "contracts/schema/run_manifest.schema.json").read_text()))
SCENARIO_SCHEMA = Draft202012Validator(json.loads((REPO / "contracts/schema/scenario.schema.json").read_text()))


def test_run_id_matches_the_contract_example():
    example = json.loads((REPO / "contracts/examples/run_manifest.json").read_text())
    assert runner.run_id("S1", "B2", 7) == example["run_id"]
    assert runner.run_id("S1", "B2", 7, attempt=2) == "S1-B2-s0007-a2"


def test_scenarios_are_valid():
    for path in [REPO / "ml/tests/scenarios/s1_short.yaml", *sorted((REPO / "scenarios").glob("*.yaml"))]:
        doc = yaml.safe_load(path.read_text())
        assert [e.message for e in SCENARIO_SCHEMA.iter_errors(doc)] == [], path
    assert runner.find_scenario("S1").name == "S1.yaml"
    with pytest.raises(runner.RunError):
        runner.find_scenario("S404")


def test_every_config_is_known_or_explained():
    taxonomy = json.loads((REPO / "contracts/taxonomy.json").read_text())
    assert set(runner.CONFIGS) | set(runner.NOT_YET) == set(taxonomy["configs"])
    for config in runner.CONFIGS.values():
        assert (REPO / config["script"]).exists()
    # A configuration that is not built is refused before anything is
    # touched. (This must stay a configuration that cannot run: a test here
    # that starts a real run takes the testbed away from whoever has it.)
    assert "B1" in runner.NOT_YET
    with pytest.raises(runner.RunError, match="chunk 8D"):
        runner.run_one("S1", "B1", 0)


def test_matrix_expands_seed_ranges():
    runs = runner.expand({"scenarios": ["S1"], "configs": ["B0", "B2"], "seeds": {"from": 3, "to": 5}})
    # One seed's configurations run back to back, so they see the same machine state.
    assert runs == [("S1", "B0", 3), ("S1", "B2", 3), ("S1", "B0", 4), ("S1", "B2", 4), ("S1", "B0", 5), ("S1", "B2", 5)]
    assert len(runner.expand({"scenarios": ["S1", "S2"], "configs": ["B0"], "seeds": [1, 2]})) == 4


def seconds(*ranges):
    return {s for a, b in ranges for s in range(a, b)}


def test_coverage_needs_samples_to_the_end():
    # call: 0 to 24, measured from 4. download: 8 to 24.
    full = {"call": seconds((0, 25)), "download": seconds((8, 25))}
    assert validity.coverage_problems(SCENARIO, full) == []
    died = {"call": seconds((0, 14)), "download": seconds((8, 25))}
    assert validity.coverage_problems(SCENARIO, died) == ["call: no samples between 14 s and 21 s"]
    gap = {"call": seconds((0, 9), (16, 25)), "download": seconds((8, 25))}
    assert validity.coverage_problems(SCENARIO, gap) == ["call: no samples between 9 s and 16 s"]
    assert validity.coverage_problems(SCENARIO, {"call": seconds((0, 25))}) == ["download: no samples at all"]
    # The warm-up is not judged.
    late = {"call": seconds((6, 25)), "download": seconds((10, 25))}
    assert validity.coverage_problems(SCENARIO, late) == []


def test_rate_must_match_the_scenario_after_events():
    assert validity.rate_problems(SCENARIO, {"up": 20000, "down": 100000}) == []
    assert validity.rate_problems(SCENARIO, {"up": 20000, "down": 50000}) != []
    assert validity.rate_problems(SCENARIO, {"up": 20000}) != []
    dropped = {**SCENARIO, "events": [{"at_s": 10, "type": "link_rate", "args": {"direction": "down", "rate_kbit": 20000}}]}
    assert validity.link_after_events(dropped) == {"up": 20000, "down": 20000}
    assert validity.rate_problems(dropped, {"up": 20000, "down": 20000}) == []
    assert validity.rate_problems(dropped, {"up": 20000, "down": 100000}) != []


def test_sample_seconds_survives_a_cut_line(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"t_s": 3.2, "traffic_id": "call", "metric": "x", "value": 1}\n'
                    '{"t_s": 4.0, "metric": "queue_delay_ms", "value": 1}\n'
                    '{"t_s": 5.9, "traffic_id": "call", "met')
    assert validity.sample_seconds(path) == {"call": {3}}


def test_manifest_follows_the_contract(tmp_path, monkeypatch):
    run = runner._Run(REPO / "ml/tests/scenarios/s1_short.yaml", "B2", 7, 1, None, tmp_path)
    run.out.mkdir()
    ok = run.manifest("2026-10-12T10:15:00+05:30", [])
    bad = run.manifest("2026-10-12T10:15:00+05:30", ["call client: no summary", "bottleneck down is 5 kbit"])
    for manifest in (ok, bad):
        assert [e.message for e in MANIFEST.iter_errors(manifest)] == []
    assert ok["valid"] and ok["invalid_reason"] is None
    assert not bad["valid"] and "no summary" in bad["invalid_reason"]
    assert json.loads((run.out / "manifest.json").read_text()) == bad


def test_valid_runs_are_skipped_and_invalid_ones_retried_once(tmp_path, monkeypatch):
    calls = []

    def fake_run(scenario, config, seed, attempt=1, tag=None, results=None):
        calls.append((config, seed, attempt))
        run = runner._Run(scenario, config, seed, attempt, tag, results)
        run.out.mkdir(parents=True, exist_ok=True)
        (run.out / "environment.json").write_text(json.dumps({"runtime": "rust"}))
        # Seed 2 fails its first attempt; seed 3 always fails.
        problems = ["generator died"] if (seed == 2 and attempt == 1) or seed == 3 else []
        return run.manifest("2026-10-12T10:15:00+05:30", problems)

    monkeypatch.setattr(runner, "run_one", fake_run)
    matrix = tmp_path / "m.yaml"
    matrix.write_text(yaml.safe_dump({"scenarios": [str(REPO / "ml/tests/scenarios/s1_short.yaml")],
                                      "configs": ["B2"], "seeds": [1, 2, 3]}))
    said = []
    report = lambda *a, **k: said.append(" ".join(map(str, a)))  # noqa: E731
    assert runner.run_matrix(matrix, tmp_path, report) is False
    assert calls == [("B2", 1, 1), ("B2", 2, 1), ("B2", 2, 2), ("B2", 3, 1), ("B2", 3, 2)]
    assert (tmp_path / "S901-B2-s0002-a2" / "manifest.json").exists()

    # Run again, as after an interruption: nothing valid is repeated, and a
    # run that used up its attempts is not tried a third time.
    calls.clear()
    runner.run_matrix(matrix, tmp_path, report)
    assert calls == []
    assert any("skip" in line and "S901-B2-s0002-a2" in line for line in said)
    assert any("gave up on S901-B2-s0003" in line for line in said)

    # A run that was killed leaves a directory without a manifest: it is run again in place.
    calls.clear()
    (tmp_path / "S901-B2-s0001" / "manifest.json").unlink()
    runner.run_matrix(matrix, tmp_path, report)
    assert calls == [("B2", 1, 1)]


def test_a_second_run_is_refused_while_one_has_the_testbed(tmp_path, monkeypatch):
    """Two runs at once would each tear the other's network down."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    first = runner.claim_testbed()
    try:
        with pytest.raises(runner.RunError, match="in use by run"):
            runner.claim_testbed()
        # And run_one gives up before it writes or starts anything.
        with pytest.raises(runner.RunError, match="in use"):
            runner.run_one("S1", "B2", 0, results=tmp_path / "results")
        assert not (tmp_path / "results").exists()
    finally:
        first.close()
    runner.claim_testbed().close()  # free again once the first has gone


def test_table_puts_configurations_side_by_side(tmp_path):
    """Two made-up runs of one scenario: the table shows each entry under
    each configuration, with a cost, and judges a download against B2."""
    from aqos_ml.run import table
    doc = yaml.safe_load((REPO / "scenarios" / "S1.yaml").read_text())
    for config, latency, mbit in (("B2", 12.0, 80.0), ("B4", 900.0, 20.0)):
        run_dir = tmp_path / f"S1-{config}-s0001"
        (run_dir / "gen").mkdir(parents=True)
        (run_dir / "scenario.yaml").write_text(yaml.safe_dump(doc))
        (run_dir / "manifest.json").write_text(json.dumps({
            "valid": True, "scenario_id": "S1", "seed": 1, "config": config, "warmup_s": 20}))
        lines = []
        for t in range(20, 200):
            base = {"run_id": "x", "t_s": t + 0.5, "source": "generator", "traffic_id": "call"}
            for d in ("up", "down"):
                lines.append({**base, "metric": "latency_ms", "value": latency, "direction": d})
                lines.append({**base, "metric": "jitter_ms", "value": 1.0, "direction": d})
                lines.append({**base, "metric": "loss_ratio", "value": 0.0, "direction": d})
                lines.append({**base, "metric": "frame_miss", "value": 0.0, "direction": d})
            lines.append({"run_id": "x", "t_s": t + 0.5, "source": "generator", "traffic_id": "download",
                          "metric": "throughput_kbit", "value": mbit * 1000, "direction": "down"})
        (run_dir / "metrics.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        (run_dir / "gen" / "call.client.json").write_text(json.dumps({"params": {"fps": 30}}))
        (run_dir / "gen" / "download.client.json").write_text(json.dumps({"params": {}}))
    text = table.markdown(tmp_path)
    assert "| Entry | Measure | B2 | B4 |" in text
    assert "| call (video_call) | latency p95 ms | 12.0 | 900.0 |" in text
    assert "| call (video_call) | **cost** | 0.00 | 1.00 |" in text
    assert "| download (bulk_download) | throughput Mbit/s | 80.0 | 20.0 |" in text
    assert "| download (bulk_download) | **cost** | 0.00 | 0.67 |" in text  # a quarter of its usual speed
    assert "| *all entries* | **summed cost** | **0.00** | **1.67** |" in text


def test_classifier_configurations_default_to_rust(monkeypatch):
    from aqos_ml.run import runner
    monkeypatch.delenv("AQOS_SERVICE", raising=False)
    monkeypatch.delenv("AQOS_SERVE_ARGS", raising=False)
    version = (runner.MODELS / "chosen-v2.txt").read_text().strip()
    if not (runner.MODELS / version / "trees.json").exists() or not runner.CLASSIFY_RUST.exists():
        pytest.skip("trees.json or bin/aqos-classify missing: python -m aqos_ml.export; make build")
    o1 = runner.service_command("O1", "s.sock", ["--model", version, "--mode", "top_label"], version)
    assert o1[0] == runner.CLASSIFY_RUST and o1[o1.index("-mode") + 1] == "top_label"
    rules = runner.service_command("B4", "s.sock", ["--rules"], "qosify-rules-v1")
    assert "aqos_ml.serve" in rules
    monkeypatch.setenv("AQOS_SERVE_ARGS", "--feature-noise 1.0")
    with pytest.raises(runner.RunError, match="not supported"):
        runner.service_command("O2", "s.sock", ["--model", version], version)
    monkeypatch.setenv("AQOS_SERVICE", "python")
    assert "aqos_ml.serve" in runner.service_command("O2", "s.sock", ["--model", version], version)


def test_rust_runtime_selects_all_router_processes_but_keeps_testbed(monkeypatch, tmp_path):
    monkeypatch.delenv("AQOS_RUNTIME", raising=False)  # Rust must be the default.
    monkeypatch.setenv("AQOS_RUNTIME_DIR", str(tmp_path))
    for binary in [runner.AQOSD, runner.EXTRACT, runner.CTL, runner.CAKE_READER,
                   runner.REFLECTOR, runner.WATCHDOG]:
        assert runner.runtime_binary(binary) == tmp_path / (binary.name + "-rs")
    assert runner.runtime_binary(runner.TESTBED) == runner.TESTBED
    monkeypatch.setenv("AQOS_RUNTIME", "go")
    assert runner.runtime_binary(runner.AQOSD) == REPO / "bin/go/aqosd"
    monkeypatch.setenv("AQOS_RUNTIME", "unknown")
    with pytest.raises(runner.RunError, match="unknown AQOS_RUNTIME"):
        runner.runtime_binary(runner.AQOSD)


def test_rust_runtime_cannot_reuse_cached_go_run(monkeypatch, tmp_path):
    run_id = "S901-O2-s0007"
    directory = tmp_path / run_id
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({"run_id": run_id, "valid": True}))
    (directory / "environment.json").write_text(json.dumps({"runtime": "go", "service": "rust"}))
    monkeypatch.setenv("AQOS_RUNTIME", "rust")
    with pytest.raises(runner.RunError, match="different runtime backend"):
        runner.run_until_valid("ml/tests/scenarios/s1_short.yaml", "O2", 7, results=tmp_path)


def test_missing_default_classifier_is_refused_before_touching_testbed(monkeypatch, tmp_path):
    monkeypatch.delenv("AQOS_RUNTIME", raising=False)
    monkeypatch.delenv("AQOS_SERVICE", raising=False)
    monkeypatch.delenv("AQOS_SERVE_ARGS", raising=False)
    monkeypatch.setattr(runner, "CLASSIFY_RUST", tmp_path / "absent")
    def unexpected(*args, **kwargs):
        pytest.fail("service preflight must precede testbed ownership or network changes")
    monkeypatch.setattr(runner, "claim_testbed", unexpected)
    monkeypatch.setattr(runner, "_testbed", unexpected)
    with pytest.raises(runner.RunError, match="Rust classifier"):
        runner.run_one("ml/tests/scenarios/s1_short.yaml", "O2", 7, results=tmp_path / "runs")
    assert not (tmp_path / "runs").exists()

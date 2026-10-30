"""Classifier pipeline on a small made-up dataset: features, training,
calibration, the artifact, and the service in front of it. Whether the
model is any good is a question for the real dataset; this checks that the
machinery is right."""

import gzip
import json
import socket
import threading

import numpy as np
import pytest
from jsonschema import Draft202012Validator

from aqos_ml import decide, evaluate, features, model, rules, serve, train

SCHEMA = evaluate.REPO / "contracts" / "schema"
EXAMPLE = json.loads((evaluate.REPO / "contracts" / "examples" / "feature_record.initial.json").read_text())


def schema_errors(name, doc):
    validator = Draft202012Validator(json.loads((SCHEMA / f"{name}.schema.json").read_text()))
    return [e.message for e in validator.iter_errors(doc)]


def record(rng, cls, flow, session, kind="initial"):
    """A feature record whose first packet's size gives its class away."""
    size = 100 + 150 * evaluate.CLASSES.index(cls) + int(rng.integers(0, 60))
    sizes = [size] + [int(s) for s in rng.choice([-1, 1], 7) * rng.integers(40, 1400, 7)]
    window = None
    if kind == "window":
        window = {"samples": 3, "pkts_up_per_s": float(size), "pkts_down_per_s": 10.0, "bytes_up_per_s": 1e4,
                  "bytes_down_per_s": 2e4, "mean_pkt_size_up": 100.0, "mean_pkt_size_down": 900.0,
                  "pkt_size_spread": 5.0, "rate_cv": 0.1, "burstiness": 1.2}
    return {
        "schema_version": 1, "flow_id": f"{flow:016x}", "host_id": "a1b2c3d4e5f60718", "kind": kind,
        "ts_ns": 1000, "ip_version": 4, "protocol": 17, "src_port": 51820, "dst_port": 443,
        "pkt_sizes": sizes, "pkt_gaps_us": [int(g) for g in rng.integers(100, 20000, 7)], "window": window,
        "label": cls, "session": session, "source": "made-up",
    }


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    out = tmp_path_factory.mktemp("model")
    rng, flow = np.random.default_rng(3), 0
    path = out / "tiny.jsonl.gz"
    with gzip.open(path, "wt") as f:
        for split, flows in (("train", 60), ("calibration", 25), ("validation", 25), ("test", 5)):
            for cls in evaluate.CLASSES:
                for _ in range(flows):
                    flow += 1
                    for kind in ("initial", "window"):
                        f.write(json.dumps({**record(rng, cls, flow, f"{split}/{cls}", kind), "split": split}) + "\n")
    lines = []
    version, written = train.train(path, out / "models", seed=1, jobs=1, trials=2, report=lines.append)
    return {"dir": out / "models", "version": version, "written": written, "log": lines, "dataset": path}


def test_feature_vector():
    assert len(features.NAMES) == 28 == len(set(features.NAMES))
    v = features.vector(EXAMPLE)
    assert v[:8] == EXAMPLE["pkt_sizes"] and v[8:15] == EXAMPLE["pkt_gaps_us"]
    assert v[15:19] == [4, 17, 2, 1]  # 51820 ephemeral, 3478 registered: classes, never the numbers
    assert all(np.isnan(v[19:]))  # no window: missing, not zero
    assert [features.port_class(p) for p in (0, 1023, 1024, 49151, 49152, 65535)] == [0, 0, 1, 1, 2, 2]
    assert features.matrix([]).shape == (0, 28)
    for name in ("host_id", "flow_id", "src_port", "dst_port", "ts_ns"):
        assert name not in features.NAMES


def test_weights_count_each_flow_and_each_class_once():
    rows = ([{"session": "a", "flow_id": "f1", "label": "standard"}] * 9
            + [{"session": "a", "flow_id": "f2", "label": "standard"}]
            + [{"session": "b", "flow_id": "f1", "label": "telephony"}] * 2)
    w = train.weights(rows)
    assert w[:9].sum() == pytest.approx(w[9])        # nine records of one flow weigh what one flow does
    assert w[:10].sum() == pytest.approx(w[10:].sum())  # each class the same
    assert w.sum() == pytest.approx(len(rows))


def test_training_writes_a_complete_artifact(trained):
    assert len(trained["written"]) == 2  # both candidates are kept; one is chosen
    chosen = [m for m in trained["written"].values() if m["chosen"]]
    assert len(chosen) == 1 and chosen[0]["model_version"] == trained["version"]
    directory = trained["dir"] / trained["version"]
    meta = json.loads((directory / "meta.json").read_text())
    assert meta["features"] == features.NAMES and meta["classes"] == evaluate.CLASSES
    assert meta["dataset"]["sha256"] == train.sha256(trained["dataset"])
    assert meta["calibration"] in train.CALIBRATIONS and meta["model_bytes"] > 0
    assert meta["validation"]["accuracy"] > 0.9  # the made-up classes are easy
    assert "test" not in meta["dataset"]["records"]  # the test split is never read
    envelopes = json.loads((directory / "envelopes.json").read_text())
    assert set(envelopes) == set(evaluate.CLASSES)
    low, high = envelopes["telephony"]["pkts_up_per_s"]
    assert 100 <= low <= high <= 160
    assert (trained["dir"] / "chosen-tiny.txt").read_text().strip() == trained["version"]


def test_loaded_model_follows_the_classification_contract(trained):
    loaded = model.load(trained["version"], trained["dir"])
    rng = np.random.default_rng(9)
    rows = [record(rng, cls, i, "s") for i, cls in enumerate(evaluate.CLASSES * 3)]
    many = loaded.classify_many(rows)
    for row, classification in zip(rows, many):
        assert schema_errors("classification", classification) == []
        assert sum(classification["probs"].values()) == pytest.approx(1, abs=1e-6)
        assert classification["flow_id"] == row["flow_id"]
        assert classification["model_version"] == trained["version"]
    one = loaded.classify(rows[0])
    assert one["probs"] == pytest.approx(many[0]["probs"])  # alone or in a batch, the same answer
    assert sum(max(c["probs"], key=c["probs"].get) == r["label"] for c, r in zip(many, rows)) >= 19
    assert loaded.classify_many([]) == []


def test_model_trained_on_other_features_is_refused(trained, tmp_path):
    source = trained["dir"] / trained["version"]
    copy = tmp_path / "old"
    copy.mkdir()
    meta = json.loads((source / "meta.json").read_text())
    meta["features"] = meta["features"][:-1]
    (copy / "meta.json").write_text(json.dumps(meta))
    (copy / "model.joblib").write_bytes((source / "model.joblib").read_bytes())
    with pytest.raises(ValueError):
        model.load("old", tmp_path)


def test_missing_class_gets_zero_and_the_rest_still_sum_to_one():
    class Five:
        classes_ = np.array(evaluate.CLASSES[:5])

        def predict_proba(self, X):
            return np.full((len(X), 5), 0.2)

    probs = model.Model(Five(), "five").classify(EXAMPLE)["probs"]
    assert probs["low_priority_data"] == 0 and sum(probs.values()) == pytest.approx(1)


def test_score_reports_calibration(trained):
    loaded = model.load(trained["version"], trained["dir"])
    rng = np.random.default_rng(4)
    rows = [record(rng, cls, i, "s") for i, cls in enumerate(evaluate.CLASSES * 5)]
    result = evaluate.score(loaded, rows, evaluate.load_damage_table())
    assert 0 <= result["ece"] <= 1
    assert sum(b["flows"] for b in result["reliability"] if b) == pytest.approx(len(rows))

    class Overconfident(rules.Rules):
        def verdict(self, row):
            return "standard"

    always = evaluate.score(Overconfident(), rows, evaluate.load_damage_table())
    assert always["ece"] == pytest.approx(1 - 1 / 7)  # certain every time, right a seventh of the time


def request(i, rec=EXAMPLE, **extra):
    return {"op": "classify", "id": i, "record": rec, "previous_tin": None, "leased_classes": [], **extra}


def test_service_answers(trained):
    service = serve.Service(model.load(trained["version"], trained["dir"]),
                            decide.Engine(evaluate.load_damage_table()))
    bad_record = {**EXAMPLE, "pkt_sizes": [1, 2, 3]}
    responses = service.answer([
        request(1), {"op": "health", "id": 2}, request(3, bad_record), {"op": "verify", "id": 4}, "nonsense",
        request(5, previous_tin="video", leased_classes=["telephony"]), {"op": "classify", "id": 6},
    ])
    for response in responses:
        assert schema_errors("service_response", response) == [], response
    assert [r["id"] for r in responses] == [1, 2, 3, 4, None, 5, 6]
    assert [r["ok"] for r in responses] == [True, True, False, False, False, True, False]
    assert schema_errors("policy_proposal", responses[0]["proposal"]) == []
    assert schema_errors("classification", responses[0]["classification"]) == []
    assert responses[0]["proposal"]["flow_label"]["host_id"] == EXAMPLE["host_id"]
    assert responses[0]["proposal"]["source"] == "decision_engine"
    assert responses[1]["model_version"] == trained["version"] and responses[1]["decision_rule"] == "expected_damage"
    assert responses[2]["error"].startswith("record:")
    assert responses[5]["proposal"]["flow_label"]["previous_tin"] == "video"


def test_service_with_rules_says_rules():
    service = serve.Service(rules.Rules(), decide.Engine(evaluate.load_damage_table(), mode="top_label",
                                                         rule_name="rules"))
    window = json.loads((evaluate.REPO / "contracts" / "examples" / "feature_record.window.json").read_text())
    response = service.answer([request(1, window)])[0]
    assert response["proposal"]["flow_label"]["decision_rule"] == "rules"
    assert response["proposal"]["source"] == "reclassifier"  # a window record is a second look
    assert schema_errors("policy_proposal", response["proposal"]) == []
    assert service.health({"id": 0})["model_version"] == rules.RULES_VERSION


def test_a_failing_model_does_not_take_the_service_down():
    class Broken:
        model_version = "broken"

        def classify(self, rec):
            raise RuntimeError("no trees")

    service = serve.Service(Broken(), decide.Engine(evaluate.load_damage_table()))
    responses = service.answer([request(1), {"op": "health", "id": 2}])
    assert (responses[0]["ok"], responses[1]["ok"]) == (False, True)
    assert "no trees" in responses[0]["error"]


def test_service_over_the_socket(trained, tmp_path):
    service = serve.Service(model.load(trained["version"], trained["dir"]),
                            decide.Engine(evaluate.load_damage_table()))
    path = tmp_path / "ml.sock"
    server = serve.Server(path, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        requests = [request(i) for i in range(300)] + [{"op": "health", "id": 300}]
        responses = serve.ask(path, requests)
        assert set(responses) == set(range(301))  # one answer each, whatever order they came in
        assert all(r["ok"] for r in responses.values())
        assert serve.ask(path, [{"op": "health", "id": 7}])[7]["ready"]
        # A line that is not JSON gets an answer and the connection lives on.
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(5)
            sock.connect(str(path))
            sock.sendall(b"{not json\n" + json.dumps({"op": "health", "id": 9}).encode() + b"\n")
            reader = sock.makefile()
            lines = [json.loads(reader.readline()), json.loads(reader.readline())]
        assert {(r["id"], r["ok"]) for r in lines} == {(None, False), (9, True)}
    finally:
        server.shutdown()
        server.server_close()


def test_head_to_head_report(trained):
    from aqos_ml import compare
    out = compare.compare(trained["dataset"], models_dir=trained["dir"], jobs=1, report=lambda *_: None)
    names = [r["contestant"] for r in out["results"]]
    assert names[0] == "Rules, qosify defaults" and names[1].startswith("Rules, tuned")
    assert sum("(O2)" in n for n in names) == 2 and sum("(O1)" in n for n in names) == 2
    assert out["split"] == "validation" and out["flows"] == 25 * 7
    by = {r["contestant"]: r for r in out["results"]}
    model_score = by["LightGBM, expected damage (O2)"]
    # The made-up classes are told apart by the first packet, which the
    # rules never look at: the model must win, and clearly.
    assert model_score["expected_damage_balanced"] < by[names[1]]["expected_damage_balanced"]
    low, high = model_score["vs_tuned_rules"]["interval_95"]
    # One session per class here, so nothing varies and the interval is the difference.
    assert low == pytest.approx(model_score["vs_tuned_rules"]["difference"]) == pytest.approx(high) and high < 0
    assert by[names[0]]["ece"] is None and model_score["ece"] is not None
    assert out["holdout"] == {"made-up": {"skipped": "a class would have no data left without this source"}}
    # No video calls or cloud gaming in the made-up data: the central claim
    # cannot be checked, and the verdict must say so and not say "go".
    go, reasons = compare.verdict(out)
    assert not go and any("cannot be checked" in r for r in reasons)
    text = compare.markdown(out)
    assert "## Verdict: no-go" in text and "| Rules, qosify defaults |" in text
    assert "The damage table is not the measured one" in text or out["damage_table"]["status"] == "measured"


def test_interval_resamples_sessions():
    from aqos_ml import compare
    flow = lambda session, label, cost: (session, label, None, "s", cost, {})  # noqa: E731
    better = [flow(f"s{i}", cls, 0.0) for i in range(30) for cls in ("telephony", "standard")]
    worse = [flow(f"s{i}", cls, 1.0 + (i % 3)) for i in range(30) for cls in ("telephony", "standard")]
    low, high = compare.interval(better, worse)
    assert low < high < 0 and low == pytest.approx(-2, abs=0.5)
    same = compare.interval(better, better)
    assert same == (0.0, 0.0)
    assert compare.balanced(worse) == pytest.approx(2.0)
    share, n = compare.share([("s", "standard", "video_call", "x", 0.0, {"bulk": 0.5})], "bulk", lambda f: True)
    assert (share, n) == (0.5, 1)

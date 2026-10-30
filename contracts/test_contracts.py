"""Checks the contract schemas against the examples and against each other.

The Go side has a matching test in contracts_test.go that decodes the same
example files.
"""

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

HERE = Path(__file__).parent
SCHEMAS = {
    p.name.removesuffix(".schema.json"): json.loads(p.read_text())
    for p in sorted((HERE / "schema").glob("*.schema.json"))
}
TAXONOMY = json.loads((HERE / "taxonomy.json").read_text())

CLASSES = set(TAXONOMY["classes"])
TINS = set(TAXONOMY["tins"])
DSCPS = set(TAXONOMY["dscp"])
PROFILES = set(TAXONOMY["profiles"])


def load_example(name):
    path = HERE / "examples" / name
    if path.suffix == ".yaml":
        return yaml.safe_load(path.read_text())
    return json.loads(path.read_text())


def errors(schema_name, instance):
    validator = Draft202012Validator(SCHEMAS[schema_name])
    return [e.message for e in validator.iter_errors(instance)]


def without_none(values):
    return {v for v in values if v is not None}


@pytest.mark.parametrize("schema", SCHEMAS.values(), ids=SCHEMAS.keys())
def test_schema_is_valid(schema):
    Draft202012Validator.check_schema(schema)


EXAMPLES = [
    ("feature_record", "feature_record.initial.json"),
    ("feature_record", "feature_record.window.json"),
    ("classification", "classification.json"),
    ("policy_proposal", "policy_proposal.flow_label.json"),
    ("policy_proposal", "policy_proposal.shaper_rate.json"),
    ("policy_verdict", "policy_verdict.clamp.json"),
    ("policy_verdict", "policy_verdict.reject.json"),
    ("run_manifest", "run_manifest.json"),
    ("scenario", "scenario.S1.yaml"),
    ("service_request", "service_request.classify.json"),
    ("service_request", "service_request.health.json"),
    ("service_response", "service_response.classify.json"),
    ("service_response", "service_response.health.json"),
    ("service_response", "service_response.error.json"),
]


@pytest.mark.parametrize("schema_name,example", EXAMPLES)
def test_example_is_valid(schema_name, example):
    assert errors(schema_name, load_example(example)) == []


def test_every_schema_has_an_example():
    assert {name for name, _ in EXAMPLES} | {"metric_sample"} == set(SCHEMAS)


def test_metric_samples_are_valid():
    lines = (HERE / "examples" / "metric_samples.jsonl").read_text().splitlines()
    assert lines
    for line in lines:
        assert errors("metric_sample", json.loads(line)) == []


def mutate(example, path, value):
    """Returns a copy of the example with the value at path replaced."""
    doc = copy.deepcopy(load_example(example))
    node = doc
    for key in path[:-1]:
        node = node[key]
    if value is DELETE:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return doc


DELETE = object()

INVALID = [
    ("feature_record", "feature_record.initial.json", ["pkt_sizes"], [100] * 7),
    ("feature_record", "feature_record.initial.json", ["pkt_sizes", 0], 0),
    ("feature_record", "feature_record.initial.json", ["payload"], "deadbeef"),
    ("feature_record", "feature_record.initial.json", ["src_ip"], "192.168.1.10"),
    ("feature_record", "feature_record.window.json", ["window"], None),
    ("classification", "classification.json", ["probs", "telephony"], 1.2),
    ("classification", "classification.json", ["probs", "standard"], DELETE),
    ("policy_proposal", "policy_proposal.flow_label.json", ["reason"], ""),
    ("policy_proposal", "policy_proposal.flow_label.json", ["flow_label"], DELETE),
    ("policy_proposal", "policy_proposal.flow_label.json", ["kind"], "shaper_rate"),
    ("policy_proposal", "policy_proposal.flow_label.json", ["flow_label", "tin"], "express"),
    ("policy_proposal", "policy_proposal.shaper_rate.json", ["shaper_rate", "rate_kbit"], 0),
    ("policy_verdict", "policy_verdict.reject.json", ["applied"], {"rate_kbit": 1000}),
    ("policy_verdict", "policy_verdict.clamp.json", ["applied"], None),
    ("run_manifest", "run_manifest.json", ["valid"], False),
    ("run_manifest", "run_manifest.json", ["config"], "B9"),
    ("scenario", "scenario.S1.yaml", ["traffic", 0, "profile"], "netflix"),
    ("service_request", "service_request.classify.json", ["record"], DELETE),
    ("service_request", "service_request.classify.json", ["leased_classes"], ["gaming"]),
    ("service_request", "service_request.classify.json", ["op"], "verify"),
    ("service_request", "service_request.health.json", ["record"], {}),
    ("service_response", "service_response.classify.json", ["proposal"], DELETE),
    ("service_response", "service_response.classify.json", ["ok"], False),
    ("service_response", "service_response.error.json", ["error"], DELETE),
    ("scenario", "scenario.S1.yaml", ["hosts"], []),
    ("scenario", "scenario.S1.yaml", ["duration_s"], 0),
]


@pytest.mark.parametrize("schema_name,example,path,value", INVALID)
def test_invalid_is_rejected(schema_name, example, path, value):
    assert errors(schema_name, mutate(example, path, value)) != []


def test_classification_probs_sum_to_one():
    doc = load_example("classification.json")
    assert sum(doc["probs"].values()) == pytest.approx(1.0, abs=1e-6)
    assert doc["confidence"] == pytest.approx(max(doc["probs"].values()))


def test_flow_label_dscp_maps_to_tin():
    label = load_example("policy_proposal.flow_label.json")["flow_label"]
    assert TAXONOMY["dscp"][label["dscp"]]["tin"] == label["tin"]


def test_scenario_traffic_uses_declared_hosts():
    doc = load_example("scenario.S1.yaml")
    assert {t["host"] for t in doc["traffic"]} <= set(doc["hosts"])
    ids = [t["id"] for t in doc["traffic"]]
    assert len(ids) == len(set(ids))


def test_taxonomy_is_consistent():
    for name, entry in TAXONOMY["classes"].items():
        assert TAXONOMY["dscp"][entry["dscp"]]["tin"] == entry["tin"], name
    for name, entry in TAXONOMY["tins"].items():
        assert TAXONOMY["dscp"][entry["dscp"]]["tin"] == name
    assert set(TAXONOMY["profiles"].values()) <= CLASSES
    assert TAXONOMY["classes"][TAXONOMY["default_class"]]["tin"] == "best_effort"


def test_schema_enums_match_taxonomy():
    """The schemas repeat the taxonomy's names; they must not drift apart."""
    proposal = SCHEMAS["policy_proposal"]
    label = proposal["$defs"]["flow_label"]["properties"]
    assert set(proposal["$defs"]["tin"]["enum"]) == TINS
    assert set(label["dscp"]["enum"]) == DSCPS
    assert without_none(label["top_class"]["enum"]) == CLASSES

    applied = SCHEMAS["policy_verdict"]["properties"]["applied"]["oneOf"][1]["properties"]
    assert set(applied["tin"]["enum"]) == TINS
    assert set(applied["dscp"]["enum"]) == DSCPS

    assert set(SCHEMAS["classification"]["properties"]["probs"]["required"]) == CLASSES
    assert set(SCHEMAS["classification"]["properties"]["probs"]["properties"]) == CLASSES

    sample = SCHEMAS["metric_sample"]["properties"]
    assert without_none(sample["true_class"]["enum"]) == CLASSES
    assert without_none(sample["tin"]["enum"]) == TINS
    assert without_none(sample["profile"]["enum"]) == PROFILES

    scenario = SCHEMAS["scenario"]["properties"]
    assert set(scenario["traffic"]["items"]["properties"]["profile"]["enum"]) == PROFILES
    assert scenario["primary_metrics"]["items"]["enum"] == sample["metric"]["enum"]

    assert SCHEMAS["run_manifest"]["properties"]["config"]["enum"] == TAXONOMY["configs"]


def test_service_messages_carry_valid_contracts():
    """The service schemas leave the nested records to their own schemas;
    the examples must satisfy those too."""
    request = load_example("service_request.classify.json")
    response = load_example("service_response.classify.json")
    assert errors("feature_record", request["record"]) == []
    assert errors("classification", response["classification"]) == []
    assert errors("policy_proposal", response["proposal"]) == []
    assert response["proposal"]["flow_label"]["flow_id"] == request["record"]["flow_id"]

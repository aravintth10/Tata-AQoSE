import json

import pytest
from jsonschema import Draft202012Validator

from aqos_ml import evaluate, rules

from harness import REPO

CLASSIFICATION = Draft202012Validator(json.loads((REPO / "contracts/schema/classification.schema.json").read_text()))
EXAMPLE = json.loads((REPO / "contracts/examples/feature_record.window.json").read_text())
# The PRD's illustrative table: these tests are about the rule, and must
# not change when the measured table does.
DAMAGE = evaluate.load_damage_table(evaluate.REPO / "configs" / "damage_table.illustrative.yaml")


def record(pps_up, pps_down, bytes_up, bytes_down, label=None):
    window = {**EXAMPLE["window"], "pkts_up_per_s": pps_up, "pkts_down_per_s": pps_down,
              "bytes_up_per_s": bytes_up, "bytes_down_per_s": bytes_down}
    row = {**EXAMPLE, "window": window}
    if label:
        row["label"] = label
    return row


VOICE = record(50, 50, 6000, 6000)                  # 100 packets/s of 120 bytes
HD_CALL = record(250, 300, 250_000, 300_000)        # 550 packets/s of 1000 bytes
DOWNLOAD = record(400, 800, 21_000, 1_200_000)      # 1200 packets/s
BROWSING = record(5, 8, 600, 9000)                  # 13 packets/s of 740 bytes


def test_verdicts_follow_the_two_thresholds():
    r = rules.Rules(bulk_pps=200, priority_bytes=450)
    assert r.verdict(VOICE) == rules.PRIORITY
    assert r.verdict(DOWNLOAD) == rules.BULK
    assert r.verdict(BROWSING) == rules.DEFAULT
    # The case the project exists for: an HD call has the packet rate of a
    # download, so a rate threshold sends it to the slow lane.
    assert r.verdict(HD_CALL) == rules.BULK
    # No window, no rate, no verdict.
    assert r.verdict({**EXAMPLE, "window": None}) == rules.DEFAULT
    assert r.verdict(record(0, 0, 0, 0)) == rules.DEFAULT


def test_classification_follows_the_contract():
    c = rules.Rules().classify(VOICE)
    assert [e.message for e in CLASSIFICATION.iter_errors(c)] == []
    assert c["probs"][rules.PRIORITY] == 1.0 and sum(c["probs"].values()) == 1.0
    assert c["model_version"] == rules.RULES_VERSION and c["flow_id"] == EXAMPLE["flow_id"]


def test_damage_table_covers_every_class_and_tin():
    assert set(DAMAGE) == set(evaluate.CLASSES)
    for cls, row in DAMAGE.items():
        assert set(row) == set(evaluate.TINS)
        # Damage is relative to the class's best tin, which costs nothing.
        # That is not always its own tin: the PRD puts a download's best
        # place in Bulk, though its class maps to Best Effort.
        assert min(row.values()) == 0 and max(row.values()) <= 10
    # The two rows the PRD gives.
    assert (DAMAGE["telephony"]["voice"], DAMAGE["telephony"]["best_effort"], DAMAGE["telephony"]["bulk"]) == (0, 2, 10)
    assert (DAMAGE["high_throughput_data"]["voice"], DAMAGE["high_throughput_data"]["best_effort"],
            DAMAGE["high_throughput_data"]["bulk"]) == (4, 1, 0)


def labelled():
    flows = [(VOICE, "telephony"), (HD_CALL, "multimedia_conferencing"),
             (DOWNLOAD, "high_throughput_data"), (BROWSING, "standard")]
    return [{**record, "label": label, "flow_id": f"{i:016x}"} for i, (record, label) in enumerate(flows)]


def test_score_is_the_mean_damage_of_the_tins_chosen():
    result = evaluate.score(rules.Rules(200, 450), labelled(), DAMAGE)
    # voice -> Voice (0), HD call -> Bulk (10), download -> Bulk (0), browsing -> Best Effort (0)
    assert result["expected_damage"] == pytest.approx(10 / 4)
    assert result["expected_damage_balanced"] == pytest.approx(10 / 4)
    assert result["rows"] == 4 and result["flows"] == 4
    assert result["damage_by_class"]["multimedia_conferencing"] == 10
    assert result["tins"]["multimedia_conferencing"] == {"bulk": 1}
    assert result["confusion"]["telephony"] == {rules.PRIORITY: 1}
    assert result["recall"]["standard"] == 1.0 and result["recall"]["telephony"] == 0.0
    assert result["recall"]["multimedia_streaming"] is None  # no such rows
    assert result["model_version"] == rules.RULES_VERSION


def test_tuning_lowers_expected_damage_and_reports_the_thresholds():
    rows = labelled()
    default = evaluate.score(rules.Rules(), rows, DAMAGE)["expected_damage_balanced"]
    best, result, tried = rules.tune(rows, DAMAGE)
    assert result["expected_damage_balanced"] < default
    assert len(tried) == len(rules.BULK_GRID) * len(rules.PRIORITY_GRID)
    assert result["expected_damage_balanced"] == min(t[2] for t in tried)
    # On these four flows the best the rules can do is raise the rate
    # threshold above the call; the call still cannot reach Video.
    assert 550 < best.bulk_pps <= 1200
    assert result["tins"]["multimedia_conferencing"] != {"video": 1}


def test_top_label_ties_do_not_depend_on_order():
    probs = {cls: 0.0 for cls in evaluate.CLASSES}
    probs["standard"] = probs["low_priority_data"] = 0.5
    forward = evaluate.top_label_tin({"probs": probs})
    backward = evaluate.top_label_tin({"probs": dict(reversed(list(probs.items())))})
    assert forward == backward == "best_effort"


def test_a_long_flow_counts_once_and_every_class_counts_the_same():
    """One call with a hundred window records and one download with one.
    Counted by record the call would be 99% of the score."""
    rows = [{**HD_CALL, "label": "multimedia_conferencing", "flow_id": "a" * 16} for _ in range(100)]
    rows.append({**DOWNLOAD, "label": "high_throughput_data", "flow_id": "b" * 16})
    result = evaluate.score(rules.Rules(200, 450), rows, DAMAGE)
    assert result["rows"] == 101 and result["flows"] == 2
    # The call goes to Bulk (10), the download to Bulk (0).
    assert result["expected_damage"] == pytest.approx(5)
    assert result["tins"]["multimedia_conferencing"] == {"bulk": 1}

    # Nine more downloads: by flow the call is now a tenth of the data, by
    # class it is still half.
    rows += [{**DOWNLOAD, "label": "high_throughput_data", "flow_id": f"{i:016x}"} for i in range(9)]
    result = evaluate.score(rules.Rules(200, 450), rows, DAMAGE)
    assert result["expected_damage"] == pytest.approx(10 / 11)
    assert result["expected_damage_balanced"] == pytest.approx(5)

    # The same flow id in two sessions is two flows.
    two = [{**VOICE, "label": "telephony", "session": s} for s in ("x", "y")]
    assert evaluate.score(rules.Rules(), two, DAMAGE)["flows"] == 2

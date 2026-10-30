"""Decision engine: the PRD's worked example, the demotion bar, leases, the
fallback, and that every proposal it can produce satisfies the contract."""

import json
import random
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from aqos_ml import decide, evaluate

REPO = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO / "contracts" / "schema" / "policy_proposal.schema.json").read_text())
# The PRD's illustrative table: these tests are about the rule, and must
# not change when the measured table does.
DAMAGE = evaluate.load_damage_table(evaluate.REPO / "configs" / "damage_table.illustrative.yaml")
HOST = "a1b2c3d4e5f60718"


def classification(**probs):
    full = {cls: 0.0 for cls in evaluate.CLASSES} | probs
    return {"schema_version": 1, "flow_id": "3fa9c1d2e4b50761", "model_version": "test",
            "probs": full, "confidence": max(full.values()), "inference_us": 1.0}


def valid(proposal):
    return [e.message for e in Draft202012Validator(SCHEMA).iter_errors(proposal)]


@pytest.mark.parametrize("download", ["high_throughput_data", "low_priority_data"])
def test_worked_example(download):
    """45% call and 55% download: Best Effort. 5% and 95%: Bulk."""
    engine = decide.Engine(DAMAGE)
    unsure = classification(telephony=0.45, **{download: 0.55})
    sure = classification(telephony=0.05, **{download: 0.95})
    assert engine.tin(unsure) == "best_effort"
    assert engine.tin(sure) == "bulk"


def test_top_label_takes_the_tin_of_the_likeliest_class():
    engine = decide.Engine(DAMAGE, mode="top_label")
    assert engine.tin(classification(telephony=0.4, standard=0.35, multimedia_streaming=0.25)) == "voice"
    assert engine.tin(classification(multimedia_conferencing=0.6, standard=0.4)) == "video"
    # The demotion bar applies here too.
    assert engine.tin(classification(low_priority_data=0.55, telephony=0.45)) == "best_effort"
    assert engine.tin(classification(low_priority_data=0.95, telephony=0.05)) == "bulk"


def test_expected_damage_hedges_where_top_label_gambles():
    probs = classification(telephony=0.4, standard=0.35, multimedia_streaming=0.25)
    assert decide.Engine(DAMAGE, mode="top_label").tin(probs) == "voice"
    assert decide.Engine(DAMAGE).tin(probs) != "voice"


def test_demotion_bar_is_strict_and_configurable():
    at_bar = classification(low_priority_data=0.8, standard=0.2)
    assert decide.Engine(DAMAGE).tin(at_bar) == "best_effort"
    assert decide.Engine(DAMAGE, demotion_bar=0.7).tin(at_bar) == "bulk"
    _, _, _, held = decide.Engine(DAMAGE).choose(at_bar)
    assert held


def test_a_tie_follows_the_taxonomy_but_never_into_bulk():
    """When the table cannot separate tins, the flow goes where RFC 4594
    puts its class. Demotion needs evidence, not a tie."""
    flat = {cls: {tin: 1.0 for tin in evaluate.TINS} for cls in evaluate.CLASSES}
    engine = decide.Engine(flat)
    assert engine.tin(classification(telephony=1.0)) == "voice"
    assert engine.tin(classification(multimedia_conferencing=0.6, standard=0.4)) == "video"
    assert engine.tin(classification(standard=1.0)) == "best_effort"
    assert engine.tin(classification(low_priority_data=1.0)) == "best_effort"
    # Only Bulk hurts, as measured with a flow alone on its device: a call
    # is promoted, since nothing says not to; nothing is demoted.
    only_bulk = {cls: {"voice": 0.0, "video": 0.0, "best_effort": 0.0, "bulk": 9.0} for cls in evaluate.CLASSES}
    only_bulk["low_priority_data"]["bulk"] = 0.0
    engine = decide.Engine(only_bulk)
    assert engine.tin(classification(multimedia_conferencing=0.9, high_throughput_data=0.1)) == "video"
    assert engine.tin(classification(high_throughput_data=1.0)) == "best_effort"
    assert engine.tin(classification(low_priority_data=1.0)) == "best_effort"
    # A difference smaller than the table's resolution is a tie too.
    nearly = {cls: {"voice": 0.0, "video": 0.01, "best_effort": 0.03, "bulk": 9.0} for cls in evaluate.CLASSES}
    assert decide.Engine(nearly).tin(classification(multimedia_streaming=1.0)) == "video"
    assert decide.Engine(nearly).tin(classification(standard=1.0)) == "best_effort"
    # A real difference wins over the taxonomy.
    real = {cls: {"voice": 0.0, "video": 2.0, "best_effort": 2.0, "bulk": 9.0} for cls in evaluate.CLASSES}
    assert decide.Engine(real).tin(classification(realtime_interactive=1.0)) == "voice"


def test_voice_needs_a_call_or_a_game():
    """A table that says Voice harms nothing must not send everything
    there: Voice is a quarter of the link."""
    free = {cls: {"voice": 0.0, "video": 1.0, "best_effort": 2.0, "bulk": 9.0} for cls in evaluate.CLASSES}
    engine = decide.Engine(free)
    assert engine.tin(classification(multimedia_streaming=1.0)) == "video"
    assert engine.tin(classification(standard=0.55, telephony=0.45)) == "video"
    assert engine.tin(classification(standard=0.4, telephony=0.35, realtime_interactive=0.25)) == "voice"
    proposal = engine.propose(classification(multimedia_streaming=1.0), "a1b2c3d4e5f60718")
    assert "not sure enough that it is a call or a game" in proposal["reason"]
    assert decide.Engine(free, voice_bar=-1).tin(classification(multimedia_streaming=1.0)) == "voice"


def test_lease_changes_the_table_not_the_probabilities():
    engine = decide.Engine(DAMAGE)
    probs = classification(multimedia_conferencing=0.3, high_throughput_data=0.7)
    assert engine.tin(probs) == "best_effort"
    tin, _, expected, _ = engine.choose(probs, leased_classes=["multimedia_conferencing"])
    assert tin == "video"
    assert expected["best_effort"] == pytest.approx(0.3 * 2 * 5 + 0.7 * 1)
    # A lease on a class that is not interactive does nothing.
    assert engine.choose(probs, leased_classes=["standard"])[0] == "best_effort"
    assert DAMAGE["multimedia_conferencing"]["best_effort"] == 2  # table itself untouched


def test_dscp_follows_the_contract():
    engine = decide.Engine(DAMAGE)
    call = engine.propose(classification(multimedia_conferencing=0.9, standard=0.1), HOST)
    assert (call["flow_label"]["tin"], call["flow_label"]["dscp"]) == ("video", "AF41")
    hedge = engine.propose(classification(telephony=0.45, low_priority_data=0.55), HOST)
    assert (hedge["flow_label"]["tin"], hedge["flow_label"]["dscp"]) == ("best_effort", "CS0")


def test_reason_is_a_sentence_about_this_decision():
    engine = decide.Engine(DAMAGE)
    reason = engine.propose(classification(multimedia_conferencing=0.78, standard=0.22), HOST)["reason"]
    assert "a video call" in reason and "78%" in reason and "Video" in reason
    held = engine.propose(classification(low_priority_data=0.7, standard=0.3), HOST)["reason"]
    assert "Bulk" in held and "Best Effort" in held
    leased = engine.propose(classification(telephony=1.0), HOST, leased_classes=["telephony"])["reason"]
    assert "lease" in leased


@pytest.mark.parametrize("broken", [
    {"probs": {}},
    {"probs": None},
    {"probs": {cls: 0.5 for cls in evaluate.CLASSES}},
    {"probs": {cls: float("nan") for cls in evaluate.CLASSES}},
    {"probs": {cls: "x" for cls in evaluate.CLASSES}},
    {},
])
def test_anything_wrong_gives_best_effort(broken):
    proposal = decide.Engine(DAMAGE).propose({"flow_id": "3fa9c1d2e4b50761", **broken}, HOST)
    label = proposal["flow_label"]
    assert (label["tin"], label["dscp"], label["decision_rule"]) == ("best_effort", "CS0", "fallback")
    assert label["top_class"] is None and label["expected_damage"] is None
    assert not valid(proposal)


@pytest.mark.parametrize("mode", decide.MODES)
def test_every_proposal_satisfies_the_contract(mode):
    engine, rng = decide.Engine(DAMAGE, mode=mode), random.Random(5)
    seen = set()
    for _ in range(2000):
        raw = [rng.random() ** rng.choice([1, 4, 12]) for _ in evaluate.CLASSES]
        probs = dict(zip(evaluate.CLASSES, (v / sum(raw) for v in raw)))
        leased = rng.choice([(), decide.INTERACTIVE])
        proposal = engine.propose(classification(**probs), HOST, previous_tin=rng.choice([None, "video"]),
                                  leased_classes=leased)
        assert not valid(proposal), proposal
        label = proposal["flow_label"]
        assert label["decision_rule"] == mode
        assert evaluate.TAXONOMY["dscp"][label["dscp"]]["tin"] == label["tin"]
        if label["tin"] == "bulk":
            assert sum(probs[c] for c in engine._bulk_classes) > engine.demotion_bar
        seen.add(label["tin"])
    assert seen == set(evaluate.TINS)


def test_engine_is_what_evaluate_scores_with():
    class Fixed:
        model_version = "fixed"

        def classify(self, row):
            return classification(telephony=0.45, low_priority_data=0.55)

    rows = [{"flow_id": "f1", "session": "s", "label": "telephony"}]
    gamble = evaluate.score(Fixed(), rows, DAMAGE, decide=lambda c, record=None: "bulk")
    hedge = evaluate.score(Fixed(), rows, DAMAGE, decide=decide.Engine(DAMAGE).tin)
    assert (gamble["expected_damage"], hedge["expected_damage"]) == (10, 2)


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        decide.Engine(DAMAGE, mode="guess")


def test_bulk_needs_more_than_confidence():
    """Confidently 'background transfer' is not enough on a flow's first
    packets, or for a UDP flow: unseen calls were sent to Bulk that way."""
    sure = classification(low_priority_data=0.97, standard=0.03)
    engine = decide.Engine(evaluate.load_damage_table())
    window_tcp = {"kind": "window", "protocol": 6}
    assert engine.tin(sure, window_tcp) == "bulk"
    assert engine.tin(sure, {"kind": "initial", "protocol": 6}) == "best_effort"
    assert engine.tin(sure, {"kind": "window", "protocol": 17}) == "best_effort"
    assert engine.tin(sure) == "bulk"  # without a record there is nothing to check
    unguarded = decide.Engine(evaluate.load_damage_table(), bulk_guard=False)
    assert unguarded.tin(sure, {"kind": "window", "protocol": 17}) == "bulk"
    proposal = engine.propose(sure, "a1b2c3d4e5f60718", record={"kind": "window", "protocol": 17})
    assert proposal["flow_label"]["tin"] == "best_effort" and "while it is UDP" in proposal["reason"]

import json

from aqos_ml import envelope

RANGES = {name: [10.0, 20.0] for name in envelope.WINDOW}
INSIDE = {name: 15.0 for name in envelope.WINDOW}


def test_outside_counts_and_names_the_furthest():
    assert envelope.outside({"call": RANGES}, "call", INSIDE) == (0, None)
    window = {**INSIDE, "rate_cv": 21.0, "burstiness": 60.0, "pkts_up_per_s": 9.0}
    assert envelope.outside({"call": RANGES}, "call", window) == (3, "burstiness")
    assert envelope.outside({"call": RANGES}, "game", window) is None
    assert envelope.outside({"call": RANGES}, "call", None) is None


def test_a_flow_outside_is_sent_back_and_then_left_alone_for_longer_each_time():
    outside = {**INSIDE, "rate_cv": 40.0, "burstiness": 60.0}
    watcher = envelope.Watcher({"call": RANGES}, "call")
    sent = [second for second in range(1, 61) if watcher.window("video", outside)]
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    assert sent[0] == 2 and gaps == sorted(gaps) and gaps[0] >= 5 and gaps[-1] <= 41


def test_the_cases_file_is_what_the_definition_gives_today():
    """internal/reclass/testdata/cases.json is what the Go check is tested
    against. If the definition here or the chosen model changes, it must be
    written again: python -m aqos_ml.envelope cases"""
    stored = json.loads(envelope.CASES.read_text())
    assert stored["cases"] == json.loads(json.dumps(envelope.cases(stored["envelopes"])))

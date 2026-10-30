import numpy as np

from aqos_ml import report


def test_a_method_that_wins_on_every_seed_is_better():
    rng = np.random.default_rng(1)
    ours = {seed: 0.05 + 0.01 * seed for seed in range(10)}
    theirs = {seed: 1.0 + 0.02 * seed for seed in range(10)}
    c = report.compare(ours, theirs, rng)
    assert c["verdict"] == "better" and c["effect"] == -1.0 and c["p"] < 0.01 and c["high"] < 0
    assert report.compare(theirs, ours, rng)["verdict"] == "worse"


def test_identical_runs_are_a_tie_and_noise_is_not_a_win():
    rng = np.random.default_rng(1)
    same = {seed: 0.3 for seed in range(10)}
    c = report.compare(same, dict(same), rng)
    assert c["verdict"] == "tie" and c["p"] is None and c["effect"] is None
    # Better by less than two identical runs differ by: a tie.
    ours = {seed: 0.30 for seed in range(10)}
    theirs = {seed: 0.32 for seed in range(10)}
    assert report.compare(ours, theirs, rng)["verdict"] == "better"
    assert report.compare(ours, theirs, rng, noise=0.05)["verdict"] == "tie"


def test_too_few_seeds_claims_nothing():
    rng = np.random.default_rng(1)
    c = report.compare({1: 0.0, 2: 0.0}, {1: 1.0, 2: 1.0}, rng)
    assert c["verdict"] == "too few seeds" and c["median"] == -1.0
    assert report.compare({1: 0.0}, {2: 1.0}, rng) is None


def test_holm_never_lowers_a_p_value():
    results = [{"p": 0.01}, {"p": 0.04}, {"p": 0.03}, {"p": None}]
    report.holm(results)
    assert results[0]["p_holm"] == 0.03 and results[2]["p_holm"] == 0.06 and results[1]["p_holm"] == 0.06
    assert "p_holm" not in results[3]

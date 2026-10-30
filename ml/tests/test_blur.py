import numpy as np

from aqos_ml import serve


def record():
    return {"kind": "window", "protocol": 17, "pkt_sizes": [120, -1200, 800, -60, 1500, -1500, 40, 300],
            "pkt_gaps_us": [100, 20000, 300, 5, 0, 7000, 33000], "window": {"samples": 5, "pkts_up_per_s": 50.0,
                                                                            "rate_cv": 0.2}}


def test_no_noise_changes_nothing():
    s = serve.Service.__new__(serve.Service)
    r = record()
    assert s.blur(r) is r


def test_noise_moves_the_numbers_and_keeps_what_the_guard_reads():
    s = serve.Service.__new__(serve.Service)
    s.noise, s._rng = 1.0, np.random.default_rng(3)
    r = record()
    out = s.blur(r)
    assert out["kind"] == "window" and out["protocol"] == 17 and out["window"]["samples"] == 5
    assert out["pkt_sizes"] != r["pkt_sizes"] and out["pkt_gaps_us"] != r["pkt_gaps_us"]
    assert all(40 <= abs(v) <= 1500 and np.sign(v) == np.sign(o) for v, o in zip(out["pkt_sizes"], r["pkt_sizes"]))
    assert r == record()  # the original is untouched

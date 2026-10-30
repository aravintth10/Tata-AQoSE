"""Randomised scenarios for making training data in the testbed.

Each has a few applications on a random link. Some run alone on an idle
link and some share it, so the first packets of a flow are seen both clean
and queued behind other traffic.
"""

import random

# How often each profile is drawn. Classes with one profile get more, so
# the seven classes come out roughly even.
WEIGHTS = {
    "voice_call": 3, "video_call": 3, "gaming": 2, "cloud_gaming": 2, "abr_video": 3,
    "web_browsing": 3, "bulk_download": 2, "game_update": 1, "backup_upload": 2, "p2p": 1,
}
HOSTS = ["laptop", "desktop", "tv1", "phone"]
FIRST_ID = 1000


def make(index, seed=1, duration_s=38, warmup_s=2):
    """Scenario number index, the same every time for the same seed."""
    rng = random.Random(f"{seed}:{index}")
    alone = rng.random() < 0.25
    count = 1 if alone else rng.randint(3, 6)
    profiles = rng.choices(list(WEIGHTS), weights=list(WEIGHTS.values()), k=count)
    traffic = []
    for n, profile in enumerate(profiles):
        entry = {"id": f"t{n}-{profile.replace('_', '-')}", "host": rng.choice(HOSTS), "profile": profile,
                 "start_s": round(rng.uniform(0, 12), 1), "stop_s": None,
                 "params": {"ip_version": rng.choice([4, 4, 4, 6])}}
        traffic.append(entry)
    down = rng.choice([10000, 20000, 50000, 100000, 200000])
    return {
        "schema_version": 1, "id": f"S{FIRST_ID + index}",
        "name": f"training mix {index}: " + ", ".join(profiles),
        "duration_s": duration_s, "warmup_s": warmup_s,
        "link": {"down_kbit": down, "up_kbit": max(down // rng.choice([2, 5, 10]), 2000),
                 "delay_ms": rng.choice([5, 10, 20, 40, 60]),
                 "loss_pct": rng.choice([0, 0, 0, 0.1, 0.5])},
        "hosts": HOSTS, "traffic": traffic, "events": [],
        "primary_metrics": ["latency_ms"],
    }

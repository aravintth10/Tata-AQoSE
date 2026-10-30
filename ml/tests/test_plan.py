import numpy as np
import pytest
import yaml

from aqos_ml.gen import plan as planning
from aqos_ml.gen.plan import AUDIO, MAIN, MAX_PAYLOAD
from aqos_ml.gen.wire import HEADER_SIZE

from harness import REPO

SCENARIO = yaml.safe_load((REPO / "ml/tests/scenarios/all_profiles.yaml").read_text())
IDS = [t["id"] for t in SCENARIO["traffic"]]


def scenario(profile, duration_s=20, **params):
    return {
        "id": "S99", "warmup_s": 0, "duration_s": duration_s,
        "traffic": [{"id": "x", "host": "laptop", "profile": profile, "start_s": 0, "stop_s": None,
                     "params": params}],
    }


@pytest.mark.parametrize("traffic_id", IDS)
def test_same_seed_gives_same_plan(traffic_id):
    a = planning.build(SCENARIO, traffic_id, 7)
    b = planning.build(SCENARIO, traffic_id, 7)
    assert a.digest() == b.digest()
    assert a.params == b.params
    for direction in a.schedules:
        assert np.array_equal(a.schedules[direction].t_s, b.schedules[direction].t_s)
        assert np.array_equal(a.schedules[direction].size, b.schedules[direction].size)


@pytest.mark.parametrize("traffic_id", IDS)
def test_other_seed_gives_other_plan(traffic_id):
    assert planning.build(SCENARIO, traffic_id, 7).digest() != planning.build(SCENARIO, traffic_id, 8).digest()


def test_entries_draw_independently():
    """Removing one entry must not change another's traffic, only its ports."""
    smaller = {**SCENARIO, "traffic": [t for t in SCENARIO["traffic"] if t["id"] != "voice"]}
    a, b = planning.build(SCENARIO, "call", 7), planning.build(smaller, "call", 7)
    assert a.params == b.params
    assert a.schedules["up"].digest() == b.schedules["up"].digest()


def test_ports_are_distinct_and_follow_the_seed():
    ports = planning.allocate_ports(7, IDS)
    flat = [p for pair in ports.values() for p in pair]
    assert len(set(flat)) == 2 * len(IDS)
    assert all(planning.PORT_RANGE[0] <= p < planning.PORT_RANGE[1] for p in flat)
    assert ports == planning.allocate_ports(7, reversed(IDS))
    assert ports != planning.allocate_ports(8, IDS)


def test_pinning_one_param_leaves_the_others():
    free = planning.build(scenario("video_call"), "x", 7)
    pinned = planning.build(scenario("video_call", kbit_up=2000), "x", 7)
    assert pinned.params["kbit_up"] == 2000
    for name in ("kbit_down", "audio_payload", "keyframe_s"):
        assert pinned.params[name] == free.params[name]


def test_unknown_param_is_rejected():
    with pytest.raises(ValueError, match="does not take"):
        planning.build(scenario("voice_call", bitrate=5), "x", 7)


def test_entry_must_stop_after_it_starts():
    sc = scenario("voice_call")
    sc["traffic"][0].update(start_s=10, stop_s=5)
    with pytest.raises(ValueError, match="stops before"):
        planning.build(sc, "x", 7)


def test_stop_is_capped_at_the_end_of_the_run():
    sc = scenario("voice_call", duration_s=20)
    sc["traffic"][0]["stop_s"] = 500
    assert planning.build(sc, "x", 7).stop_s == 20


@pytest.mark.parametrize("profile", sorted(planning.UDP_PROFILES))
def test_schedules_are_well_formed(profile):
    plan = planning.build(scenario(profile), "x", 7)
    for s in plan.schedules.values():
        assert len(s) > 0
        assert np.all(np.diff(s.t_s) >= 0)
        assert s.t_s[0] >= 0 and s.t_s[-1] < plan.duration_s
        assert s.size.min() >= HEADER_SIZE and s.size.max() <= MAX_PAYLOAD
        assert np.all(s.index < s.count)
        # Every frame is complete: as many packets as its count says.
        main = s.stream == MAIN
        frames, packets = np.unique(s.frame[main], return_counts=True)
        counts = s.count[main][np.searchsorted(s.frame[main], frames)]
        assert np.array_equal(packets, counts)
        assert np.array_equal(frames, np.arange(len(frames)))


def test_voice_call_shape():
    plan = planning.build(scenario("voice_call"), "x", 7)
    up = plan.schedules["up"]
    assert len(up) == 20 * 50
    assert np.allclose(np.diff(up.t_s), 0.020)
    assert 100 <= plan.params["payload_up"] <= 200


def test_video_bitrate_matches_the_plan():
    plan = planning.build(scenario("video_call", duration_s=60), "x", 7)
    for direction in ("up", "down"):
        s = plan.schedules[direction]
        video = s.stream == MAIN
        kbit = s.size[video].sum() * 8 / 1000 / 60
        assert kbit == pytest.approx(plan.params["kbit_" + direction], rel=0.02)
        assert np.count_nonzero(s.stream == AUDIO) == 60 * 50


def test_cloud_gaming_shape():
    plan = planning.build(scenario("cloud_gaming"), "x", 7)
    down, up = plan.schedules["down"], plan.schedules["up"]
    assert down.frame.max() + 1 == 20 * 60
    assert down.size.sum() * 8 / 1000 / 20 == pytest.approx(plan.params["kbit_down"], rel=0.02)
    assert len(up) == 20 * 125
    # A frame's packets are paced, but all leave before the next frame.
    last = down.t_s[down.index == down.count - 1]
    assert np.all(last - np.arange(len(last)) / 60 < 1 / 60)


def test_true_class_comes_from_the_taxonomy():
    for traffic_id in IDS:
        plan = planning.build(SCENARIO, traffic_id, 7)
        assert plan.true_class == planning.TAXONOMY["profiles"][plan.profile]


def changing(profile, events, duration_s=40, **params):
    out = scenario(profile, duration_s, **params)
    out["events"] = [{"at_s": at, "type": "traffic_param", "args": {"traffic": "x", "param": name, "value": value}}
                     for at, name, value in events]
    return out


def test_camera_off_leaves_an_audio_only_call():
    steady = planning.build(scenario("video_call", 40), "x", 7)
    plan = planning.build(changing("video_call", [(10, "camera", "off"), (25, "camera", "on")]), "x", 7)
    assert plan.params["camera"] == "on"
    for direction in ("up", "down"):
        s = plan.schedules[direction]
        video = s.stream == MAIN
        off = (s.t_s >= 10.1) & (s.t_s < 24.9)
        assert not (video & off).any() and (~video & off).sum() > 700  # 50 audio packets a second remain
        assert (video & (s.t_s < 10)).any() and (video & (s.t_s > 25)).any()
        # Frames still count up without gaps, which the receiver relies on.
        frames = np.unique(s.frame[video])
        assert np.array_equal(frames, np.arange(len(frames)))
        # Outside the change the call is the one the same seed gives without it.
        before = steady.schedules[direction]
        assert np.array_equal(s.size[s.t_s < 9.9], before.size[before.t_s < 9.9])


def test_a_game_in_its_lobby_sends_a_few_packets_a_second():
    plan = planning.build(changing("gaming", [(20, "phase", "match")], phase="lobby", tick_hz=60), "x", 7)
    assert plan.params["phase"] == "lobby"
    s = plan.schedules["up"]
    assert (s.t_s < 20).sum() == pytest.approx(20 * planning.LOBBY_HZ, abs=2)
    assert (s.t_s >= 20).sum() == pytest.approx(20 * 60, abs=2)
    assert np.array_equal(s.frame, np.arange(len(s)))


def test_a_paused_download_is_in_the_plan_of_both_ends():
    plan = planning.build(changing("bulk_download", [(10, "paused", True), (25, "paused", False),
                                                     (35, "paused", True)]), "x", 7)
    assert plan.script["pauses"] == [[10.0, 25.0], [35.0, 40.0]]
    assert planning.build(scenario("bulk_download", 40), "x", 7).script == {}


def test_a_change_the_profile_does_not_have_is_refused():
    with pytest.raises(ValueError):
        planning.build(changing("voice_call", [(10, "camera", "off")]), "x", 7)
    with pytest.raises(ValueError):
        planning.build(changing("video_call", [(10, "camera", "dim")]), "x", 7)


def test_a_replayed_call_sends_what_was_recorded():
    """A video_call with a trace replays a real capture: its first packets,
    the ones the classifier sees, exactly as recorded, then the recording
    looped to the entry's length. The seed changes the ports only."""
    from aqos_ml import trace as traces
    recorded = traces.load("whatsapp-video-005937")
    plan = planning.build(scenario("video_call", 200, trace="whatsapp-video-005937"), "x", 3)
    other = planning.build(scenario("video_call", 200, trace="whatsapp-video-005937"), "x", 4)
    for direction in ("up", "down"):
        s, side = plan.schedules[direction], recorded[direction]
        n = 50
        assert np.allclose(s.t_s[:n], side["t_s"][:n])
        assert np.array_equal(s.size[:n], np.clip(side["size"][:n], HEADER_SIZE, planning.REPLAY_MAX_PAYLOAD))
        assert s.t_s[-1] < 200 and s.t_s[-1] > 199
        assert np.all(np.diff(s.t_s) >= 0)
        video = s.stream == MAIN
        frames = np.unique(s.frame[video])
        assert np.array_equal(frames, np.arange(len(frames)))
        assert np.all(s.index < s.count)
        assert np.all(s.size[~video] < planning.AUDIO_BELOW) and np.all(s.size[video] >= planning.AUDIO_BELOW)
        assert 15 < plan.params["fps_" + direction] < 35
        assert np.array_equal(s.size, other.schedules[direction].size)
    assert (plan.server_port, plan.client_port) != (other.server_port, other.client_port)


def test_a_replayed_call_cannot_switch_its_camera():
    doc = changing("video_call", [(10, "camera", "off")])
    doc["traffic"][0]["params"] = {"trace": "whatsapp-video-005937"}
    with pytest.raises(ValueError, match="replays a recorded call"):
        planning.build(doc, "x", 7)


def test_a_trace_is_named_not_pathed():
    with pytest.raises(ValueError):
        planning.build(scenario("video_call", 20, trace="../../etc/passwd"), "x", 7)

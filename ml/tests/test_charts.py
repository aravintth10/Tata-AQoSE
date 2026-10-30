from aqos_ml import charts


def test_every_configuration_has_its_own_colour_and_name():
    assert len(set(charts.COLOR.values())) == len(charts.COLOR) == 6
    assert set(charts.COLOR) == set(charts.NAME) == set(charts.SHORT)


def test_the_table_and_offline_charts_are_written(tmp_path):
    assert charts.damage_table(tmp_path).stat().st_size > 10_000
    assert charts.offline(tmp_path).stat().st_size > 10_000


def test_events_read_as_words():
    said = charts._event({"type": "traffic_param", "args": {"traffic": "call", "param": "camera", "value": "off"}})
    assert said == "call: camera off"
    assert charts._event({"type": "link_rate", "args": {"direction": "down", "rate_kbit": 20000}}) == "link down to 20 Mbit/s"
    assert charts._event({"type": "kill_controller"}) == "kill controller"

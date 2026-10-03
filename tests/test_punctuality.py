"""到发偏差、晚点传导与历史重放。"""

from __future__ import annotations

import pytest

from psdline.errors import RailError
from psdline.punctuality import service_date_of


def _plan(platform, run_id, calls, **kwargs):
    date = service_date_of(platform.clock.now())
    return platform.register_run_plan(run_id, date, calls, **kwargs)


def _call(station_id, arrival_at, departure_at):
    return {
        "station_id": station_id,
        "arrival_at": arrival_at,
        "departure_at": departure_at,
    }


def test_plan_baseline_deviation_is_separate_from_propagated_delay(platform):
    start = platform.clock.now()
    _plan(
        platform,
        "A",
        [_call("ST1", start, start), _call("ST2", start + 200, start + 240)],
        allow_arrival_delay_s=60,
        allow_departure_delay_s=60,
    )
    arrival = platform.record_actual_arrival("A", "ST1", start + 30)
    deviation = arrival["deviation"]
    assert deviation["arrival_deviation_s"] == 30
    assert deviation["arrival_delay_s"] == 30
    downstream = platform.punctuality.require_call("A", "ST2")
    assert downstream.propagated_arrival_delay_s == 30
    assert downstream.effective_arrival_delay_s == 30


def test_headway_delay_propagates_to_next_run_at_same_station(platform):
    start = platform.clock.now()
    _plan(
        platform,
        "A",
        [_call("ST1", start, start)],
        allow_arrival_delay_s=10,
        allow_departure_delay_s=999,
    )
    _plan(
        platform,
        "B",
        [_call("ST1", start + 120, start + 120)],
        allow_arrival_delay_s=10,
        allow_departure_delay_s=10,
    )
    platform.record_actual_arrival("A", "ST1", start)
    platform.record_actual_departure("A", "ST1", start + 300)
    following = platform.punctuality.require_call("B", "ST1")
    assert following.earliest_arrival_at == start + 420
    assert following.effective_arrival_delay_s == 300
    assert following.forecast_arrival_breach is True
    assert following.release_blocked is True


def test_plan_is_immutable_after_first_actual(platform):
    start = platform.clock.now()
    _plan(platform, "A", [_call("ST1", start, start)])
    platform.record_actual_arrival("A", "ST1", start + 1)
    with pytest.raises(RailError):
        _plan(platform, "A", [_call("ST1", start + 2, start + 2)])
    with pytest.raises(RailError):
        platform.record_actual_arrival("A", "ST1", start + 3)


def test_replay_historical_prefix_remains_stable(platform):
    start = platform.clock.now()
    date = service_date_of(platform.clock.now())
    _plan(
        platform,
        "A",
        [_call("ST1", start, start), _call("ST2", start + 200, start + 240)],
    )
    assert platform.replay_punctuality(1, date)["summary"]["calls"] == 2
    platform.record_actual_arrival("A", "ST1", start + 20)
    replay = platform.replay_punctuality(2, date)
    assert replay["summary"]["arrived"] == 1
    current = platform.punctuality_status(date)
    assert current["projection_event_count"] == 2
    assert replay["as_of_seq"] == 1
    assert replay["calls"][0]["actual_arrival_at"] is None
    assert replay["calls"][1]["propagated_arrival_delay_s"] == 0


def test_live_arrival_and_departure_share_one_projection(platform):
    start = platform.clock.now()
    _plan(
        platform,
        "T01",
        [_call("ST1", start, start + 60)],
        allow_arrival_delay_s=999,
        allow_departure_delay_s=999,
    )
    arrival = platform.arrive("T01", "ST1", 0)
    assert arrival["punctuality"]["status"] == "arrived"
    platform.check_gap("T01")
    early = platform.depart("T01", "ST1")
    assert early["ok"] is False
    assert any("最早可发车" in item for item in early["violations"])
    platform.clock.advance(60)
    departure = platform.depart("T01", "ST1")
    assert departure["ok"] is True
    assert departure["punctuality"]["status"] == "departed"


def test_overdue_live_departure_is_held(platform):
    start = platform.clock.now()
    _plan(
        platform,
        "T01",
        [_call("ST1", start, start)],
        allow_arrival_delay_s=10,
        allow_departure_delay_s=10,
    )
    platform.clock.advance(20)
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    departure = platform.depart("T01", "ST1")
    assert departure["ok"] is False
    assert departure["punctuality"]["arrival_breach"] is True


def test_rebuild_after_restart_returns_same_projection(tmp_path, clock):
    from psdline.app import build_platform
    from psdline.config import default_config

    start = clock.now()
    first = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"))
    first.start()
    date = service_date_of(start)
    first.register_run_plan("A", date, [_call("ST1", start, start)])
    first.record_actual_arrival("A", "ST1", start + 15)
    expected = first.punctuality_status(date)

    second = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"))
    restored = second.punctuality_status(date)
    assert restored["calls"] == expected["calls"]
    assert restored["event_count"] == expected["event_count"]

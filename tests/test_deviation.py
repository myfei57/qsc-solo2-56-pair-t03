"""到发偏差、晚点传播与历史重放。"""

from __future__ import annotations

import pytest

from psdline.clock import ManualClock
from psdline.deviation import DeviationEventStore, DeviationService
from psdline.errors import RailError
from psdline.console.api import Api
from psdline.store import JsonlJournal


def _service(tmp_path, clock: ManualClock) -> DeviationService:
    journal = JsonlJournal(str(tmp_path / "deviation"), "deviation")
    return DeviationService(
        clock,
        DeviationEventStore(journal),
        station_order=["ST1", "ST2"],
        allowed_arrival_s=60.0,
        allowed_departure_s=60.0,
        default_headway_s=120.0,
    )


def _seed(service: DeviationService) -> None:
    service.register_plan("T01", "ST1", 0.0, 0.0, version="v1")
    service.register_plan("T02", "ST1", 120.0, 120.0, version="v1")
    service.register_plan("T01", "ST2", 300.0, 360.0, version="v1")
    service.register_plan("T02", "ST2", 420.0, 480.0, version="v1")


def test_delay_propagates_to_following_train_and_downstream_station(tmp_path, clock):
    service = _service(tmp_path, clock)
    _seed(service)
    service.register_actual_arrival("T01", "ST1", 100.0)
    service.register_actual_departure("T01", "ST1", 100.0)

    t02_st1 = service.stop_deviation("T02", "ST1")
    t01_st2 = service.stop_deviation("T01", "ST2")
    t02_st2 = service.stop_deviation("T02", "ST2")

    assert t02_st1.arrival_deviation_s == 100.0
    assert t02_st1.headway_delay_s == 100.0
    assert t02_st1.projected_arrival == 220.0
    assert t01_st2.upstream_delay_s == 100.0
    assert t01_st2.arrival_deviation_s == 100.0
    assert t02_st2.headway_delay_s == 100.0
    assert t02_st2.upstream_delay_s == 100.0


def test_over_tolerance_departure_is_held_until_acknowledged(tmp_path, clock):
    service = _service(tmp_path, clock)
    _seed(service)
    service.register_actual_arrival("T01", "ST1", 100.0)

    held = service.release_decision("T01", "ST1", at=100.0)
    assert held["ok"] is False
    assert "departure_deviation_exceeded" in held["violations"]

    service.acknowledge_delay("T01", "ST1", reason="调度确认")
    released = service.release_decision("T01", "ST1", at=100.0)
    assert released["ok"] is True
    assert released["acknowledged"] is True


def test_events_are_immutable_and_history_replays_consistently(tmp_path, clock):
    service = _service(tmp_path, clock)
    _seed(service)
    service.register_actual_arrival("T01", "ST1", 100.0)
    cutoff = service.view().sequence

    service.register_plan("T02", "ST1", 200.0, 200.0, version="v2")
    service.register_plan("T02", "ST2", 500.0, 560.0, version="v2")

    historical = service.replay(sequence=cutoff)
    current = service.view()
    assert [stop.version for stop in historical.stops if stop.train_id == "T02"] == ["v1", "v1"]
    assert [stop.version for stop in current.stops if stop.train_id == "T02"] == ["v2", "v2"]

    with pytest.raises(RailError):
        service.register_plan("T01", "ST1", 1.0, 2.0, version="v1")
    with pytest.raises(RailError):
        service.register_actual_arrival("T01", "ST1", 101.0)


def test_platform_holds_and_acknowledges_departure(platform):
    platform.clock.advance(61)
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")

    held = platform.depart("T01", "ST1")
    assert held["ok"] is False
    assert "发车偏差超过允许值且未确认" in held["violations"]

    released = platform.depart("T01", "ST1", acknowledge_delay=True)
    assert released["ok"] is True
    assert released["release"]["acknowledged"] is True


def test_platform_deviation_appears_in_shared_views(platform):
    platform.arrive("T01", "ST1", 0)
    status = platform.status()
    station = platform.station_view("ST1")
    train = platform.train_view("T01")

    assert status["deviation"]["sequence"] >= 1
    assert any(item["train_id"] == "T01" for item in station["deviations"])
    assert any(item["station_id"] == "ST1" for item in train["deviations"])


def test_deviation_api_and_replay(platform):
    api = Api(platform)
    status, view = api.handle("GET", "/api/deviation", {}, {})
    assert status == 200
    assert view["baseline"] == "planned"

    status, response = api.handle(
        "POST",
        "/api/deviation/plan",
        {},
        {
            "train_id": "T99",
            "station_id": "ST1",
            "planned_arrival": 10.0,
            "planned_departure": 20.0,
            "version": "night",
        },
    )
    assert status == 200 and response["ok"] is True
    cutoff = view["sequence"]
    status, replay = api.handle("POST", "/api/deviation/replay", {}, {"sequence": cutoff})
    assert status == 200
    assert all(stop["train_id"] != "T99" for stop in replay["stops"])


def test_event_store_restarts_and_rebuilds_view(tmp_path, clock):
    journal = JsonlJournal(str(tmp_path / "deviation"), "deviation")
    first = DeviationService(
        clock,
        DeviationEventStore(journal),
        station_order=["ST1"],
        allowed_arrival_s=60.0,
        allowed_departure_s=60.0,
        default_headway_s=120.0,
    )
    first.register_plan("T01", "ST1", 0.0, 0.0, version="v1")
    first.register_actual_arrival("T01", "ST1", 90.0)

    second = DeviationService(
        clock,
        DeviationEventStore(journal),
        station_order=["ST1"],
        allowed_arrival_s=60.0,
        allowed_departure_s=60.0,
        default_headway_s=120.0,
    )
    stop = second.stop_deviation("T01", "ST1")
    assert stop.arrival_deviation_s == 90.0
    assert second.view().sequence == 2

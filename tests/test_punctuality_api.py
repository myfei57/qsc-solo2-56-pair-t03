"""到发偏差控制台接口。"""

from __future__ import annotations

from psdline.console.api import Api
from psdline.punctuality import service_date_of


def test_punctuality_api_plan_actual_and_replay(platform):
    api = Api(platform)
    start = platform.clock.now()
    service_date = service_date_of(start)
    status_code, payload = api.handle(
        "POST",
        "/api/punctuality/plans",
        {},
        {
            "run_id": "T01",
            "service_date": service_date,
            "calls": [
                {"station_id": "ST1", "arrival_at": start, "departure_at": start},
                {"station_id": "ST2", "arrival_at": start + 200, "departure_at": start + 240},
            ],
            "allow_arrival_delay_s": 10,
            "allow_departure_delay_s": 10,
        },
    )
    assert status_code == 200 and payload["plan"]["revision"] == 1

    status_code, arrival = api.handle(
        "POST",
        "/api/punctuality/arrivals",
        {},
        {"run_id": "T01", "station_id": "ST1", "at": start + 20},
    )
    assert status_code == 200 and arrival["deviation"]["arrival_delay_s"] == 20

    status_code, run = api.handle("GET", "/api/punctuality/runs/T01", {}, {})
    assert status_code == 200 and run["summary"]["arrived"] == 1

    status_code, station = api.handle(
        "GET", "/api/punctuality/stations/ST1", {"service_date": service_date}, {}
    )
    assert status_code == 200 and station["summary"]["calls"] == 1

    status_code, replay = api.handle("GET", "/api/punctuality/replay/1", {}, {})
    assert status_code == 200 and replay["as_of_seq"] == 1
    assert replay["calls"][0]["actual_arrival_at"] is None

    status_code, status = api.handle("GET", "/api/punctuality", {}, {})
    assert status_code == 200 and status["event_count"] == 2


def test_punctuality_api_rejects_bad_plan(platform):
    api = Api(platform)
    service_date = service_date_of(platform.clock.now())
    status_code, payload = api.handle(
        "POST",
        "/api/punctuality/plans",
        {},
        {"run_id": "T01", "service_date": service_date, "calls": []},
    )
    assert status_code == 400 and payload["component"] == "console"

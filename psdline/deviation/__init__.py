"""到发偏差与晚点传播。"""

from __future__ import annotations

from .engine import materialize
from .model import ActualStop, DeviationView, PlannedStop, StopDeviation
from .service import DeviationService
from .store import (
    ACTUAL_ARRIVAL,
    ACTUAL_DEPARTURE,
    DELAY_ACKNOWLEDGED,
    PLANNED,
    DeviationEvent,
    DeviationEventStore,
    planned_from_event,
)


def deviation_summary(view: DeviationView) -> dict:
    """汇总一份偏差视图，供状态接口与审计概览共用。"""

    stops = view.stops
    late_arrivals = [stop for stop in stops if stop.scheduled and stop.arrival_late]
    late_departures = [
        stop
        for stop in stops
        if stop.scheduled
        and stop.effective_departure - stop.planned_departure > view.allowed_departure_s
    ]
    held = [stop for stop in stops if stop.release_hold]
    return {
        "sequence": view.sequence,
        "active_version": view.active_version,
        "versions": list(view.versions),
        "stops": len(stops),
        "late_arrivals": len(late_arrivals),
        "late_departures": len(late_departures),
        "departure_holds": len(held),
        "headway_breaches": sum(1 for stop in stops if stop.arrival_headway_breach),
        "max_arrival_deviation_s": max((stop.arrival_deviation_s for stop in stops), default=0.0),
        "max_departure_deviation_s": max((stop.departure_deviation_s for stop in stops), default=0.0),
        "baseline": "planned",
        "propagation": "full_recompute",
    }


__all__ = [
    "ACTUAL_ARRIVAL",
    "ACTUAL_DEPARTURE",
    "DELAY_ACKNOWLEDGED",
    "PLANNED",
    "ActualStop",
    "DeviationEvent",
    "DeviationEventStore",
    "DeviationService",
    "DeviationView",
    "PlannedStop",
    "StopDeviation",
    "deviation_summary",
    "materialize",
    "planned_from_event",
]

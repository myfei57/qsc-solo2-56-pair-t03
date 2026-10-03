"""偏差登记、重算与发车放行判定。"""

from __future__ import annotations

import threading
from typing import Callable

from ..clock import Clock
from ..errors import RailError
from .engine import materialize
from .model import ActualStop, DeviationView, PlannedStop, StopDeviation
from .store import (
    ACTUAL_ARRIVAL,
    ACTUAL_DEPARTURE,
    DELAY_ACKNOWLEDGED,
    PLANNED,
    DeviationEvent,
    DeviationEventStore,
    actual_from_events,
)


class DeviationService:
    """排班、放行、审计共用的偏差服务。"""

    def __init__(
        self,
        clock: Clock,
        events: DeviationEventStore,
        *,
        station_order: list[str],
        allowed_arrival_s: float = 60.0,
        allowed_departure_s: float = 60.0,
        default_headway_s: float = 120.0,
        headway_provider: Callable[[str], float] | None = None,
    ) -> None:
        self._clock = clock
        self._events = events
        self._station_order = list(station_order)
        self._allowed_arrival_s = allowed_arrival_s
        self._allowed_departure_s = allowed_departure_s
        self._default_headway_s = default_headway_s
        self._headway_provider = headway_provider
        self._lock = threading.RLock()
        self._view = self._materialize_locked()

    # ---- 登记 ---------------------------------------------------------

    def register_plan(
        self,
        train_id: str,
        station_id: str,
        planned_arrival: float,
        planned_departure: float,
        *,
        version: str = "",
        headway_s: float | None = None,
    ) -> DeviationEvent:
        """登记一班车一个站台的计划到发；同一版本同点不可重复。"""

        version = version or self._next_version()
        headway = self._resolve_headway(station_id, headway_s)
        plan = PlannedStop(
            train_id=train_id,
            station_id=station_id,
            version=version,
            planned_arrival=float(planned_arrival),
            planned_departure=float(planned_departure),
            headway_s=headway,
            sequence=self._events.sequence,
        )
        self._validate_plan(plan)
        with self._lock:
            for existing in self._plans_locked():
                if (
                    existing.train_id == train_id
                    and existing.station_id == station_id
                    and existing.version == version
                ):
                    raise RailError(
                        f"{version} 中 {train_id}@{station_id} 的计划已存在，历史班次不可覆盖",
                        component="deviation",
                    )
            event = self._events.append(PLANNED, plan.as_dict(), self._clock.now())
            self._invalidate_locked()
        return event

    def register_actual_arrival(
        self,
        train_id: str,
        station_id: str,
        at: float | None = None,
        *,
        version: str = "",
    ) -> DeviationEvent:
        """登记实际到站；重复到站会被拒绝。"""

        moment = self._clock.now() if at is None else float(at)
        with self._lock:
            plan = self._require_plan_locked(train_id, station_id, version)
            self._require_no_actual_locked(
                train_id, station_id, ACTUAL_ARRIVAL, plan.version
            )
            payload = {
                "train_id": train_id,
                "station_id": station_id,
                "version": plan.version,
                "actual_arrival": moment,
            }
            event = self._events.append(ACTUAL_ARRIVAL, payload, self._clock.now())
            self._invalidate_locked()
        return event

    def register_unplanned_actual_arrival(self, train_id: str, station_id: str, at: float) -> DeviationEvent:
        """登记没有排班的实际到站；发车仍会因未排班而被扣留。"""

        with self._lock:
            event = self._events.append(
                ACTUAL_ARRIVAL,
                {
                    "train_id": train_id,
                    "station_id": station_id,
                    "version": "",
                    "actual_arrival": float(at),
                },
                self._clock.now(),
            )
            self._invalidate_locked()
        return event

    def register_actual_departure(
        self,
        train_id: str,
        station_id: str,
        at: float | None = None,
    ) -> DeviationEvent:
        """登记实际发车；必须先有实际到站，重复发车会被拒绝。"""

        moment = self._clock.now() if at is None else float(at)
        with self._lock:
            actual = self._latest_actual_locked(train_id, station_id)
            if actual is None or actual.actual_arrival is None:
                raise RailError(f"{train_id}@{station_id} 尚未登记实际到站", component="deviation")
            version = actual.version
            self._require_no_actual_locked(train_id, station_id, ACTUAL_DEPARTURE, version)
            payload = {
                "train_id": train_id,
                "station_id": station_id,
                "version": actual.version,
                "actual_departure": moment,
            }
            event = self._events.append(ACTUAL_DEPARTURE, payload, self._clock.now())
            self._invalidate_locked()
        return event

    def register_unplanned_actual_departure(self, train_id: str, station_id: str, at: float) -> DeviationEvent:
        """登记没有排班的实际发车，供审计和历史重放保留事实。"""

        with self._lock:
            latest = self._latest_actual_locked(train_id, station_id)
            if latest is not None and latest.actual_departure is not None:
                raise RailError(f"{train_id}@{station_id} 的实际发车已登记", component="deviation")
            event = self._events.append(
                ACTUAL_DEPARTURE,
                {
                    "train_id": train_id,
                    "station_id": station_id,
                    "version": "",
                    "actual_departure": float(at),
                },
                self._clock.now(),
            )
            self._invalidate_locked()
        return event

    def acknowledge_delay(
        self,
        train_id: str,
        station_id: str,
        *,
        version: str = "",
        reason: str = "",
    ) -> DeviationEvent:
        """值班员确认已知悉超允许偏差；确认记录不可删除。"""

        with self._lock:
            plan = self._require_plan_locked(train_id, station_id, version)
            key = (train_id, station_id, plan.version)
            if key in self._acknowledged_locked():
                raise RailError(f"{train_id}@{station_id} 的晚点已确认", component="deviation")
            payload = {
                "train_id": train_id,
                "station_id": station_id,
                "version": plan.version,
                "reason": reason,
            }
            event = self._events.append(DELAY_ACKNOWLEDGED, payload, self._clock.now())
            self._invalidate_locked()
        return event

    # ---- 查询 ---------------------------------------------------------

    def view(self) -> DeviationView:
        """返回当前事件流对应的偏差视图。"""

        with self._lock:
            return self._view

    def stop_deviation(self, train_id: str, station_id: str) -> StopDeviation | None:
        with self._lock:
            return self._find_stop_locked(self._view, train_id, station_id)

    def station_deviations(self, station_id: str) -> list[StopDeviation]:
        with self._lock:
            return [item for item in self._view.stops if item.station_id == station_id]

    def release_decision(self, train_id: str, station_id: str, at: float | None = None) -> dict:
        """发车放行前读取同一份偏差视图并给出可否发车。"""

        moment = self._clock.now() if at is None else float(at)
        with self._lock:
            stop = self._find_stop_locked(self._view, train_id, station_id)
            latest = self._latest_actual_locked(train_id, station_id)
            if (
                latest is not None
                and latest.actual_departure is not None
                and stop is not None
                and latest.version == stop.version
            ):
                return {
                    "ok": False,
                    "train_id": train_id,
                    "station_id": station_id,
                    "hold": True,
                    "reasons": ["实际发车已登记，历史到发不可重放"],
                    "violations": ["actual_departure_exists"],
                    "hold_until": None,
                }
            stop = self._find_stop_locked(self._view, train_id, station_id)
            if stop is None:
                return {
                    "ok": False,
                    "train_id": train_id,
                    "station_id": station_id,
                    "hold": True,
                    "reasons": ["计划到发缺失"],
                    "violations": ["schedule_missing"],
                    "hold_until": None,
                }
            if not stop.scheduled:
                return {
                    "ok": False,
                    "train_id": train_id,
                    "station_id": station_id,
                    "hold": True,
                    "reasons": ["实际到发没有对应班次计划"],
                    "violations": ["unscheduled_stop"],
                    "hold_until": None,
                }
            if stop.actual_arrival is None:
                return {
                    "ok": False,
                    "train_id": train_id,
                    "station_id": station_id,
                    "hold": True,
                    "reasons": ["实际到站尚未登记"],
                    "violations": ["actual_arrival_missing"],
                    "hold_until": stop.projected_arrival,
                }
            violations: list[str] = []
            reasons: list[str] = []
            if stop.arrival_headway_breach and not stop.delay_acknowledged:
                violations.append("arrival_headway_breach")
                reasons.append("到站追踪间隔不足")
            departure_at = max(moment, stop.effective_arrival)
            departure_deviation = departure_at - stop.planned_departure
            delay_late = departure_deviation > self._allowed_departure_s
            before_projection = departure_at < stop.projected_departure
            if delay_late and not stop.delay_acknowledged:
                violations.append("departure_deviation_exceeded")
                reasons.append("发车偏差超过允许值且未确认")
            if before_projection:
                violations.append("before_projected_departure")
                reasons.append("尚未到追踪计算的可发车时刻")
            hold_until = stop.projected_departure
            if delay_late and not stop.delay_acknowledged:
                hold_until = max(hold_until, stop.planned_departure + self._allowed_departure_s)
            return {
                "ok": not violations,
                "train_id": train_id,
                "station_id": station_id,
                "hold": bool(violations),
                "reasons": reasons,
                "violations": violations,
                "departure_deviation_s": departure_deviation,
                "allowed_deviation_s": self._allowed_departure_s,
                "acknowledged": stop.delay_acknowledged,
                "hold_until": hold_until,
                "deviation": stop.as_dict(),
            }

    def replay(self, *, at: float | None = None, sequence: int | None = None) -> DeviationView:
        """按登记时间或事件序号截断事件流，全量重算历史视图。"""

        with self._lock:
            events = self._events.list()
            if at is not None:
                events = [event for event in events if event.registered_at <= float(at)]
            if sequence is not None:
                events = events[: max(0, int(sequence))]
            return self._materialize_locked(
                events=events,
                sequence=len(events),
                use_event_sequence=True,
            )

    # ---- 内部 ---------------------------------------------------------

    def _invalidate_locked(self) -> None:
        self._view = self._materialize_locked()

    def _materialize_locked(
        self,
        events: list[DeviationEvent] | None = None,
        sequence: int | None = None,
        use_event_sequence: bool = False,
    ):
        events = self._events.list() if events is None else events
        plans = [PlannedStop.from_dict(event.payload) for event in events if event.type == PLANNED]
        actuals = actual_from_events(events)
        acknowledged = {
            (
                str(event.payload.get("train_id", "")),
                str(event.payload.get("station_id", "")),
                str(event.payload.get("version", "")),
            )
            for event in events
            if event.type == DELAY_ACKNOWLEDGED
        }
        return materialize(
            plans,
            actuals,
            station_order=self._station_order,
            current_time=self._clock.now(),
            allowed_arrival_s=self._allowed_arrival_s,
            allowed_departure_s=self._allowed_departure_s,
            default_headway_s=self._default_headway_s,
            acknowledged=acknowledged,
            sequence=(
                events[-1].sequence + 1
                if use_event_sequence and events
                else self._events.sequence if sequence is None else sequence
            ),
        )

    def _plans_locked(self) -> list[PlannedStop]:
        return [PlannedStop.from_dict(event.payload) for event in self._events.list() if event.type == PLANNED]

    def _latest_actual_locked(self, train_id: str, station_id: str) -> ActualStop | None:
        matches = [
            actual
            for actual in actual_from_events(self._events.list())
            if actual.train_id == train_id and actual.station_id == station_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: max(item.arrival_sequence, item.departure_sequence))

    def _acknowledged_locked(self) -> set[tuple[str, str, str]]:
        return {
            (
                str(event.payload.get("train_id", "")),
                str(event.payload.get("station_id", "")),
                str(event.payload.get("version", "")),
            )
            for event in self._events.list()
            if event.type == DELAY_ACKNOWLEDGED
        }

    def _require_plan_locked(
        self,
        train_id: str,
        station_id: str,
        version: str,
    ) -> PlannedStop:
        matches = [
            plan
            for plan in self._plans_locked()
            if plan.train_id == train_id and plan.station_id == station_id
        ]
        if not matches:
            raise RailError(f"{train_id}@{station_id} 没有计划到发", component="deviation")
        if version:
            for plan in matches:
                if plan.version == version:
                    return plan
            raise RailError(f"{train_id}@{station_id} 不存在排班版本 {version}", component="deviation")
        return max(matches, key=lambda item: (item.sequence, item.version))

    def _require_no_actual_locked(
        self,
        train_id: str,
        station_id: str,
        event_type: str,
        version: str,
    ) -> None:
        if any(
            event.type == event_type
            and event.payload.get("train_id") == train_id
            and event.payload.get("station_id") == station_id
            and event.payload.get("version") == version
            for event in self._events.list()
        ):
            label = "到站" if event_type == ACTUAL_ARRIVAL else "发车"
            raise RailError(f"{train_id}@{station_id} 的实际{label}已登记", component="deviation")

    def _find_stop_locked(self, view: DeviationView, train_id: str, station_id: str):
        for stop in view.stops:
            if stop.train_id == train_id and stop.station_id == station_id:
                return stop
        return None

    def _next_version(self) -> str:
        versions = {plan.version for plan in self._plans_locked()}
        index = 1
        while f"shift-{index}" in versions:
            index += 1
        return f"shift-{index}"

    def _resolve_headway(self, station_id: str, headway_s: float | None) -> float:
        if headway_s is not None:
            value = float(headway_s)
        elif self._headway_provider is not None:
            value = float(self._headway_provider(station_id))
        else:
            value = self._default_headway_s
        if value <= 0:
            raise RailError("追踪间隔必须为正", component="deviation")
        return value

    @staticmethod
    def _validate_plan(plan: PlannedStop) -> None:
        if not plan.train_id or not plan.station_id or not plan.version:
            raise RailError("计划到发的列车、站台和版本不能为空", component="deviation")
        if plan.planned_departure < plan.planned_arrival:
            raise RailError("计划发车不能早于计划到站", component="deviation")
        if plan.headway_s <= 0:
            raise RailError("追踪间隔必须为正", component="deviation")

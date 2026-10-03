"""到发偏差事件服务。

排班、发车放行和审计都通过这一个服务读取同一份投影。计划与实绩事件只追加到
JSONL；任何修订或补录后都由 :func:`project_punctuality` 全量重算，不叠加增量，
因此按历史事件前缀重放时结果固定，且旧事件不会被改写。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Collection, Sequence
from dataclasses import replace

from ..errors import RailError
from ..store import JsonlJournal
from ..clock import Clock
from .models import CallDeviation, PlannedCall, RunPlan, validate_plan_calls
from .projection import PunctualityProjection, project_punctuality

ACTUAL_ARRIVAL = "arrival"
ACTUAL_DEPARTURE = "departure"


class PunctualityService:
    """计划、实绩、偏差投影与发车放行判定的统一入口。"""

    def __init__(
        self,
        journal: JsonlJournal,
        clock: Clock,
        known_stations: Collection[str],
        headways: dict[str, float] | None = None,
        *,
        default_arrival_tolerance_s: float = 120.0,
        default_departure_tolerance_s: float = 120.0,
        default_headway_s: float = 120.0,
    ) -> None:
        self._journal = journal
        self._clock = clock
        self._known_stations = set(known_stations)
        self._configured_headways = {key: float(value) for key, value in (headways or {}).items()}
        self._default_arrival_tolerance_s = default_arrival_tolerance_s
        self._default_departure_tolerance_s = default_departure_tolerance_s
        self._default_headway_s = default_headway_s
        self._lock = threading.RLock()
        self._events: list[dict] = self._journal.read_all()
        self._projection = self._build()

    @property
    def events(self) -> list[dict]:
        with self._lock:
            return [dict(event) for event in self._events]

    @property
    def projection(self) -> PunctualityProjection:
        with self._lock:
            return self._projection

    def register_plan(
        self,
        run_id: str,
        service_date: str,
        calls: Sequence[PlannedCall],
        *,
        allow_arrival_delay_s: float | None = None,
        allow_departure_delay_s: float | None = None,
    ) -> RunPlan:
        """登记或修订计划；出现任一实绩后计划冻结。"""

        plan_calls = tuple(calls)
        validate_plan_identity(run_id, service_date)
        validate_plan_calls(plan_calls)
        for call in plan_calls:
            if call.station_id not in self._known_stations:
                raise RailError(f"计划站台不属于当前线路: {call.station_id}", component="punctuality")
        _validate_tolerance(allow_arrival_delay_s, "到站允许晚点")
        _validate_tolerance(allow_departure_delay_s, "发车允许晚点")
        with self._lock:
            existing = self._latest_plan(run_id)
            if self._actual_count(run_id):
                raise RailError(f"车次 {run_id} 已有实绩，计划不可再修订", component="punctuality")
            revision = 1 if existing is None else existing.revision + 1
            if existing is not None and existing.service_date != service_date:
                raise RailError("计划修订不能改变班次日期", component="punctuality")
            plan = RunPlan(
                run_id=run_id,
                service_date=service_date,
                calls=plan_calls,
                revision=revision,
                allow_arrival_delay_s=allow_arrival_delay_s,
                allow_departure_delay_s=allow_departure_delay_s,
            )
            self._append(
                {
                    **plan.as_dict(),
                    "type": "run_plan",
                    "sealed": False,
                }
            )
            return RunPlan.from_dict(self._events[-1])

    def record_arrival(self, run_id: str, station_id: str, at: float | None = None) -> CallDeviation:
        """登记实际到站；用于人工补录和现场到站自动落账。"""

        self._record_actual(run_id, station_id, ACTUAL_ARRIVAL, at)
        return self.require_call(run_id, station_id)

    def record_departure(self, run_id: str, station_id: str, at: float | None = None) -> CallDeviation:
        """登记实际发车；只允许在安全和放行条件都通过后调用。"""

        self._record_actual(run_id, station_id, ACTUAL_DEPARTURE, at)
        return self.require_call(run_id, station_id)

    def depart_decision(self, run_id: str, station_id: str, at: float | None = None) -> dict:
        """返回当前车次/站台的发车放行判定，不写事件。"""

        moment = self._clock.now() if at is None else float(at)
        with self._lock:
            call = self._projection.find(run_id, station_id)
            if call is None:
                return {
                    "ok": False,
                    "reason": "no_plan",
                    "run_id": run_id,
                    "station_id": station_id,
                    "violations": [f"车次 {run_id} 在站台 {station_id} 没有已冻结计划"],
                }
            if call.actual_departure_at is not None:
                return {
                    "ok": False,
                    "reason": "already_departed",
                    "run_id": run_id,
                    "station_id": station_id,
                    "actual_departure_at": call.actual_departure_at,
                    "violations": ["实际发车已登记，不能重复放行"],
                }
            violations = list(call.violations)
            if call.actual_arrival_at is None:
                violations.append("尚未登记实际到站")
            wait_s = max(0.0, round(call.earliest_departure_at - moment, 3))
            if wait_s > 0:
                violations.append(f"未到最早可发车时刻，需等待 {wait_s:.0f}s")
            allowed = not violations
            return {
                "ok": allowed,
                "reason": "allowed" if allowed else "punctuality_hold",
                "run_id": run_id,
                "service_date": call.service_date,
                "station_id": station_id,
                "at": moment,
                "wait_s": wait_s,
                "earliest_departure_at": call.earliest_departure_at,
                "plan_departure_at": call.plan_departure_at,
                "effective_departure_delay_s": call.effective_departure_delay_s,
                "arrival_breach": call.arrival_breach,
                "forecast_arrival_breach": call.forecast_arrival_breach,
                "departure_breach": call.departure_breach,
                "headway_breach": call.headway_breach,
                "release_blocked": call.release_blocked or bool(wait_s),
                "deviation": call.as_dict(),
                "violations": violations,
            }

    def find_live_arrival(
        self,
        train_id: str,
        at: float | None = None,
        service_date: str | None = None,
    ) -> CallDeviation | None:
        """为只有列车标识的现场到站找唯一未到站计划停站。"""

        moment = self._clock.now() if at is None else float(at)
        shift = service_date or service_date_of(moment)
        candidates = [
            call
            for call in self._projection.for_run(train_id)
            if call.service_date == shift
            and call.actual_arrival_at is None
            and call.actual_departure_at is None
            and call.plan_arrival_at <= moment
        ]
        if len(candidates) != 1:
            return None
        return candidates[0]

    def find_live_departure(self, train_id: str, station_id: str, at: float | None = None) -> CallDeviation | None:
        """为现场发车找当前班次的未发车停站。"""

        moment = self._clock.now() if at is None else float(at)
        for call in self._projection.for_run(train_id):
            if (
                call.station_id == station_id
                and call.service_date == service_date_of(moment)
                and call.actual_arrival_at is not None
                and call.actual_departure_at is None
            ):
                return call
        return None

    def require_call(self, run_id: str, station_id: str) -> CallDeviation:
        call = self._projection.find(run_id, station_id)
        if call is None:
            raise RailError(f"找不到 {run_id} 在 {station_id} 的到发偏差", component="punctuality")
        return call

    def run_view(self, run_id: str) -> dict:
        plan = self._latest_plan(run_id)
        if plan is not None and self._actual_count(run_id):
            plan = replace(plan, sealed=True)
        calls = self._projection.for_run(run_id)
        return {
            "run_id": run_id,
            "plan": plan.as_dict() if plan else None,
            "deviations": [call.as_dict() for call in calls],
            "summary": summarize_calls(calls),
        }

    def station_view(self, station_id: str, service_date: str = "") -> dict:
        calls = self._projection.for_station(station_id)
        if service_date:
            calls = tuple(call for call in calls if call.service_date == service_date)
        return {
            "station_id": station_id,
            "service_date": service_date,
            "calls": [call.as_dict() for call in calls],
            "summary": summarize_calls(calls),
        }

    def status(self, service_date: str = "") -> dict:
        calls = self._projection.calls
        if service_date:
            calls = tuple(call for call in calls if call.service_date == service_date)
        return {
            "service_date": service_date,
            "event_count": len(self._events),
            "projection_event_count": self._projection.event_count,
            "revision": len(self._events),
            "calls": [call.as_dict() for call in calls],
            "summary": summarize_calls(calls),
        }

    def replay(self, as_of_seq: int, service_date: str = "") -> dict:
        """按历史事件前缀重建，用于不可变历史班次核对。"""

        projection = self._build(as_of_seq=as_of_seq)
        calls = projection.calls
        if service_date:
            calls = tuple(call for call in calls if call.service_date == service_date)
        return {
            "as_of_seq": projection.event_count,
            "service_date": service_date,
            "calls": [call.as_dict() for call in calls],
            "summary": summarize_calls(calls),
        }

    def _record_actual(self, run_id: str, station_id: str, kind: str, at: float | None) -> None:
        observed = self._clock.now() if at is None else float(at)
        if observed < 0:
            raise RailError("实际到发时刻不能为负", component="punctuality")
        with self._lock:
            plan = self._latest_plan(run_id)
            if plan is None:
                raise RailError(f"车次 {run_id} 尚未登记计划", component="punctuality")
            ordered = list(plan.calls)
            indexes = {call.station_id: index for index, call in enumerate(ordered)}
            if station_id not in indexes:
                raise RailError(f"站台 {station_id} 不在车次 {run_id} 的计划里", component="punctuality")
            index = indexes[station_id]
            key = (run_id, station_id)
            arrivals = self._actuals(ACTUAL_ARRIVAL)
            departures = self._actuals(ACTUAL_DEPARTURE)
            if kind == ACTUAL_ARRIVAL:
                if key in arrivals:
                    raise RailError("实际到站已存在，历史实绩不可覆盖", component="punctuality")
                if key in departures:
                    raise RailError("该停站已有实际发车，不能补录到站", component="punctuality")
            else:
                if key not in arrivals:
                    raise RailError("必须先登记实际到站，才能登记实际发车", component="punctuality")
                if key in departures:
                    raise RailError("实际发车已存在，历史实绩不可覆盖", component="punctuality")
                if observed < float(arrivals[key]["at"]):
                    raise RailError("实际发车不能早于本站实际到站", component="punctuality")

            if index > 0:
                previous_key = (run_id, ordered[index - 1].station_id)
                previous_departure = departures.get(previous_key)
                if previous_departure is not None and observed < float(previous_departure["at"]):
                    raise RailError("实际到发早于上一站实际发车", component="punctuality")
            if index + 1 < len(ordered):
                next_key = (run_id, ordered[index + 1].station_id)
                next_arrival = arrivals.get(next_key)
                if next_arrival is not None and float(next_arrival["at"]) < observed:
                    raise RailError("下一站实际到站早于本次实际到发", component="punctuality")
            self._append(
                {
                    "type": "actual",
                    "kind": kind,
                    "run_id": run_id,
                    "service_date": plan.service_date,
                    "station_id": station_id,
                    "at": observed,
                    "recorded_at": self._clock.now(),
                }
            )

    def _append(self, event: dict) -> None:
        seq = len(self._events) + 1
        payload = {
            "schema": "punctuality/v1",
            "seq": seq,
            "recorded_at": self._clock.now(),
            **event,
        }
        self._journal.append(payload)
        self._events.append(payload)
        self._projection = self._build()

    def _build(self, as_of_seq: int | None = None) -> PunctualityProjection:
        return project_punctuality(
            self._events,
            self._headways(),
            default_arrival_tolerance_s=self._default_arrival_tolerance_s,
            default_departure_tolerance_s=self._default_departure_tolerance_s,
            default_headway_s=self._default_headway_s,
            as_of_seq=as_of_seq,
        )

    def _headways(self) -> dict[str, float]:
        return dict(self._configured_headways)

    def _latest_plan(self, run_id: str) -> RunPlan | None:
        for event in reversed(self._events):
            if event.get("type") == "run_plan" and event.get("run_id") == run_id:
                return RunPlan.from_dict(event)
        return None

    def _actuals(self, kind: str) -> dict[tuple[str, str], dict]:
        found: dict[tuple[str, str], dict] = {}
        for event in self._events:
            if event.get("type") == "actual" and event.get("kind") == kind:
                found[(str(event.get("run_id", "")), str(event.get("station_id", "")))] = event
        return found

    def _actual_count(self, run_id: str) -> int:
        return sum(
            1
            for event in self._events
            if event.get("type") == "actual" and event.get("run_id") == run_id
        )


def validate_plan_identity(run_id: str, service_date: str) -> None:
    if not run_id:
        raise RailError("车次标识为空", component="punctuality")
    try:
        time.strptime(service_date, "%Y-%m-%d")
    except (TypeError, ValueError) as error:
        raise RailError("班次日期必须使用 YYYY-MM-DD", component="punctuality") from error


def service_date_of(at: float) -> str:
    """把 Unix 秒换算成 UTC 班次日期。"""

    return time.strftime("%Y-%m-%d", time.gmtime(at))


def summarize_calls(calls: Sequence[CallDeviation]) -> dict:
    """给排班、站台页和审计共用的偏差汇总。"""

    arrival_delays = [call.arrival_delay_s for call in calls if call.arrival_delay_s is not None]
    departure_delays = [
        call.departure_delay_s for call in calls if call.departure_delay_s is not None
    ]
    return {
        "calls": len(calls),
        "planned": sum(1 for call in calls if call.status == "planned"),
        "arrived": sum(1 for call in calls if call.status == "arrived"),
        "departed": sum(1 for call in calls if call.status == "departed"),
        "arrival_breaches": sum(1 for call in calls if call.arrival_breach),
        "forecast_arrival_breaches": sum(1 for call in calls if call.forecast_arrival_breach),
        "departure_breaches": sum(1 for call in calls if call.departure_breach),
        "headway_breaches": sum(1 for call in calls if call.headway_breach),
        "release_blocked": sum(1 for call in calls if call.release_blocked),
        "max_arrival_delay_s": max(arrival_delays, default=0.0),
        "max_departure_delay_s": max(departure_delays, default=0.0),
        "max_effective_arrival_delay_s": max(
            (call.effective_arrival_delay_s for call in calls), default=0.0
        ),
        "max_effective_departure_delay_s": max(
            (call.effective_departure_delay_s for call in calls), default=0.0
        ),
    }


def _validate_tolerance(value: float | None, label: str) -> None:
    if value is not None and value < 0:
        raise RailError(f"{label}不能为负", component="punctuality")

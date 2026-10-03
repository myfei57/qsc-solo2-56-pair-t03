"""到发偏差领域模型。

排班和实绩都以「班次（service_date）＋车次（run_id）＋站台」为不可替代的业务键。
计划可以在第一个实绩出现前修订；一旦车次开班，计划被冻结，后续只能追加新的
实绩事件。投影里的每个站台偏差同时保留计划基线偏差和按追踪间隔传导后的有效
偏差，避免用一个数混淆考核口径与放行口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import RailError


@dataclass(frozen=True)
class PlannedCall:
    """一个车次在一个站台的计划到发。"""

    station_id: str
    arrival_at: float
    departure_at: float

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "arrival_at": self.arrival_at,
            "departure_at": self.departure_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "PlannedCall":
        return cls(
            station_id=str(payload.get("station_id", "")),
            arrival_at=float(payload.get("arrival_at", 0.0)),
            departure_at=float(payload.get("departure_at", 0.0)),
        )


@dataclass(frozen=True)
class RunPlan:
    """一个班次内一个车次的完整停站序列。"""

    run_id: str
    service_date: str
    calls: tuple[PlannedCall, ...]
    revision: int = 1
    allow_arrival_delay_s: float | None = None
    allow_departure_delay_s: float | None = None
    sealed: bool = False

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "service_date": self.service_date,
            "revision": self.revision,
            "allow_arrival_delay_s": self.allow_arrival_delay_s,
            "allow_departure_delay_s": self.allow_departure_delay_s,
            "sealed": self.sealed,
            "calls": [call.as_dict() for call in self.calls],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RunPlan":
        raw_calls = payload.get("calls", [])
        return cls(
            run_id=str(payload.get("run_id", "")),
            service_date=str(payload.get("service_date", "")),
            revision=int(payload.get("revision", 1)),
            allow_arrival_delay_s=_optional_float(payload.get("allow_arrival_delay_s")),
            allow_departure_delay_s=_optional_float(payload.get("allow_departure_delay_s")),
            sealed=bool(payload.get("sealed", False)),
            calls=tuple(PlannedCall.from_dict(item) for item in raw_calls if isinstance(item, dict)),
        )


@dataclass
class CallDeviation:
    """一个站台到发偏差与放行判定。"""

    run_id: str
    service_date: str
    station_id: str
    sequence: int
    plan_arrival_at: float
    plan_departure_at: float
    planned_dwell_s: float
    actual_arrival_at: float | None = None
    actual_departure_at: float | None = None
    arrival_deviation_s: float | None = None
    departure_deviation_s: float | None = None
    arrival_delay_s: float | None = None
    departure_delay_s: float | None = None
    propagated_arrival_delay_s: float = 0.0
    propagated_departure_delay_s: float = 0.0
    effective_arrival_delay_s: float = 0.0
    effective_departure_delay_s: float = 0.0
    earliest_arrival_at: float = 0.0
    earliest_departure_at: float = 0.0
    arrival_headway_s: float = 0.0
    headway_breach: bool = False
    dwell_breach: bool = False
    forecast_arrival_breach: bool = False
    arrival_breach: bool = False
    departure_breach: bool = False
    release_blocked: bool = False
    status: str = "planned"
    violations: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "service_date": self.service_date,
            "station_id": self.station_id,
            "sequence": self.sequence,
            "plan_arrival_at": self.plan_arrival_at,
            "plan_departure_at": self.plan_departure_at,
            "planned_dwell_s": self.planned_dwell_s,
            "actual_arrival_at": self.actual_arrival_at,
            "actual_departure_at": self.actual_departure_at,
            "arrival_deviation_s": self.arrival_deviation_s,
            "departure_deviation_s": self.departure_deviation_s,
            "arrival_delay_s": self.arrival_delay_s,
            "departure_delay_s": self.departure_delay_s,
            "propagated_arrival_delay_s": self.propagated_arrival_delay_s,
            "propagated_departure_delay_s": self.propagated_departure_delay_s,
            "effective_arrival_delay_s": self.effective_arrival_delay_s,
            "effective_departure_delay_s": self.effective_departure_delay_s,
            "earliest_arrival_at": self.earliest_arrival_at,
            "earliest_departure_at": self.earliest_departure_at,
            "arrival_headway_s": self.arrival_headway_s,
            "headway_breach": self.headway_breach,
            "dwell_breach": self.dwell_breach,
            "forecast_arrival_breach": self.forecast_arrival_breach,
            "arrival_breach": self.arrival_breach,
            "departure_breach": self.departure_breach,
            "release_blocked": self.release_blocked,
            "status": self.status,
            "violations": list(self.violations),
        }


def validate_plan_calls(calls: tuple[PlannedCall, ...]) -> None:
    """写入前校验计划停站序列。"""

    if not calls:
        raise RailError("班次至少要有一个计划停站", component="punctuality")
    seen: set[str] = set()
    previous: PlannedCall | None = None
    for index, call in enumerate(calls):
        if not call.station_id:
            raise RailError("计划停站缺少站台标识", component="punctuality")
        if call.station_id in seen:
            raise RailError(f"车次在同一班次重复经过站台 {call.station_id}", component="punctuality")
        seen.add(call.station_id)
        if call.arrival_at < 0 or call.departure_at < 0:
            raise RailError("计划到发时刻不能为负", component="punctuality")
        if call.departure_at < call.arrival_at:
            raise RailError(f"站台 {call.station_id} 的计划发车早于计划到站", component="punctuality")
        if previous is not None and call.arrival_at < previous.departure_at:
            raise RailError(
                f"站台 {call.station_id} 的计划到站早于上一站 {previous.station_id} 的计划发车",
                component="punctuality",
            )
        previous = call


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)

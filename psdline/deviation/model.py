"""到发偏差领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlannedStop:
    """一班车在一个站台的计划到发时刻（秒级 Unix 时间戳）。"""

    train_id: str
    station_id: str
    version: str
    planned_arrival: float
    planned_departure: float
    headway_s: float
    sequence: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return self.train_id, self.station_id

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "station_id": self.station_id,
            "version": self.version,
            "planned_arrival": self.planned_arrival,
            "planned_departure": self.planned_departure,
            "headway_s": self.headway_s,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "PlannedStop":
        return cls(
            train_id=str(payload.get("train_id", "")),
            station_id=str(payload.get("station_id", "")),
            version=str(payload.get("version", "")),
            planned_arrival=float(payload.get("planned_arrival", 0.0)),
            planned_departure=float(payload.get("planned_departure", 0.0)),
            headway_s=float(payload.get("headway_s", 0.0)),
            sequence=int(payload.get("sequence", 0)),
        )


@dataclass(frozen=True)
class ActualStop:
    """一班车在一个站台登记的实际到发；任一端点可在事件到达前留空。"""

    train_id: str
    station_id: str
    version: str = ""
    actual_arrival: float | None = None
    actual_departure: float | None = None
    arrival_sequence: int = 0
    departure_sequence: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return self.train_id, self.station_id

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "station_id": self.station_id,
            "version": self.version,
            "actual_arrival": self.actual_arrival,
            "actual_departure": self.actual_departure,
            "arrival_sequence": self.arrival_sequence,
            "departure_sequence": self.departure_sequence,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ActualStop":
        def timestamp(name: str) -> float | None:
            value = payload.get(name)
            return None if value is None else float(value)

        return cls(
            train_id=str(payload.get("train_id", "")),
            station_id=str(payload.get("station_id", "")),
            version=str(payload.get("version", "")),
            actual_arrival=timestamp("actual_arrival"),
            actual_departure=timestamp("actual_departure"),
            arrival_sequence=int(payload.get("arrival_sequence", 0)),
            departure_sequence=int(payload.get("departure_sequence", 0)),
        )


@dataclass
class StopDeviation:
    """单班车、单站台的偏差与追踪间隔计算结果。"""

    train_id: str
    station_id: str
    version: str
    planned_arrival: float
    planned_departure: float
    projected_arrival: float
    projected_departure: float
    actual_arrival: float | None = None
    actual_departure: float | None = None
    arrival_deviation_s: float = 0.0
    departure_deviation_s: float = 0.0
    primary_delay_s: float = 0.0
    upstream_delay_s: float = 0.0
    headway_delay_s: float = 0.0
    inherited_delay_s: float = 0.0
    recovered_delay_s: float = 0.0
    headway_s: float = 0.0
    observed_headway_s: float | None = None
    predecessor_train_id: str = ""
    arrival_headway_breach: bool = False
    arrival_allowed_deviation_s: float = 0.0
    departure_allowed_deviation_s: float = 0.0
    delay_acknowledged: bool = False
    scheduled: bool = True

    @property
    def effective_arrival(self) -> float:
        return self.actual_arrival if self.actual_arrival is not None else self.projected_arrival

    @property
    def effective_departure(self) -> float:
        return self.actual_departure if self.actual_departure is not None else self.projected_departure

    @property
    def arrival_late(self) -> bool:
        return self.arrival_deviation_s > self.arrival_allowed_deviation_s

    @property
    def departure_late(self) -> bool:
        return self.departure_deviation_s > self.departure_allowed_deviation_s

    @property
    def release_hold(self) -> bool:
        if not self.scheduled:
            return False
        return self.departure_late and not self.delay_acknowledged

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "station_id": self.station_id,
            "version": self.version,
            "scheduled": self.scheduled,
            "planned_arrival": self.planned_arrival,
            "planned_departure": self.planned_departure,
            "projected_arrival": self.projected_arrival,
            "projected_departure": self.projected_departure,
            "effective_arrival": self.effective_arrival,
            "effective_departure": self.effective_departure,
            "actual_arrival": self.actual_arrival,
            "actual_departure": self.actual_departure,
            "arrival_deviation_s": self.arrival_deviation_s,
            "departure_deviation_s": self.departure_deviation_s,
            "primary_delay_s": self.primary_delay_s,
            "upstream_delay_s": self.upstream_delay_s,
            "headway_delay_s": self.headway_delay_s,
            "inherited_delay_s": self.inherited_delay_s,
            "recovered_delay_s": self.recovered_delay_s,
            "headway_s": self.headway_s,
            "observed_headway_s": self.observed_headway_s,
            "predecessor_train_id": self.predecessor_train_id,
            "arrival_headway_breach": self.arrival_headway_breach,
            "arrival_allowed_deviation_s": self.arrival_allowed_deviation_s,
            "departure_allowed_deviation_s": self.departure_allowed_deviation_s,
            "arrival_late": self.arrival_late,
            "departure_late": self.departure_late,
            "delay_acknowledged": self.delay_acknowledged,
            "release_hold": self.release_hold,
        }


@dataclass
class DeviationView:
    """给排班、发车放行和审计共用的一次全量重算结果。"""

    sequence: int
    generated_at: float
    allowed_arrival_s: float
    allowed_departure_s: float
    active_version: str
    versions: list[str] = field(default_factory=list)
    stops: list[StopDeviation] = field(default_factory=list)
    baseline: str = "planned"
    propagation: str = "full_recompute"

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "generated_at": self.generated_at,
            "allowed_arrival_s": self.allowed_arrival_s,
            "allowed_departure_s": self.allowed_departure_s,
            "active_version": self.active_version,
            "versions": list(self.versions),
            "baseline": self.baseline,
            "propagation": self.propagation,
            "stops": [stop.as_dict() for stop in self.stops],
        }

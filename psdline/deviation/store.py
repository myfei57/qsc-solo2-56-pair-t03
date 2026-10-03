"""到发偏差事件流。

事件只追加，不提供修改或删除接口。派生视图随时可以由事件流全量重算；跨班时刻
表用新版本表示，旧版本继续保留，保证历史班次回放时不被新排班改写。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from ..errors import RailError
from ..store import JsonlJournal
from .model import ActualStop, PlannedStop

PLANNED = "planned"
ACTUAL_ARRIVAL = "actual_arrival"
ACTUAL_DEPARTURE = "actual_departure"
DELAY_ACKNOWLEDGED = "delay_acknowledged"


@dataclass(frozen=True)
class DeviationEvent:
    """一条不可变到发事件。"""

    sequence: int
    type: str
    registered_at: float
    payload: dict[str, Any]

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "type": self.type,
            "registered_at": self.registered_at,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, record: dict) -> "DeviationEvent":
        payload = record.get("payload", {})
        return cls(
            sequence=int(record.get("sequence", 0)),
            type=str(record.get("type", "")),
            registered_at=float(record.get("registered_at", 0.0)),
            payload=dict(payload) if isinstance(payload, dict) else {},
        )


class DeviationEventStore:
    """线程安全的只追加事件存储。"""

    def __init__(self, journal: JsonlJournal) -> None:
        self._journal = journal
        self._lock = threading.RLock()
        self._events = [
            DeviationEvent.from_dict(item)
            for item in journal.read_all()
            if isinstance(item, dict)
        ]

    @property
    def sequence(self) -> int:
        with self._lock:
            return len(self._events)

    def append(self, event_type: str, payload: dict[str, Any], registered_at: float) -> DeviationEvent:
        """追加事件；同序列落盘与内存索引在同一把锁内完成。"""

        if not event_type:
            raise RailError("偏差事件类型为空", component="deviation")
        with self._lock:
            event = DeviationEvent(
                sequence=len(self._events),
                type=event_type,
                registered_at=registered_at,
                payload=dict(payload),
            )
            self._journal.append(event.as_dict())
            self._events.append(event)
            return event

    def list(self) -> list[DeviationEvent]:
        with self._lock:
            return list(self._events)

    def replay(self, limit: int | None = None) -> list[DeviationEvent]:
        with self._lock:
            records = self._events if limit is None else self._events[:limit]
            return list(records)


def planned_from_event(event: DeviationEvent) -> PlannedStop:
    return PlannedStop.from_dict(event.payload)


def actual_from_events(events: list[DeviationEvent]) -> list[ActualStop]:
    merged: dict[tuple[str, str, str], ActualStop] = {}
    for event in events:
        if event.type not in (ACTUAL_ARRIVAL, ACTUAL_DEPARTURE):
            continue
        version = str(event.payload.get("version", ""))
        key = (str(event.payload.get("train_id", "")), str(event.payload.get("station_id", "")), version)
        current = merged.get(key) or ActualStop(train_id=key[0], station_id=key[1], version=version)
        data = current.as_dict()
        data["version"] = version
        if event.type == ACTUAL_ARRIVAL:
            data["actual_arrival"] = float(event.payload["actual_arrival"])
            data["arrival_sequence"] = event.sequence
        else:
            data["actual_departure"] = float(event.payload["actual_departure"])
            data["departure_sequence"] = event.sequence
        merged[key] = ActualStop.from_dict(data)
    return list(merged.values())

"""到发偏差的确定性投影。

事件只追加，投影每次从事件前缀全量重算。计算采用固定口径：

* 每个站台的「计划偏差」只拿实际时刻减计划时刻，用于排班、审计和跨班考核；
* 「有效偏差」再考虑同一站台前车实际发车加追踪间隔、以及上游站有效发车晚点；
* 早到不会被记成负晚点向下游传导，但如果挤掉追踪间隔或停站时间，会形成硬性违章。

全量重算保证历史补录、事件重放和服务重启得到同一结果；不保存任何增量中间态。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CallDeviation, PlannedCall, RunPlan, validate_plan_calls


@dataclass(frozen=True)
class PunctualityProjection:
    """一个不可变事件前缀的全部偏差结果。"""

    calls: tuple[CallDeviation, ...]
    event_count: int

    def as_dict(self) -> dict:
        return {
            "event_count": self.event_count,
            "calls": [call.as_dict() for call in self.calls],
        }

    def for_run(self, run_id: str) -> tuple[CallDeviation, ...]:
        return tuple(call for call in self.calls if call.run_id == run_id)

    def for_station(self, station_id: str) -> tuple[CallDeviation, ...]:
        return tuple(call for call in self.calls if call.station_id == station_id)

    def find(self, run_id: str, station_id: str) -> CallDeviation | None:
        for call in self.calls:
            if call.run_id == run_id and call.station_id == station_id:
                return call
        return None


def project_punctuality(
    events: list[dict],
    headways: dict[str, float],
    *,
    default_arrival_tolerance_s: float,
    default_departure_tolerance_s: float,
    default_headway_s: float,
    as_of_seq: int | None = None,
) -> PunctualityProjection:
    """从只追加事件重算所有班次的站台偏差。"""

    cutoff = len(events) if as_of_seq is None else max(0, min(as_of_seq, len(events)))
    plans: dict[str, RunPlan] = {}
    arrivals: dict[tuple[str, str], dict] = {}
    departures: dict[tuple[str, str], dict] = {}

    for seq, event in enumerate(events[:cutoff], start=1):
        event_type = str(event.get("type", ""))
        if event_type == "run_plan":
            plan = RunPlan.from_dict(event)
            validate_plan_calls(plan.calls)
            plans[plan.run_id] = plan
        elif event_type == "actual":
            run_id = str(event.get("run_id", ""))
            station_id = str(event.get("station_id", ""))
            kind = str(event.get("kind", ""))
            if run_id not in plans:
                continue
            plan_stations = [call.station_id for call in plans[run_id].calls]
            if station_id not in plan_stations:
                continue
            payload = dict(event)
            payload["seq"] = seq
            key = (run_id, station_id)
            if kind == "arrival":
                arrivals[key] = payload
            elif kind == "departure":
                departures[key] = payload

    nodes = [
        _make_node(plan, sequence, call)
        for plan in plans.values()
        for sequence, call in enumerate(plan.calls, start=1)
    ]
    nodes.sort(key=lambda node: (node.service_date, node.plan_arrival_at, node.run_id, node.sequence))

    previous_by_station: dict[tuple[str, str], CallDeviation] = {}
    previous_by_run: dict[str, CallDeviation] = {}
    for node in nodes:
        station_key = (node.service_date, node.station_id)
        station_previous = previous_by_station.get(station_key)
        route_previous = previous_by_run.get(node.run_id)
        headway = float(headways.get(node.station_id, default_headway_s))
        arrival = arrivals.get((node.run_id, node.station_id))
        departure = departures.get((node.run_id, node.station_id))
        node.arrival_headway_s = headway

        route_earliest = _route_arrival_earliest(route_previous, node)
        station_earliest = _station_arrival_earliest(station_previous, node, headway)
        feasible_arrival = max(node.plan_arrival_at, route_earliest, station_earliest)
        node.earliest_arrival_at = feasible_arrival
        node.propagated_arrival_delay_s = max(0.0, feasible_arrival - node.plan_arrival_at)

        violations: list[str] = []
        if arrival is not None:
            observed = float(arrival["at"])
            node.actual_arrival_at = observed
            node.arrival_deviation_s = round(observed - node.plan_arrival_at, 3)
            node.arrival_delay_s = max(0.0, node.arrival_deviation_s)
            if observed < feasible_arrival:
                node.headway_breach = True
                violations.append(
                    f"实际到站 {observed} 早于追踪间隔约束的最早到站 {feasible_arrival}"
                )
            node.effective_arrival_delay_s = max(0.0, max(observed, feasible_arrival) - node.plan_arrival_at)
            node.status = "arrived"
        else:
            node.effective_arrival_delay_s = node.propagated_arrival_delay_s

        departure_separation_earliest = _departure_separation_earliest(station_previous, headway)
        dwell_from = node.actual_arrival_at if node.actual_arrival_at is not None else feasible_arrival
        dwell_earliest = dwell_from + node.planned_dwell_s
        feasible_departure = max(
            node.plan_departure_at,
            dwell_earliest,
            departure_separation_earliest,
        )
        node.earliest_departure_at = feasible_departure
        node.propagated_departure_delay_s = max(
            0.0, feasible_departure - node.plan_departure_at
        )

        if departure is not None:
            observed_departure = float(departure["at"])
            node.actual_departure_at = observed_departure
            node.departure_deviation_s = round(
                observed_departure - node.plan_departure_at, 3
            )
            node.departure_delay_s = max(0.0, node.departure_deviation_s)
            if arrival is not None and observed_departure < float(arrival["at"]) + node.planned_dwell_s:
                node.dwell_breach = True
                violations.append("实际发车未满足计划停站时长")
            if observed_departure < departure_separation_earliest:
                node.headway_breach = True
                violations.append("实际发车未满足同站台追踪间隔")
            effective_departure = max(observed_departure, feasible_departure)
            node.effective_departure_delay_s = max(
                0.0, effective_departure - node.plan_departure_at
            )
            node.status = "departed"
        else:
            node.effective_departure_delay_s = max(
                node.effective_arrival_delay_s, node.propagated_departure_delay_s
            )

        arrival_limit = _arrival_tolerance(plans[node.run_id], default_arrival_tolerance_s)
        departure_limit = _departure_tolerance(plans[node.run_id], default_departure_tolerance_s)
        if node.actual_arrival_at is not None and node.arrival_delay_s > arrival_limit:
            node.arrival_breach = True
            violations.append(f"到站晚点 {node.arrival_delay_s:.0f}s 超过允许 {arrival_limit:.0f}s")
        elif node.actual_arrival_at is None and node.effective_arrival_delay_s > arrival_limit:
            node.forecast_arrival_breach = True
            violations.append(
                f"预测到站晚点 {node.effective_arrival_delay_s:.0f}s 超过允许 {arrival_limit:.0f}s"
            )
        if node.actual_departure_at is not None and node.departure_delay_s > departure_limit:
            node.departure_breach = True
            violations.append(f"发车晚点 {node.departure_delay_s:.0f}s 超过允许 {departure_limit:.0f}s")

        if node.actual_departure_at is None:
            node.release_blocked = bool(
                node.arrival_breach
                or node.forecast_arrival_breach
                or node.effective_departure_delay_s > departure_limit
            )
        node.violations = tuple(violations)
        previous_by_station[station_key] = node
        previous_by_run[node.run_id] = node

    return PunctualityProjection(calls=tuple(nodes), event_count=cutoff)


def _make_node(plan: RunPlan, sequence: int, call: PlannedCall) -> CallDeviation:
    return CallDeviation(
        run_id=plan.run_id,
        service_date=plan.service_date,
        station_id=call.station_id,
        sequence=sequence,
        plan_arrival_at=call.arrival_at,
        plan_departure_at=call.departure_at,
        planned_dwell_s=max(0.0, call.departure_at - call.arrival_at),
        earliest_arrival_at=call.arrival_at,
        earliest_departure_at=call.departure_at,
    )


def _route_arrival_earliest(previous: CallDeviation | None, node: CallDeviation) -> float:
    if previous is None:
        return node.plan_arrival_at
    previous_departure = (
        previous.actual_departure_at
        if previous.actual_departure_at is not None
        else previous.earliest_departure_at
    )
    planned_travel = node.plan_arrival_at - previous.plan_departure_at
    return previous_departure + planned_travel


def _station_arrival_earliest(
    previous: CallDeviation | None, node: CallDeviation, headway: float
) -> float:
    if previous is None:
        return node.plan_arrival_at
    previous_departure = (
        previous.actual_departure_at
        if previous.actual_departure_at is not None
        else previous.earliest_departure_at
    )
    return previous_departure + headway


def _departure_separation_earliest(previous: CallDeviation | None, headway: float) -> float:
    if previous is None:
        return 0.0
    previous_departure = (
        previous.actual_departure_at
        if previous.actual_departure_at is not None
        else previous.earliest_departure_at
    )
    return previous_departure + headway


def _arrival_tolerance(plan: RunPlan, default: float) -> float:
    return default if plan.allow_arrival_delay_s is None else plan.allow_arrival_delay_s


def _departure_tolerance(plan: RunPlan, default: float) -> float:
    return default if plan.allow_departure_delay_s is None else plan.allow_departure_delay_s

"""从不可变到发事件全量重算偏差。

设计取舍写在这里，避免调用方各自猜一套：

* 偏差基线使用**计划时刻**。前车实际到站只用于计算追踪间隔约束，不改变
  ``arrival_deviation_s`` / ``departure_deviation_s``，这样跨班换表后历史偏差仍
  稳定，调度、放行、审计读到的是同一条事实。
* 每次新增事件都从事件流全量重算，不做增量叠加。晚点可能被运行恢复、换班表和
  追踪间隔共同吸收，叠加增量会把同一分钟重复传给下游。
* 站内按计划班次顺序追踪；同一列车再沿线路顺序把上一站实际/预计发车传到下一站。
"""

from __future__ import annotations

from collections import defaultdict

from .model import ActualStop, DeviationView, PlannedStop, StopDeviation


def _version_rank(version: str, first_sequence: dict[str, int]) -> tuple[int, str]:
    return first_sequence.get(version, 0), version


def _select_plans(plans: list[PlannedStop]) -> tuple[dict[tuple[str, str], PlannedStop], str, list[str]]:
    grouped: dict[str, list[PlannedStop]] = defaultdict(list)
    first_sequence: dict[str, int] = {}
    for plan in plans:
        if plan.version not in first_sequence:
            first_sequence[plan.version] = plan.sequence
        grouped[plan.version].append(plan)
        first_sequence[plan.version] = min(first_sequence[plan.version], plan.sequence)
    versions = sorted(grouped, key=lambda version: _version_rank(version, first_sequence))
    active_version = versions[-1] if versions else ""
    selected: dict[tuple[str, str], PlannedStop] = {}
    # 旧班次覆盖不到的新车/新站沿用最新包含该键的版本；新版未改的点不被清空。
    for version in versions:
        for plan in grouped[version]:
            selected[plan.key] = plan
    return selected, active_version, versions


def _station_index(station_id: str, station_order: list[str]) -> int:
    try:
        return station_order.index(station_id)
    except ValueError:
        return len(station_order)


def _service_key(
    row: PlannedStop | None,
    actual: ActualStop | None,
    station_order: list[str],
) -> tuple:
    station_id = row.station_id if row else (actual.station_id if actual else "")
    if row is not None:
        return (_station_index(station_id, station_order), 0, row.planned_arrival, row.train_id)
    arrival = actual.actual_arrival if actual else 0.0
    return (_station_index(station_id, station_order), 1, arrival or 0.0, station_id, actual.train_id if actual else "")


def materialize(
    plans: list[PlannedStop],
    actuals: list[ActualStop],
    *,
    station_order: list[str],
    current_time: float,
    allowed_arrival_s: float,
    allowed_departure_s: float,
    default_headway_s: float,
    acknowledged: set[tuple[str, str, str]] | None = None,
    sequence: int = 0,
) -> DeviationView:
    """把计划、实测与晚点确认重算成一份不可变视图。"""

    acknowledged = acknowledged or set()
    selected, active_version, versions = _select_plans(plans)
    actual_map = {
        (actual.train_id, actual.station_id, actual.version): actual for actual in actuals
    }

    matched_actuals: set[tuple[str, str, str]] = set()
    rows: list[tuple[str, PlannedStop | None, ActualStop | None]] = []
    for key, plan in selected.items():
        actual = actual_map.get((plan.train_id, plan.station_id, plan.version))
        if actual is not None:
            matched_actuals.add(
                (actual.train_id, actual.station_id, actual.version)
            )
        rows.append((key[1], plan, actual))
    for actual in actuals:
        actual_key = (actual.train_id, actual.station_id, actual.version)
        if actual_key in matched_actuals:
            continue
        rows.append((actual.station_id, None, actual))
    rows.sort(key=lambda item: _service_key(item[1], item[2], station_order))

    by_train: dict[str, dict[str, StopDeviation]] = defaultdict(dict)
    by_station: dict[str, list[StopDeviation]] = defaultdict(list)
    result: list[StopDeviation] = []

    for _station_id, plan, actual in rows:
        if plan is not None:
            stop = _planned_stop(
                plan,
                actual,
                by_train.get(plan.train_id, {}),
                by_station.get(plan.station_id, []),
                station_order,
                acknowledged,
                allowed_arrival_s,
                allowed_departure_s,
            )
        else:
            stop = _unscheduled_stop(actual, by_station.get(actual.station_id, []), default_headway_s)
        by_train[stop.train_id][stop.station_id] = stop
        by_station[stop.station_id].append(stop)
        result.append(stop)

    return DeviationView(
        sequence=sequence,
        generated_at=current_time,
        allowed_arrival_s=allowed_arrival_s,
        allowed_departure_s=allowed_departure_s,
        active_version=active_version,
        versions=versions,
        stops=result,
    )


def _planned_stop(
    plan: PlannedStop,
    actual: ActualStop | None,
    prior_by_train: dict[str, StopDeviation],
    prior_at_station: list[StopDeviation],
    station_order: list[str],
    acknowledged: set[tuple[str, str, str]],
    allowed_arrival_s: float,
    allowed_departure_s: float,
) -> StopDeviation:
    upstream = _upstream_constraint(plan, prior_by_train, station_order)
    predecessor = prior_at_station[-1] if prior_at_station else None
    headway_constraint = (
        predecessor.effective_arrival + plan.headway_s
        if predecessor is not None
        else plan.planned_arrival
    )
    base_arrival = max(plan.planned_arrival, upstream, headway_constraint)
    upstream_delay = max(0.0, upstream - plan.planned_arrival)
    headway_delay = max(0.0, headway_constraint - plan.planned_arrival)
    inherited_delay = max(upstream_delay, headway_delay)

    observed_headway: float | None = None
    headway_breach = False
    if predecessor is not None and actual and actual.actual_arrival is not None and predecessor.actual_arrival is not None:
        observed_headway = actual.actual_arrival - predecessor.actual_arrival
        headway_breach = observed_headway < plan.headway_s

    effective_arrival = actual.actual_arrival if actual and actual.actual_arrival is not None else base_arrival
    arrival_deviation = effective_arrival - plan.planned_arrival
    primary_delay = max(0.0, effective_arrival - base_arrival)
    recovered_delay = max(0.0, inherited_delay - max(0.0, arrival_deviation - primary_delay))

    dwell = max(0.0, plan.planned_departure - plan.planned_arrival)
    effective_departure = (
        actual.actual_departure
        if actual and actual.actual_departure is not None
        else max(plan.planned_departure, effective_arrival + dwell)
    )
    departure_deviation = effective_departure - plan.planned_departure

    return StopDeviation(
        train_id=plan.train_id,
        station_id=plan.station_id,
        version=plan.version,
        planned_arrival=plan.planned_arrival,
        planned_departure=plan.planned_departure,
        projected_arrival=effective_arrival,
        projected_departure=max(
            plan.planned_departure,
            effective_arrival + dwell,
        ),
        actual_arrival=actual.actual_arrival if actual else None,
        actual_departure=actual.actual_departure if actual else None,
        arrival_deviation_s=arrival_deviation,
        departure_deviation_s=departure_deviation,
        primary_delay_s=primary_delay,
        upstream_delay_s=upstream_delay,
        headway_delay_s=headway_delay,
        inherited_delay_s=inherited_delay,
        recovered_delay_s=recovered_delay,
        headway_s=plan.headway_s,
        observed_headway_s=observed_headway,
        predecessor_train_id=predecessor.train_id if predecessor else "",
        arrival_headway_breach=headway_breach,
        arrival_allowed_deviation_s=allowed_arrival_s,
        departure_allowed_deviation_s=allowed_departure_s,
        delay_acknowledged=(plan.train_id, plan.station_id, plan.version) in acknowledged,
        scheduled=True,
    )


def _upstream_constraint(
    plan: PlannedStop,
    prior_by_train: dict[str, StopDeviation],
    station_order: list[str],
) -> float:
    current_index = _station_index(plan.station_id, station_order)
    candidates = [
        stop
        for stop in prior_by_train.values()
        if _station_index(stop.station_id, station_order) < current_index
    ]
    if not candidates:
        return plan.planned_arrival
    previous = max(candidates, key=lambda stop: _station_index(stop.station_id, station_order))
    previous_departure = previous.actual_departure
    if previous_departure is None:
        previous_departure = previous.projected_departure
    planned_run = plan.planned_arrival - previous.planned_departure
    return previous_departure + planned_run


def _unscheduled_stop(
    actual: ActualStop,
    prior_at_station: list[StopDeviation],
    default_headway_s: float,
) -> StopDeviation:
    predecessor = prior_at_station[-1] if prior_at_station else None
    arrival = actual.actual_arrival or 0.0
    departure = actual.actual_departure if actual.actual_departure is not None else arrival
    observed_headway = None
    breach = False
    if predecessor is not None and predecessor.actual_arrival is not None and actual.actual_arrival is not None:
        observed_headway = actual.actual_arrival - predecessor.actual_arrival
        breach = observed_headway < default_headway_s
    return StopDeviation(
        train_id=actual.train_id,
        station_id=actual.station_id,
        version="",
        planned_arrival=0.0,
        planned_departure=0.0,
        projected_arrival=arrival,
        projected_departure=departure,
        actual_arrival=actual.actual_arrival,
        actual_departure=actual.actual_departure,
        headway_s=default_headway_s,
        observed_headway_s=observed_headway,
        predecessor_train_id=predecessor.train_id if predecessor else "",
        arrival_headway_breach=breach,
        arrival_allowed_deviation_s=0.0,
        departure_allowed_deviation_s=0.0,
        scheduled=False,
    )

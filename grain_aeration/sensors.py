"""测温电缆测点评估：漂移隔离与风险区间扩大。

规则（对应投诉复盘要求）：
1. ``drifting`` / ``invalid`` 的测点被**隔离**：不参与任何结露/降温计算；
2. 某点隔离时，沿同一根测温电缆把相邻点（上下各一层，及同层最近点）
   纳入**扩大风险区间**——该区间按更保守的参数处理（结露裕量加大）；
3. 被扩大覆盖但本身 ``good`` 的点仍可用，但打上保守标记；
4. 隔离必须由有权限人员（admin）登记原因，进入审计时间线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (
    GrainLayer,
    ISOLATED_QUALITIES,
    Sensor,
    SensorQuality,
    SensorReading,
    aware,
)

# 漂移点上下扩大的层数
EXPAND_LAYERS = 1
# 扩大风险区间内追加的结露裕量（°C）
RISK_ZONE_EXTRA_MARGIN_C = 1.0


@dataclass
class SensorAssessment:
    at: datetime
    trusted: list[SensorReading]                       # 参与决策的读数
    isolated: dict[str, SensorReading]                 # 被隔离读数（含原因）
    expanded_zone: set[str]                            # 扩大风险区间测点 id
    isolation_reasons: dict[str, str] = field(default_factory=dict)
    manually_isolated: set[str] = field(default_factory=set)

    @property
    def coldest(self) -> SensorReading | None:
        return min(self.trusted, key=lambda r: r.temperature_c, default=None)

    @property
    def trust_ratio(self) -> float:
        total = len(self.trusted) + len(self.isolated)
        return len(self.trusted) / total if total else 0.0

    def is_expanded(self, sensor_id: str) -> bool:
        return sensor_id in self.expanded_zone

    def extra_margin_c(self, sensor_id: str) -> float:
        return RISK_ZONE_EXTRA_MARGIN_C if sensor_id in self.expanded_zone else 0.0


def _cable_neighbors(sensors: list[Sensor], target: Sensor) -> set[str]:
    """同电缆上下各 EXPAND_LAYERS 个点序；不同电缆则取同粮层点（保守跨缆标记）。"""
    zone: set[str] = set()
    same_cable = sorted(
        (s for s in sensors if s.cable_id == target.cable_id),
        key=lambda s: s.cable_index,
    )
    for s in same_cable:
        if s.id == target.id:
            continue
        if abs(s.cable_index - target.cable_index) <= EXPAND_LAYERS:
            zone.add(s.id)
    # 同层最近深度点（其它电缆），覆盖"这一层可能整体偏冷"的担忧
    same_layer = [
        s
        for s in sensors
        if s.layer is target.layer and s.cable_id != target.cable_id
    ]
    if same_layer:
        nearest = min(same_layer, key=lambda s: abs(s.depth_m - target.depth_m))
        zone.add(nearest.id)
    return zone


def assess_sensors(
    sensors: list[Sensor],
    readings: dict[str, SensorReading],
    at: datetime,
    manual_isolation: dict[str, str] | None = None,
) -> SensorAssessment:
    """汇总当前时刻测点状态。

    ``manual_isolation``: 由 admin 登记的 {sensor_id: 原因}，与质量标志取并集。
    """
    aware(at, "评估时间")
    manual_isolation = manual_isolation or {}
    trusted: list[SensorReading] = []
    isolated: dict[str, SensorReading] = {}
    reasons: dict[str, str] = {}
    expanded: set[str] = set()
    manual_set = set(manual_isolation)

    by_id = {s.id: s for s in sensors}
    for sensor in sensors:
        reading = readings.get(sensor.id)
        if reading is None:
            continue
        if sensor.id in manual_isolation:
            isolated[sensor.id] = reading
            reasons[sensor.id] = manual_isolation[sensor.id]
            expanded |= _cable_neighbors(sensors, sensor)
        elif reading.quality in ISOLATED_QUALITIES:
            isolated[sensor.id] = reading
            reasons[sensor.id] = {
                SensorQuality.DRIFTING: "传感器漂移，读数不可信",
                SensorQuality.INVALID: "传感器失效",
            }[reading.quality]
            expanded |= _cable_neighbors(sensors, sensor)
        else:
            trusted.append(reading)

    expanded -= set(isolated)  # 区间只包含仍可读的点
    return SensorAssessment(
        at=at,
        trusted=trusted,
        isolated=isolated,
        expanded_zone=expanded,
        isolation_reasons=reasons,
        manually_isolated=manual_set,
    )


def cold_layer_view(
    sensors: list[Sensor], assessment: SensorAssessment
) -> dict[GrainLayer, dict[str, float]]:
    """按粮层聚合可信测点，供建议解释温度分布。"""
    by_layer: dict[GrainLayer, list[float]] = {
        GrainLayer.TOP: [],
        GrainLayer.MIDDLE: [],
        GrainLayer.BOTTOM: [],
    }
    for r in assessment.trusted:
        sensor = next(s for s in sensors if s.id == r.sensor_id)
        by_layer[sensor.layer].append(r.temperature_c)
    view: dict[GrainLayer, dict[str, float]] = {}
    for layer, temps in by_layer.items():
        if temps:
            view[layer] = {
                "min_c": min(temps),
                "max_c": max(temps),
                "mean_c": round(sum(temps) / len(temps), 2),
            }
    return view

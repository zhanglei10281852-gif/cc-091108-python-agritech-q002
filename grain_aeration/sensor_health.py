"""漂移/失效传感器隔离，以及风险深度区间的扩大。

原则：

1. quality 非 good 的测点立即隔离，不参与任何温度/湿度聚合；
2. 隔离点周围按“相邻电缆间距 × 扩展系数”向两侧扩大风险区间，
   某一侧没有相邻电缆时一直外扩到粮堆边界（未知区域按风险处理）；
3. 多个隔离点的扩大区间取并集；
4. 结露判断使用可信测点中的最低粮温（冷端包络），最保守。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import SensorQuality, SensorReading


@dataclass(frozen=True)
class SensorHealthAssessment:
    trustworthy: tuple[SensorReading, ...]
    isolated: tuple[SensorReading, ...]
    # 扩大后的风险区间（米，深度坐标，0=粮面）
    risk_bands: tuple[tuple[float, float], ...]
    pile_depth_m: float

    @property
    def isolated_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.isolated)

    @property
    def overall_risk_band(self) -> tuple[float, float]:
        """所有风险区间的外包络。"""
        if not self.risk_bands:
            return (0.0, 0.0)
        return (min(b[0] for b in self.risk_bands), max(b[1] for b in self.risk_bands))

    def conservative_cold_end_c(self) -> float | None:
        """可信测点中的最低粮温——粮堆里最先逼近结露的位置。"""
        temps = [s.temperature_c for s in self.trustworthy if s.temperature_c is not None]
        return min(temps) if temps else None

    def warm_end_c(self) -> float | None:
        temps = [s.temperature_c for s in self.trustworthy if s.temperature_c is not None]
        return max(temps) if temps else None


def assess_sensor_health(
    readings: list[SensorReading] | tuple[SensorReading, ...],
    pile_depth_m: float,
    expansion_factor: float = 1.0,
) -> SensorHealthAssessment:
    good = tuple(r for r in readings if r.quality is SensorQuality.GOOD)
    bad = tuple(r for r in readings if r.quality is not SensorQuality.GOOD)

    good_depths = sorted(r.depth_m for r in good)
    bands: list[tuple[float, float]] = []

    for r in bad:
        d = r.depth_m
        # 上方（更浅）最近的可信电缆
        above = [x for x in good_depths if x < d]
        if above:
            nearest_above = max(above)
            lo = max(0.0, d - (d - nearest_above) * expansion_factor)
        else:
            lo = 0.0  # 上方无相邻电缆：未知区域一直扩大到粮面
        # 下方（更深）最近的可信电缆
        below = [x for x in good_depths if x > d]
        if below:
            nearest_below = min(below)
            hi = min(pile_depth_m, d + (nearest_below - d) * expansion_factor)
        else:
            hi = pile_depth_m  # 下方无相邻电缆：扩大到仓底
        bands.append((lo, hi))

    merged = _merge_bands(bands)
    return SensorHealthAssessment(
        trustworthy=good,
        isolated=bad,
        risk_bands=tuple(merged),
        pile_depth_m=pile_depth_m,
    )


def _merge_bands(bands: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not bands:
        return []
    ordered = sorted(bands)
    merged: list[list[float]] = [list(ordered[0])]
    for lo, hi in ordered[1:]:
        if lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]

"""仓房资料与安全阈值加载。

将 reference/domain.json 中的静态资料绑定到运行时模型，
restrictions（熏蒸等）是不可被普通控制覆盖的安全限制。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import SensorQuality


def _parse_dt(value: str) -> datetime:
    # 资料中的时间均带显式时区
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value}")
    return dt


@dataclass(frozen=True)
class FanSpec:
    id: str
    interlock_group: str
    rated_kw: float


@dataclass(frozen=True)
class SensorSpec:
    id: str
    depth_m: float
    temperature_c: float
    humidity_pct: float
    quality: SensorQuality


@dataclass(frozen=True)
class WeatherPoint:
    at: datetime
    temperature_c: float
    humidity_pct: float
    rain: bool


@dataclass(frozen=True)
class Restriction:
    kind: str  # "fumigation" | ...
    starts_at: datetime
    ends_at: datetime

    def active_at(self, at: datetime) -> bool:
        return self.starts_at <= at < self.ends_at


@dataclass(frozen=True)
class SafetyConfig:
    """安全与决策阈值（集中管理，便于质量经理审计）。"""

    # 结露安全裕度 ℃：粮温 - 送风露点 必须大于该值
    min_condensation_margin_c: float = 2.0
    # 最小有效降温差 ℃：送风温度需比冷端粮温低这么多才值得启动
    min_cooling_delta_c: float = 3.0
    # 风道静压正常区间 Pa
    duct_static_min_pa: float = 80.0
    duct_static_max_pa: float = 900.0
    # 风机互锁：同一风道组最多允许运行的风机数（防短路/抢风）
    max_running_per_duct_group: int = 1
    # 建议有效期
    recommendation_ttl_seconds: int = 300
    # 传感器读数最大允许延迟
    sensor_max_age_seconds: int = 900
    # 漂移点风险区间向相邻电缆外扩的比例（点距的倍数）
    drift_expansion_factor: float = 1.0
    # 允许人工强制覆盖的动作（硬门禁永远不在其中）
    force_allowed_actions: frozenset[str] = field(
        default_factory=lambda: frozenset({"HOLD", "DEFER_TO_OFF_PEAK"})
    )
    # 雨雪倒灌判定：降雨且外湿超过该值
    rain_ingress_humidity_pct: float = 88.0


@dataclass(frozen=True)
class DomainConfig:
    domain: str
    warehouse_id: str
    grain: str
    depth_m: float
    sensors: tuple[SensorSpec, ...]
    fans: tuple[FanSpec, ...]
    weather: tuple[WeatherPoint, ...]
    restrictions: tuple[Restriction, ...]

    def fan(self, fan_id: str) -> FanSpec:
        for f in self.fans:
            if f.id == fan_id:
                return f
        raise KeyError(fan_id)

    def duct_groups(self) -> dict[str, list[FanSpec]]:
        groups: dict[str, list[FanSpec]] = {}
        for f in self.fans:
            groups.setdefault(f.interlock_group, []).append(f)
        return groups

    def restriction_active(self, kind: str, at: datetime) -> bool:
        return any(
            r.kind == kind and r.active_at(at) for r in self.restrictions
        )

    def any_blocking_restriction(self, at: datetime) -> list[Restriction]:
        return [r for r in self.restrictions if r.active_at(at)]

    def latest_weather_before(self, at: datetime) -> WeatherPoint | None:
        prior = [w for w in self.weather if w.at <= at]
        return max(prior, key=lambda w: w.at, default=None)


def load_domain(path: str | Path) -> DomainConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("domain") != "grain-aeration":
        raise ValueError("not a grain-aeration domain file")

    sensors = tuple(
        SensorSpec(
            id=s["id"],
            depth_m=float(s["depth_m"]),
            temperature_c=float(s["temperature_c"]),
            humidity_pct=float(s["humidity_pct"]),
            quality=SensorQuality(s.get("quality", "good")),
        )
        for s in raw["sensors"]
    )
    fans = tuple(
        FanSpec(
            id=f["id"],
            interlock_group=f["interlock_group"],
            rated_kw=float(f["rated_kw"]),
        )
        for f in raw["fans"]
    )
    weather = tuple(
        WeatherPoint(
            at=_parse_dt(w["at"]),
            temperature_c=float(w["temperature_c"]),
            humidity_pct=float(w["humidity_pct"]),
            rain=bool(w.get("rain", False)),
        )
        for w in raw.get("outside_weather", [])
    )
    restrictions = tuple(
        Restriction(
            kind=r["kind"],
            starts_at=_parse_dt(r["starts_at"]),
            ends_at=_parse_dt(r["ends_at"]),
        )
        for r in raw.get("restrictions", [])
    )
    wh = raw["warehouse"]
    return DomainConfig(
        domain=raw["domain"],
        warehouse_id=wh["id"],
        grain=wh["grain"],
        depth_m=float(wh["depth_m"]),
        sensors=sensors,
        fans=fans,
        weather=weather,
        restrictions=restrictions,
    )

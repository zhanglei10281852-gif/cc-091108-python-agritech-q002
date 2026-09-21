"""资料文件加载：仓房结构、测温电缆、风机互锁、风道压力、气象片段与电价。

兼容并扩展 reference/domain.json：
- sensors 增加 cable_id / layer / cable_index；
- fans 增加 duct_id；
- 顶层增加 ducts、duct_pressures、tariff；
- outside_weather 支持时间序列（单条点也兼容）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import (
    CN_TZ,
    DuctPressure,
    Fan,
    GrainLayer,
    Restriction,
    RestrictionKind,
    Sensor,
    SensorQuality,
    SensorReading,
    WeatherPoint,
)
from .tariff import DEFAULT_TARIFF, TariffSchedule, TariffSlot, TariffTier


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_domain(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("domain") != "grain-aeration":
        raise ValueError("资料文件 domain 必须为 grain-aeration")
    return data


def parse_sensors(data: dict) -> list[Sensor]:
    out: list[Sensor] = []
    for i, s in enumerate(data.get("sensors", [])):
        out.append(
            Sensor(
                id=s["id"],
                cable_id=s.get("cable_id", s["id"].split("-")[1] if "-" in s["id"] else "C-1"),
                depth_m=float(s["depth_m"]),
                layer=GrainLayer(s.get("layer", _layer_by_depth(float(s["depth_m"])))),
                cable_index=int(s.get("cable_index", i)),
            )
        )
    return out


def _layer_by_depth(depth_m: float) -> str:
    if depth_m < 1.5:
        return GrainLayer.TOP.value
    if depth_m < 4.0:
        return GrainLayer.MIDDLE.value
    return GrainLayer.BOTTOM.value


def latest_readings(data: dict, at: datetime | None = None) -> dict[str, SensorReading]:
    """从资料中的测点当前值构造读数（资料文件保存的是最近一次采样）。"""
    out: dict[str, SensorReading] = {}
    default_at = at or datetime(2026, 9, 11, 22, 0, tzinfo=CN_TZ)
    for s in data.get("sensors", []):
        ts = s.get("at")
        reading_at = _dt(ts) if ts else default_at
        out[s["id"]] = SensorReading(
            sensor_id=s["id"],
            at=reading_at,
            temperature_c=float(s["temperature_c"]),
            humidity_pct=s.get("humidity_pct"),
            quality=SensorQuality(s.get("quality", "good")),
        )
    return out


def parse_fans(data: dict) -> list[Fan]:
    return [
        Fan(
            id=f["id"],
            interlock_group=f["interlock_group"],
            rated_kw=float(f["rated_kw"]),
            duct_id=f.get("duct_id", ""),
        )
        for f in data.get("fans", [])
    ]


def parse_weather(data: dict) -> list[WeatherPoint]:
    points = data.get("outside_weather", [])
    if isinstance(points, dict):
        points = [points]
    return [
        WeatherPoint(
            at=_dt(p["at"]),
            temperature_c=float(p["temperature_c"]),
            humidity_pct=float(p["humidity_pct"]),
            rain=bool(p.get("rain", False)),
            wind_speed_mps=float(p.get("wind_speed_mps", 0.0)),
        )
        for p in points
    ]


def weather_at(points: list[WeatherPoint], at: datetime) -> WeatherPoint | None:
    """取不晚于 ``at`` 的最近一个气象点。"""
    past = [p for p in points if p.at <= at]
    return max(past, key=lambda p: p.at, default=None)


def parse_pressures(data: dict, at: datetime | None = None) -> dict[str, DuctPressure]:
    out: dict[str, DuctPressure] = {}
    ranges = {
        d["id"]: tuple(d.get("reference_range_pa", [-1200.0, -100.0]))
        for d in data.get("ducts", [])
    }
    for p in data.get("duct_pressures", []):
        ts = p.get("at")
        if at is not None and ts and _dt(ts) > at:
            continue
        duct_id = p["duct_id"]
        out[duct_id] = DuctPressure(
            duct_id=duct_id,
            at=_dt(ts) if ts else at or datetime(2000, 1, 1, tzinfo=CN_TZ),
            static_pressure_pa=float(p["static_pressure_pa"]),
            reference_range_pa=ranges.get(duct_id, (-1200.0, -100.0)),
        )
    return out


def parse_restrictions(data: dict) -> list[Restriction]:
    return [
        Restriction(
            kind=RestrictionKind(r["kind"]),
            starts_at=_dt(r["starts_at"]),
            ends_at=_dt(r["ends_at"]),
            note=r.get("note", ""),
        )
        for r in data.get("restrictions", [])
    ]


def parse_tariff(data: dict) -> TariffSchedule:
    slots_raw = data.get("tariff", {}).get("slots")
    if not slots_raw:
        return DEFAULT_TARIFF
    slots = tuple(
        TariffSlot(
            tier=TariffTier(s["tier"]),
            start=datetime.strptime(s["start"], "%H:%M").time(),
            end=datetime.strptime(s["end"], "%H:%M").time(),
            price_cny_per_kwh=float(s["price_cny_per_kwh"]),
        )
        for s in slots_raw
    )
    return TariffSchedule(slots=slots)

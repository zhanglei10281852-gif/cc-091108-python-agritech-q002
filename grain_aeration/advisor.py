"""通风建议生成器。

只产出**带依据的建议**，不直接驱动设备；是否执行由有权限值班员在 controller
中确认。每个周期：
1. 评估测点（隔离漂移点、扩大风险区间）；
2. 安全门否决（熏蒸/雨雪/风道压力/互锁/急停）；
3. 结露判据（入风露点 vs 粮堆最低温度，风险区间加大裕量）；
4. 降温收益与分时电价估算。
建议为 START / DEFER / STOP 三选一，并携带全部证据与预期指标。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import (
    Action,
    Evidence,
    Fan,
    FanRuntimeState,
    Recommendation,
    RecommendationStatus,
    Severity,
    WeatherPoint,
    aware,
)
from .psychrometrics import (
    DEWPOINT_MARGIN_C,
    condensation_risk,
    dew_point,
)
from .safety import SafetyContext, evaluate as safety_evaluate
from .sensors import SensorAssessment, assess_sensors
from .tariff import DEFAULT_TARIFF, TariffSchedule, TariffTier
from . import sensors as sensors_mod
from .models import Sensor, SensorReading

# 谷段通风的目标时长（小时）；峰段若非安全需要不建议运行
PLANNED_HOURS = 4.0
# 经验降温效率：每运行 1 小时，可把均温拉近入风干球的比例（仅用于预期估算）
COOLING_RATE_PER_HOUR = 0.35
RECOMMENDATION_TTL = timedelta(minutes=15)


@dataclass
class AdvisorInputs:
    at: datetime
    sensors: list[Sensor]
    readings: dict[str, SensorReading]
    weather: WeatherPoint
    fans: list[Fan]
    fan_states: dict
    pressures: dict
    restrictions: list
    emergency_stopped: bool = False
    planned_hours: float = PLANNED_HOURS
    manual_isolation: dict[str, str] = field(default_factory=dict)


@dataclass
class AdvisorResult:
    recommendation: Recommendation
    assessment: SensorAssessment


class Advisor:
    def __init__(self, tariff: TariffSchedule | None = None) -> None:
        self.tariff = tariff or DEFAULT_TARIFF

    def _isolation_evidence(self, assessment: SensorAssessment) -> list[Evidence]:
        out: list[Evidence] = []
        for sid, reading in sorted(assessment.isolated.items()):
            out.append(
                Evidence(
                    code="sensor_isolated",
                    severity=Severity.WARN,
                    message=(
                        f"测点 {sid} 已隔离（{assessment.isolation_reasons.get(sid, '质量异常')}），"
                        f"读数 {reading.temperature_c:.1f}°C 不参与本轮决策"
                    ),
                    values={
                        "isolated_sensors": [sid],
                        "quality": reading.quality.value,
                        "manual": sid in assessment.manually_isolated,
                    },
                )
            )
        if assessment.expanded_zone:
            zone = sorted(assessment.expanded_zone)
            out.append(
                Evidence(
                    code="risk_zone_expanded",
                    severity=Severity.WARN,
                    message=(
                        "因相邻测点漂移/失效，风险区间扩大至 "
                        + ", ".join(zone)
                        + f"；这些点结露安全裕量额外 +{sensors_mod.RISK_ZONE_EXTRA_MARGIN_C:.1f}°C"
                    ),
                    values={"expanded_zone": zone},
                )
            )
        return out

    def build(self, inp: AdvisorInputs) -> AdvisorResult:
        aware(inp.at, "建议时间")
        assessment = assess_sensors(
            inp.sensors, inp.readings, inp.at, inp.manual_isolation
        )

        ctx = SafetyContext(
            at=inp.at,
            weather=inp.weather,
            pressures=inp.pressures,
            restrictions=inp.restrictions,
            fan_states=inp.fan_states,
            emergency_stopped=inp.emergency_stopped,
        )
        vetoes, blocked_fans = safety_evaluate(ctx, inp.fans)
        evidence = self._isolation_evidence(assessment)

        candidates_all = [f for f in inp.fans if f.id not in blocked_fans]
        # 同一互锁组至多选择一台（同组相邻风机不可同时运行）
        candidates: list[Fan] = []
        chosen_groups: set[str] = set()
        for f in candidates_all:
            if f.interlock_group in chosen_groups:
                continue
            chosen_groups.add(f.interlock_group)
            candidates.append(f)
        evidence.append(
            Evidence(
                code="candidate_fans",
                severity=Severity.INFO,
                message=(
                    "可启动候选风机（每个互锁组至多一台）："
                    + (", ".join(f.id for f in candidates) if candidates else "无")
                ),
                values={"candidates": [f.id for f in candidates]},
            )
        )

        running_ids = [
            fid
            for fid, s in inp.fan_states.items()
            if s is FanRuntimeState.RUNNING
        ]

        grain_dp: float | None = None
        outside_dp: float | None = None
        expected_delta = 0.0
        expected_kwh = 0.0
        expected_cost = 0.0
        action = Action.DEFER

        if not assessment.trusted:
            vetoes.append(
                Evidence(
                    code="no_trusted_sensor",
                    severity=Severity.BLOCK,
                    message="所有测点均被隔离，无法评估粮堆状态，禁止自动启动",
                    values={"isolated_sensors": sorted(assessment.isolated)},
                )
            )

        dew_safe = False
        if assessment.trusted:
            coldest_reading = assessment.coldest
            coldest_sensor = next(
                s for s in inp.sensors if s.id == coldest_reading.sensor_id
            )
            extra = assessment.extra_margin_c(coldest_sensor.id)
            margin = DEWPOINT_MARGIN_C + extra
            risk = condensation_risk(
                coldest_grain_c=coldest_reading.temperature_c,
                outside_temp_c=inp.weather.temperature_c,
                outside_humidity_pct=inp.weather.humidity_pct,
                margin_c=margin,
            )
            outside_dp = risk["outside_dewpoint_c"]
            grain_dp = round(
                dew_point(
                    coldest_reading.temperature_c,
                    coldest_reading.humidity_pct or 70.0,
                ),
                2,
            )
            dew_safe = risk["safe"]
            evidence.append(
                Evidence(
                    code="dewpoint_margin",
                    severity=Severity.INFO if dew_safe else Severity.WARN,
                    message=(
                        f"粮堆最冷点 {coldest_sensor.id} {coldest_reading.temperature_c:.1f}°C；"
                        f"入风露点 {outside_dp:.1f}°C，安全裕量 {margin:.1f}°C，"
                        f"露点余量 {risk['headroom_c']:+.1f}°C；"
                        f"仅干球温差 {risk['dry_bulb_drop_c']:+.1f}°C 不构成启动理由"
                    ),
                    values={
                        "coldest_sensor": coldest_sensor.id,
                        "coldest_grain_c": coldest_reading.temperature_c,
                        "outside_dewpoint_c": outside_dp,
                        "grain_dewpoint_c": grain_dp,
                        "headroom_c": risk["headroom_c"],
                        "margin_c": margin,
                        "expanded_zone": sorted(assessment.expanded_zone),
                    },
                )
            )
            if not dew_safe:
                vetoes.append(
                    Evidence(
                        code="dewpoint_unsafe",
                        severity=Severity.BLOCK,
                        message=(
                            f"入风露点 {outside_dp:.1f}°C 已逼近粮堆最冷点 "
                            f"{coldest_reading.temperature_c:.1f}°C（余量 "
                            f"{risk['headroom_c']:+.1f}°C），启动将导致粮面/冷点结露"
                        ),
                        values={
                            "outside_dewpoint_c": outside_dp,
                            "coldest_grain_c": coldest_reading.temperature_c,
                            "headroom_c": risk["headroom_c"],
                        },
                    )
                )

            # 预期降温：均温向入风温度收敛
            mean_grain = sum(r.temperature_c for r in assessment.trusted) / len(
                assessment.trusted
            )
            expected_delta = round(
                max(
                    0.0,
                    (mean_grain - inp.weather.temperature_c)
                    * COOLING_RATE_PER_HOUR
                    * inp.planned_hours
                    / 4.0,
                ),
                2,
            )
            total_kw = sum(f.rated_kw for f in candidates)
            expected_kwh = round(total_kw * inp.planned_hours, 1)
            tier = self.tariff.tier_at(inp.at)
            rate = self.tariff.rate_at(inp.at)
            expected_cost = round(expected_kwh * rate, 2)
            evidence.append(
                Evidence(
                    code="tariff_and_yield",
                    severity=Severity.INFO,
                    message=(
                        f"当前为{tier.value}段（{rate:.2f} 元/kWh）；"
                        f"计划 {inp.planned_hours:.1f}h，预计能耗 {expected_kwh:.0f}kWh、"
                        f"电费 {expected_cost:.2f} 元、粮均温下降约 {expected_delta:.1f}°C"
                        + ("；非谷段，建议推迟" if tier is TariffTier.PEAK else "")
                    ),
                    values={
                        "tier": tier.value,
                        "rate": rate,
                        "planned_hours": inp.planned_hours,
                        "expected_kwh": expected_kwh,
                        "expected_cost": expected_cost,
                        "expected_delta_c": expected_delta,
                    },
                )
            )

        hard_blocked = any(e.severity is Severity.BLOCK for e in vetoes)

        # 对"正在运行的风机"构成停机理由的硬否决（互锁只阻止启动同组邻机，
        # 不构成运行机本身的停机理由）
        stop_codes = {
            "emergency_stop",
            "fumigation",
            "rain_backflow",
            "rain_backflow_window",
            "sealed_storage",
            "maintenance",
            "dewpoint_unsafe",
            "no_trusted_sensor",
            "weather_unknown",
        }
        stop_vetoes = [
            v
            for v in vetoes
            if v.code in stop_codes
            or (
                v.code in {"duct_pressure_offrange", "duct_pressure_unknown"}
                and set(v.values.get("fan_ids", [])) & set(running_ids)
            )
        ]

        fan_ids: list[str] = []
        if running_ids and stop_vetoes:
            action = Action.STOP
            fan_ids = list(running_ids)
            evidence.append(
                Evidence(
                    code="stop_required",
                    severity=Severity.WARN,
                    message=(
                        "风机运行中出现硬性风险（"
                        + "、".join(sorted({v.code for v in stop_vetoes}))
                        + "），建议立即停机，无需等待下一优化周期"
                    ),
                    values={"running_fans": running_ids},
                )
            )
        elif running_ids:
            # 已在运行且无停机理由：维持运行，不重复下发启动
            action = Action.DEFER
            evidence.append(
                Evidence(
                    code="hold_running",
                    severity=Severity.INFO,
                    message=f"风机 {', '.join(running_ids)} 运行中且条件仍安全，维持运行",
                    values={"running_fans": running_ids},
                )
            )
        elif hard_blocked:
            action = Action.DEFER
        else:
            # 无硬否决：谷段或平段且确有降温收益才建议启动；峰段仅 DEFER
            tier = self.tariff.tier_at(inp.at)
            if tier is TariffTier.PEAK or expected_delta <= 0.05:
                action = Action.DEFER
            else:
                action = Action.START
                fan_ids = [f.id for f in candidates]

        rec = Recommendation(
            id=f"REC-{uuid.uuid4().hex[:8]}",
            at=inp.at,
            action=action,
            fan_ids=fan_ids,
            evidence=evidence,
            vetoes=vetoes,
            grain_dewpoint_c=grain_dp,
            outside_dewpoint_c=outside_dp,
            expected_delta_c=expected_delta,
            expected_kwh=expected_kwh,
            expected_cost=expected_cost,
            valid_until=inp.at + RECOMMENDATION_TTL,
            status=RecommendationStatus.PROPOSED,
        )
        return AdvisorResult(recommendation=rec, assessment=assessment)

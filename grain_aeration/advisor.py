"""带依据的通风控制建议。

顾问综合四类信号并逐条留存依据：

- 粮堆露点（送风露点 vs 冷端粮温的结露裕度）
- 降温收益（送风温度 vs 冷端粮温的温差）
- 风道压力（静压异常禁止启动）
- 分时电价（峰段建议推迟到谷段）

硬门禁（熏蒸、雨雪倒灌、风道压力异常、相邻风机互锁、结露风险）一律
blocks_start=True，动作 FORBIDDEN/STOP，且在任何权限下都不可被覆盖。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from .config import DomainConfig, SafetyConfig
from .models import (
    Action,
    DuctPressure,
    Evidence,
    FanPlan,
    Recommendation,
    SensorReading,
    WeatherSnapshot,
)
from .psychrometrics import condensation_margin_c
from .sensor_health import assess_sensor_health
from .tariff import TimeOfUseTariff


@dataclass(frozen=True)
class AdvisorContext:
    at: datetime
    readings: tuple[SensorReading, ...]
    weather: WeatherSnapshot
    pressures: tuple[DuctPressure, ...]
    running_fan_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AdvisorTuning:
    # 经验降温速率 ℃/h（用于估算预期时长与收益）
    cooling_rate_c_per_h: float = 0.6
    # 单次计划通风小时数
    planned_hours: float = 4.0


class Advisor:
    def __init__(
        self,
        domain: DomainConfig,
        tariff: TimeOfUseTariff | None = None,
        safety: SafetyConfig | None = None,
        tuning: AdvisorTuning | None = None,
    ):
        self.domain = domain
        self.tariff = tariff or TimeOfUseTariff.default()
        self.safety = safety or SafetyConfig()
        self.tuning = tuning or AdvisorTuning()

    # ------------------------------------------------------------------ #
    def evaluate(self, ctx: AdvisorContext) -> Recommendation:
        s = self.safety
        reasons: list[Evidence] = []
        # safety_blocks：熏蒸/倒灌/压力/露点等安全门禁（出现即禁止启动，
        # 且若有风机在运行则建议立即停机）
        safety_blocks: list[Evidence] = []
        # interlock_blocks：相邻风机容量互锁，只阻止“再启动”，不阻止停机
        interlock_blocks: list[Evidence] = []

        health = assess_sensor_health(
            ctx.readings, self.domain.depth_m, s.drift_expansion_factor
        )

        # 1) 熏蒸等安全限制：硬门禁
        for r in self.domain.any_blocking_restriction(ctx.at):
            ev = Evidence(
                code="RESTRICTION_FUMIGATION" if r.kind == "fumigation" else "RESTRICTION",
                message=(
                    f"处于{r.kind}限制时段 "
                    f"{r.starts_at:%Y-%m-%d %H:%M}–{r.ends_at:%H:%M}，禁止通风"
                ),
                value=r.kind,
                blocks_start=True,
            )
            safety_blocks.append(ev)

        # 2) 雨雪倒灌风险：硬门禁
        rain_ingress = ctx.weather.rain and (
            ctx.weather.humidity_pct >= s.rain_ingress_humidity_pct
        )
        if ctx.weather.rain:
            ev = Evidence(
                code="WEATHER_RAIN_INGRESS" if rain_ingress else "WEATHER_RAIN",
                message=(
                    f"外界降雨（湿度 {ctx.weather.humidity_pct:.0f}%），"
                    + ("存在雨雪倒灌风险，禁止启动" if rain_ingress else "持续观察")
                ),
                value=ctx.weather.humidity_pct,
                blocks_start=rain_ingress,
            )
            (safety_blocks if rain_ingress else reasons).append(ev)

        # 3) 风道压力异常：硬门禁（按组判定）
        pressure_ok_groups = self._pressure_evidence(ctx.pressures, safety_blocks, reasons)

        # 4) 相邻风机互锁：同组已在运行且超过允许数量 => 阻止同组再启动
        available_fans = self._interlock_plan(
            ctx.running_fan_ids, pressure_ok_groups, interlock_blocks
        )

        # 5) 结露裕度（冷端包络 vs 送风露点）
        cold_end = health.conservative_cold_end_c()
        margin: float | None = None
        if cold_end is not None:
            margin = condensation_margin_c(cold_end, ctx.weather.dewpoint_c)
            margin_ok = margin > s.min_condensation_margin_c
            ev = Evidence(
                code="DEWPOINT_MARGIN",
                message=(
                    f"冷端粮温 {cold_end:.1f}℃，送风露点 {ctx.weather.dewpoint_c:.1f}℃，"
                    f"结露裕度 {margin:+.1f}℃（安全余量 {s.min_condensation_margin_c:.1f}℃）"
                    + ("" if margin_ok else "：送风会在粮堆内结露，禁止启动")
                ),
                value=round(margin, 2),
                blocks_start=not margin_ok,
            )
            (safety_blocks if not margin_ok else reasons).append(ev)
        else:
            safety_blocks.append(
                Evidence(
                    code="SENSOR_ALL_ISOLATED",
                    message="所有测温点均被隔离，无法评估粮温，按风险处理禁止启动",
                    value=True,
                    blocks_start=True,
                )
            )

        # 6) 传感器隔离与风险区间扩大（信息依据）
        for iso in health.isolated:
            reasons.append(
                Evidence(
                    code="SENSOR_ISOLATED",
                    message=(
                        f"测点 {iso.id}（深度 {iso.depth_m}m）状态 {iso.quality.value}，"
                        "已隔离，不参与聚合"
                    ),
                    value=iso.id,
                )
            )
        if health.risk_bands:
            reasons.append(
                Evidence(
                    code="RISK_BAND_EXPANDED",
                    message=(
                        "漂移点周围风险区间已按相邻电缆外扩并包络至粮堆边界："
                        + "，".join(f"{lo:.1f}–{hi:.1f}m" for lo, hi in health.risk_bands)
                    ),
                    value=str(health.risk_bands),
                )
            )

        # 7) 降温收益
        cooling_delta = (
            cold_end - ctx.weather.temperature_c if cold_end is not None else None
        )
        if cooling_delta is not None:
            delta_ok = cooling_delta >= s.min_cooling_delta_c
            reasons.append(
                Evidence(
                    code="COOLING_DELTA",
                    message=(
                        f"冷端粮温 {cold_end:.1f}℃，外温 {ctx.weather.temperature_c:.1f}℃，"
                        f"可降温差 {cooling_delta:+.1f}℃（门槛 {s.min_cooling_delta_c:.1f}℃）"
                        + ("" if delta_ok else "：降温收益不足")
                    ),
                    value=round(cooling_delta, 2),
                )
            )
        else:
            delta_ok = False

        # 8) 分时电价
        price = self.tariff.price_at(ctx.at)
        is_peak = self.tariff.is_peak_at(ctx.at)
        reasons.append(
            Evidence(
                code="TARIFF",
                message=(
                    f"当前{'峰' if is_peak else '非峰'}段电价 {price:.2f} 元/kWh"
                    + ("，建议推迟到谷段" if is_peak else "")
                ),
                value=price,
            )
        )

        # ---------------------------- 决策 ---------------------------- #
        running = set(ctx.running_fan_ids)
        all_reasons = safety_blocks + interlock_blocks + reasons
        if safety_blocks:
            if running:
                # 运行中出现安全门禁（突降暴雨、露点反转、压力异常等）：立即停机。
                # 门禁禁止的是“启动”，从不阻止“停机”这个安全方向。
                action = Action.STOP
                fan_plans = self._stop_plans(running)
            else:
                action = Action.FORBIDDEN
                fan_plans = []
        elif cooling_delta is not None and not delta_ok:
            action = Action.STOP if running else Action.HOLD
            fan_plans = self._stop_plans(running) if running else []
        elif is_peak and available_fans and not running:
            action = Action.DEFER_TO_OFF_PEAK
            fan_plans = []
        elif available_fans:
            # 有空余机位且条件合适：启动可启动的风机，已运行的继续运行
            action = Action.START
            fan_plans = [
                FanPlan(
                    fan_id=f.id,
                    interlock_group=f.interlock_group,
                    rated_kw=f.rated_kw,
                    start=True,
                )
                for f in available_fans
            ]
        else:
            # 无安全门禁但也没有空余机位（互锁容量已满）：维持运行
            action = Action.HOLD
            fan_plans = []

        expected = self._expectations(
            fan_plans, cooling_delta if action is Action.START else None, ctx.at
        )

        return Recommendation(
            rec_id=f"REC-{uuid.uuid4().hex[:12]}",
            created_at=ctx.at,
            action=action,
            reasons=all_reasons,
            fan_plans=fan_plans,
            based_on_weather_at=ctx.weather.at,
            based_on_sensor_at=max((r.at for r in ctx.readings), default=None),
            valid_for_seconds=s.recommendation_ttl_seconds,
            risk_depth_band=health.overall_risk_band,
            isolated_sensors=health.isolated_ids,
            **expected,
        )

    # ------------------------------------------------------------------ #
    def forced_start_plans(
        self,
        ctx: AdvisorContext,
    ) -> tuple[list, list[Evidence]]:
        """人工强制覆盖软建议时的启动计划。

        只服从安全门禁（熏蒸/倒灌/压力/露点/全测点失效）与互锁，
        忽略峰段电价与降温收益门槛。返回 (风机计划, 安全门禁依据)。
        """
        s = self.safety
        safety_blocks: list[Evidence] = []
        health = assess_sensor_health(
            ctx.readings, self.domain.depth_m, s.drift_expansion_factor
        )
        for r in self.domain.any_blocking_restriction(ctx.at):
            safety_blocks.append(Evidence(
                code="RESTRICTION_FUMIGATION" if r.kind == "fumigation" else "RESTRICTION",
                message=f"处于{r.kind}限制时段，禁止通风", value=r.kind, blocks_start=True,
            ))
        rain_ingress = ctx.weather.rain and ctx.weather.humidity_pct >= s.rain_ingress_humidity_pct
        if rain_ingress:
            safety_blocks.append(Evidence(
                code="WEATHER_RAIN_INGRESS", message="雨雪倒灌风险", value=True,
                blocks_start=True,
            ))
        pressure_ok_groups = self._pressure_evidence(ctx.pressures, safety_blocks, [])
        cold_end = health.conservative_cold_end_c()
        if cold_end is None:
            safety_blocks.append(Evidence(
                code="SENSOR_ALL_ISOLATED", message="所有测点均被隔离", value=True,
                blocks_start=True,
            ))
        else:
            margin = condensation_margin_c(cold_end, ctx.weather.dewpoint_c)
            if margin <= s.min_condensation_margin_c:
                safety_blocks.append(Evidence(
                    code="DEWPOINT_MARGIN",
                    message=f"结露裕度 {margin:+.1f}℃ 不足，禁止启动",
                    value=round(margin, 2), blocks_start=True,
                ))
        if safety_blocks:
            return [], safety_blocks
        plans: list[FanPlan] = []
        groups = self.domain.duct_groups()
        for group, fans in groups.items():
            if group not in pressure_ok_groups:
                continue
            running = [f for f in fans if f.id in ctx.running_fan_ids]
            slots = s.max_running_per_duct_group - len(running)
            for f in fans[:max(0, slots)]:
                if f.id not in ctx.running_fan_ids:
                    plans.append(FanPlan(f.id, f.interlock_group, f.rated_kw, start=True))
        return plans, []

    # ------------------------------------------------------------------ #
    def _pressure_evidence(
        self,
        pressures: tuple[DuctPressure, ...],
        blocking: list[Evidence],
        reasons: list[Evidence],
    ) -> set[str]:
        """返回压力正常的风道组集合；异常/缺测组加入硬门禁。"""
        s = self.safety
        all_groups = set(self.domain.duct_groups())
        ok_groups: set[str] = set()
        seen: set[str] = set()
        for p in pressures:
            seen.add(p.duct_group)
            in_range = s.duct_static_min_pa <= p.static_pa <= s.duct_static_max_pa
            good = p.ok and in_range
            ev = Evidence(
                code="DUCT_PRESSURE",
                message=(
                    f"风道 {p.duct_group} 静压 {p.static_pa:.0f}Pa"
                    + ("" if good else "：超出正常区间或设备告警，禁止该风道启动")
                ),
                value=p.static_pa,
                blocks_start=not good,
            )
            (reasons if good else blocking).append(ev)
            if good:
                ok_groups.add(p.duct_group)
        for missing in all_groups - seen:
            blocking.append(
                Evidence(
                    code="DUCT_PRESSURE_MISSING",
                    message=f"风道 {missing} 缺少压力采样，按异常处理禁止启动",
                    value=missing,
                    blocks_start=True,
                )
            )
        return ok_groups

    def _interlock_plan(
        self,
        running_fan_ids: frozenset[str],
        pressure_ok_groups: set[str],
        interlock_blocks: list[Evidence],
    ) -> list:
        """互锁校验并返回当前可启动的风机（每组不超过 max_running_per_duct_group）。"""
        s = self.safety
        groups = self.domain.duct_groups()
        available: list = []
        for group, fans in groups.items():
            running_in_group = [f for f in fans if f.id in running_fan_ids]
            if len(running_in_group) >= s.max_running_per_duct_group:
                interlock_blocks.append(
                    Evidence(
                        code="INTERLOCK_ADJACENT_FAN",
                        message=(
                            f"风道 {group} 中相邻风机 {','.join(f.id for f in running_in_group)} "
                            "正在运行，互锁仅阻止同组风机再启动（不影响停机）"
                        ),
                        value=group,
                        blocks_start=False,
                    )
                )
                continue
            if group not in pressure_ok_groups:
                continue  # 压力门禁已记录依据
            slots = s.max_running_per_duct_group - len(running_in_group)
            for f in fans[:slots]:
                if f.id not in running_fan_ids:
                    available.append(f)
        return available

    def _stop_plans(self, running: set[str]) -> list[FanPlan]:
        return [
            FanPlan(
                fan_id=fid,
                interlock_group=self.domain.fan(fid).interlock_group,
                rated_kw=self.domain.fan(fid).rated_kw,
                start=False,
            )
            for fid in sorted(running)
        ]

    def _expectations(self, fan_plans: list[FanPlan], cooling_delta: float | None,
                      at: datetime) -> dict:
        starts = [p for p in fan_plans if p.start]
        if not starts or cooling_delta is None:
            return {
                "expected_temp_drop_c": 0.0,
                "expected_duration_h": 0.0,
                "expected_energy_kwh": 0.0,
                "expected_cost": 0.0,
            }
        from datetime import timedelta

        hours = self.tuning.planned_hours
        kw = sum(p.rated_kw for p in starts)
        achievable = min(cooling_delta, self.tuning.cooling_rate_c_per_h * hours)
        energy = kw * hours
        # 按计划运行窗口在分时电价上积分预期电费
        _, cost = self.tariff.cost_for_run(kw, at, at + timedelta(hours=hours))
        return {
            "expected_temp_drop_c": round(achievable, 2),
            "expected_duration_h": hours,
            "expected_energy_kwh": round(energy, 2),
            "expected_cost": round(cost, 2),
        }

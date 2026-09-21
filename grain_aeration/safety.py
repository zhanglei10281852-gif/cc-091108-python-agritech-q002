"""安全否决门：不可被普通控制（含电价优化）覆盖的硬性禁止条件。

任何一条 BLOCK 成立 → 直接禁止启动，建议只能是 DEFER/STOP：
- 熏蒸期；
- 当下降雨/雪，或处于人工划定的雨雪倒灌风险窗口；
- 风道静压偏离参考区间（堵塞、风阀未开、倒灌后风阻异常）；
- 相邻风机互锁：同互锁组已有风机在运行（含"待核实"状态，保守视为可能在转）。

另含一个非否决但重要的结露判据（在 advisor 中使用），以及风机候选筛选。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import (
    DuctPressure,
    Evidence,
    Fan,
    FanRuntimeState,
    Restriction,
    RestrictionKind,
    Severity,
    WeatherPoint,
    aware,
)


@dataclass
class SafetyContext:
    at: datetime
    weather: WeatherPoint | None
    pressures: dict[str, DuctPressure]   # duct_id -> 最新压力
    restrictions: list[Restriction]
    fan_states: dict[str, FanRuntimeState]
    emergency_stopped: bool = False


def _active_restrictions(ctx: SafetyContext) -> list[Restriction]:
    return [r for r in ctx.restrictions if r.active_at(ctx.at)]


def check_restrictions(ctx: SafetyContext) -> list[Evidence]:
    out: list[Evidence] = []
    names = {
        RestrictionKind.FUMIGATION: ("fumigation", "熏蒸期内禁止通风启动"),
        RestrictionKind.RAIN_BACKFLOW: ("rain_backflow_window", "处于雨雪倒灌风险窗口，禁止启动"),
        RestrictionKind.SEALED: ("sealed_storage", "仓房处于密闭储藏状态"),
        RestrictionKind.MAINTENANCE: ("maintenance", "设备检修中，禁止启动"),
    }
    for r in _active_restrictions(ctx):
        code, msg = names[r.kind]
        out.append(
            Evidence(
                code=code,
                severity=Severity.BLOCK,
                message=msg + (f"（{r.note}）" if r.note else ""),
                values={
                    "window": [r.starts_at.isoformat(), r.ends_at.isoformat()],
                },
            )
        )
    return out


def check_weather(ctx: SafetyContext) -> list[Evidence]:
    out: list[Evidence] = []
    w = ctx.weather
    if w is None:
        out.append(
            Evidence(
                code="weather_unknown",
                severity=Severity.BLOCK,
                message="缺少当前外界气象观测，无法排除雨雪倒灌，保守禁止启动",
                values={},
            )
        )
        return out
    aware(w.at, "气象观测时间")
    if w.rain:
        out.append(
            Evidence(
                code="rain_backflow",
                severity=Severity.BLOCK,
                message="外界正在降雨/雪，存在风口倒灌与粮面结露风险，禁止启动",
                values={
                    "temperature_c": w.temperature_c,
                    "humidity_pct": w.humidity_pct,
                    "rain": True,
                },
            )
        )
    if w.humidity_pct >= 90.0:
        out.append(
            Evidence(
                code="outside_saturated",
                severity=Severity.WARN,
                message="外界相对湿度≥90%，通风基本无除湿收益，需结合露点判断",
                values={"humidity_pct": w.humidity_pct},
            )
        )
    return out


def check_duct_pressure(ctx: SafetyContext, fans: list[Fan]) -> list[Evidence]:
    out: list[Evidence] = []
    # 按风道聚合关联风机（F-1/F-2 共用 DUCT-N 时只产生一条依据）
    duct_fans: dict[str, list[str]] = {}
    for fan in fans:
        if fan.duct_id:
            duct_fans.setdefault(fan.duct_id, []).append(fan.id)

    for duct_id, fan_ids in duct_fans.items():
        p = ctx.pressures.get(duct_id)
        if p is None:
            out.append(
                Evidence(
                    code="duct_pressure_unknown",
                    severity=Severity.BLOCK,
                    message=f"风道 {duct_id} 缺少压力采样，关联风机 {', '.join(fan_ids)} 禁止启动",
                    values={"duct_id": duct_id, "fan_ids": fan_ids},
                )
            )
            continue
        if not p.in_range:
            lo, hi = p.reference_range_pa
            out.append(
                Evidence(
                    code="duct_pressure_offrange",
                    severity=Severity.BLOCK,
                    message=(
                        f"风道 {p.duct_id} 静压 {p.static_pressure_pa:.0f} Pa 超出参考区间 "
                        f"[{lo:.0f}, {hi:.0f}] Pa，疑似堵塞/风阀未开/倒灌，"
                        f"关联风机 {', '.join(fan_ids)} 禁止启动"
                    ),
                    values={
                        "fan_ids": fan_ids,
                        "duct_id": p.duct_id,
                        "static_pressure_pa": p.static_pressure_pa,
                        "reference_range_pa": [lo, hi],
                    },
                )
            )
    return out


def check_interlock(
    ctx: SafetyContext, fans: list[Fan]
) -> tuple[list[Evidence], set[str]]:
    """同互锁组任一风机非 STOPPED（含待核实/运行/中间态）→ 组内其余风机禁止启动。

    返回 (否决依据, 被互锁挡住的风机 id 集合)。
    """
    out: list[Evidence] = []
    blocked_fans: set[str] = set()
    groups: dict[str, list[Fan]] = {}
    for f in fans:
        groups.setdefault(f.interlock_group, []).append(f)

    for group, members in groups.items():
        # 非"已证实停机"的任何状态（运行/中间态/故障/待核实）都占用互锁组；
        # 状态表里缺失时按已停机处理（初始状态）。
        busy = sorted(
            {
                f.id
                for f in members
                if ctx.fan_states.get(f.id, FanRuntimeState.STOPPED)
                is not FanRuntimeState.STOPPED
            }
        )
        if busy:
            for f in members:
                if f.id not in busy:
                    blocked_fans.add(f.id)
            out.append(
                Evidence(
                    code="fan_interlock",
                    severity=Severity.BLOCK,
                    message=(
                        f"互锁组 {group} 中 {', '.join(busy)} 未处于已证实停机状态"
                        f"（运行/中间态/故障/待核实均占用），同组相邻风机禁止启动"
                    ),
                    values={"interlock_group": group, "busy_fans": busy},
                )
            )
    return out, blocked_fans


def check_emergency(ctx: SafetyContext) -> list[Evidence]:
    if not ctx.emergency_stopped:
        return []
    return [
        Evidence(
            code="emergency_stop",
            severity=Severity.BLOCK,
            message="紧急人工停机已触发，控制中枢锁定，所有启动指令禁止下发",
            values={},
        )
    ]


def evaluate(
    ctx: SafetyContext, fans: list[Fan]
) -> tuple[list[Evidence], set[str]]:
    """汇总全部硬性否决。返回 (vetoes, 被否决风机)。

    全局性否决（急停/熏蒸/雨雪/气象缺失）一旦出现，所有风机都不可启动；
    风道压力按风机所属风道逐个否决；互锁只否决同组相邻风机。
    """
    vetoes: list[Evidence] = []
    emergency_vetoes = check_emergency(ctx)
    restriction_vetoes = check_restrictions(ctx)
    weather_vetoes = check_weather(ctx)
    duct_vetoes = check_duct_pressure(ctx, fans)
    ilock_vetoes, interlock_blocked = check_interlock(ctx, fans)

    vetoes = (
        emergency_vetoes
        + restriction_vetoes
        + weather_vetoes
        + duct_vetoes
        + ilock_vetoes
    )

    blocked: set[str] = set()
    global_block = bool(
        ctx.emergency_stopped
        or any(v.severity is Severity.BLOCK for v in restriction_vetoes)
        or any(v.severity is Severity.BLOCK for v in weather_vetoes)
    )
    if global_block:
        blocked |= {f.id for f in fans}
    for v in duct_vetoes:
        if v.severity is Severity.BLOCK:
            blocked |= set(v.values.get("fan_ids", []))
    blocked |= interlock_blocked
    return vetoes, blocked

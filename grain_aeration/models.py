"""领域数据模型：传感器、风机、气象、事件、命令与状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Action(str, Enum):
    """顾问输出的动作。"""

    START = "START"                            # 建议启动通风
    HOLD = "HOLD"                              # 条件不足，维持现状
    STOP = "STOP"                              # 目标达成或条件转差，建议停机
    FORBIDDEN = "FORBIDDEN"                    # 硬门禁：禁止启动
    DEFER_TO_OFF_PEAK = "DEFER_TO_OFF_PEAK"    # 可行但高峰电价，建议推迟


class ApprovalDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    # 人工强制覆盖（硬门禁永远不可覆盖；仅用于 HOLD/DEFER 等软建议）
    FORCE_OVERRIDE = "FORCE_OVERRIDE"


class CommandVerb(str, Enum):
    START_FAN = "START_FAN"
    STOP_FAN = "STOP_FAN"
    FORCE_STOP_ALL = "FORCE_STOP_ALL"  # 紧急人工停机，立即执行，不等优化周期


class SensorQuality(str, Enum):
    GOOD = "good"
    DRIFTING = "drifting"   # 漂移：该点必须隔离
    BAD = "bad"             # 失效：该点必须隔离


class FanState(str, Enum):
    """风机由证据支持的运行状态。"""

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    # 进程重启后，凡没有“已关闭”证据的风机一律进入待核实，
    # 绝不能凭内存缺失就假定为安全停机。
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class User:
    """值班员账号与权限。"""

    username: str
    role: str  # "operator" | "quality_manager" | "admin"

    def can_approve(self) -> bool:
        return self.role in {"quality_manager", "admin"}

    def can_emergency_stop(self) -> bool:
        # 任何在岗值班员都可以紧急停机
        return self.role in {"operator", "quality_manager", "admin"}


@dataclass(frozen=True)
class SensorReading:
    id: str
    depth_m: float
    at: datetime
    temperature_c: float | None = None
    humidity_pct: float | None = None
    quality: SensorQuality = SensorQuality.GOOD

    @property
    def isolated(self) -> bool:
        return self.quality is not SensorQuality.GOOD


@dataclass(frozen=True)
class WeatherSnapshot:
    at: datetime
    temperature_c: float
    humidity_pct: float
    rain: bool = False
    wind_mps: float = 0.0

    @property
    def dewpoint_c(self) -> float:
        # 延迟导入避免循环
        from .psychrometrics import dewpoint_from_rh

        return dewpoint_from_rh(self.temperature_c, self.humidity_pct)


@dataclass(frozen=True)
class DuctPressure:
    """风道压力采样。"""

    duct_group: str
    at: datetime
    static_pa: float
    ok: bool


@dataclass(frozen=True)
class Evidence:
    """一条建议依据：代码 + 人读说明 + 量化值。"""

    code: str
    message: str
    value: float | str | bool | None = None
    blocks_start: bool = False  # 该依据是否构成硬门禁


@dataclass(frozen=True)
class FanPlan:
    """单台风机的执行计划。"""

    fan_id: str
    interlock_group: str
    rated_kw: float
    start: bool


@dataclass(frozen=True)
class Recommendation:
    """带依据的控制建议。"""

    rec_id: str
    created_at: datetime
    action: Action
    reasons: list[Evidence] = field(default_factory=list)
    fan_plans: list[FanPlan] = field(default_factory=list)
    # 建议所基于的数据时间（用于批准时的新鲜度校验）
    based_on_weather_at: datetime | None = None
    based_on_sensor_at: datetime | None = None
    valid_for_seconds: int = 300
    # 预期收益与成本，供事后对标
    expected_temp_drop_c: float = 0.0
    expected_duration_h: float = 0.0
    expected_energy_kwh: float = 0.0
    expected_cost: float = 0.0
    # 扩大后的风险深度区间（米），即便传感器正常也给出全粮堆范围
    risk_depth_band: tuple[float, float] = (0.0, 0.0)
    isolated_sensors: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.action is Action.FORBIDDEN or any(r.blocks_start for r in self.reasons)

    @property
    def expires_at(self) -> datetime:
        return self.created_at + _timedelta(self.valid_for_seconds)


def _timedelta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


@dataclass(frozen=True)
class Approval:
    rec_id: str
    decision: ApprovalDecision
    approver: User
    at: datetime
    note: str = ""


@dataclass(frozen=True)
class Command:
    """下发给设备的指令。"""

    command_id: str
    rec_id: str | None
    verb: CommandVerb
    fan_id: str | None
    issued_at: datetime
    issued_by: str
    interlock_group: str | None = None
    expected_rated_kw: float | None = None


@dataclass(frozen=True)
class FanStatusReceipt:
    """设备回执。confirmed=True 表示设备明确回报了状态。"""

    command_id: str
    fan_id: str
    at: datetime
    running: bool
    confirmed: bool
    message: str = ""


@dataclass(frozen=True)
class SessionSummary:
    """一次通风会话结束后的实际结果（由回执与电表读数汇总）。"""

    rec_id: str
    fan_ids: tuple[str, ...]
    started_at: datetime
    ended_at: datetime
    actual_duration_h: float
    actual_energy_kwh: float
    actual_cost: float
    # 通风前/后冷端（最保守、最易结露层）粮温
    cold_end_before_c: float
    cold_end_after_c: float

    @property
    def actual_temp_drop_c(self) -> float:
        return self.cold_end_before_c - self.cold_end_after_c


# ---- 事件（持久化到 JSONL，时间线回放与崩溃恢复都以它为唯一事实来源） ----

@dataclass(frozen=True)
class Event:
    seq: int
    at: datetime
    kind: str
    payload: dict[str, Any]

"""领域值对象与枚举。

约定：
- 温度：摄氏度；相对湿度：百分数（0-100）；压力：帕；功率：千瓦。
- 所有时间均为携带时区的 ``datetime``，模块内部拒绝 naive datetime。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo


def aware(at: datetime, label: str = "时间") -> datetime:
    """确保时间携带时区（粮库所有记录必须可跨时区回放）。"""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError(f"{label}必须携带明确时区")
    return at


CN_TZ = ZoneInfo("Asia/Shanghai")


class Role(str, Enum):
    """系统角色。只有值班员（及以上）能确认执行建议。"""

    VIEWER = "viewer"            # 只读
    OPERATOR = "operator"        # 值班员：可确认/执行、可急停
    QUALITY_MANAGER = "manager"  # 质量经理：可批准/执行、看复盘
    ADMIN = "admin"              # 维护权限、隔离测点


# 可把建议推进到"执行"的角色
APPROVER_ROLES = frozenset({Role.OPERATOR, Role.QUALITY_MANAGER, Role.ADMIN})


@dataclass(frozen=True)
class Actor:
    id: str
    name: str
    role: Role

    def can_approve(self) -> bool:
        return self.role in APPROVER_ROLES

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role.value}


class SensorQuality(str, Enum):
    GOOD = "good"
    DRIFTING = "drifting"   # 漂移：该点隔离，不参与决策
    SUSPECT = "suspect"     # 可疑：降级使用
    INVALID = "invalid"     # 失效：该点隔离


# 需要隔离（不进入结露/降温判断）的质量状态
ISOLATED_QUALITIES = frozenset({SensorQuality.DRIFTING, SensorQuality.INVALID})


class GrainLayer(str, Enum):
    TOP = "top"
    MIDDLE = "middle"
    BOTTOM = "bottom"


@dataclass(frozen=True)
class Sensor:
    """测温电缆上的一个测点，绑定粮层深度与电缆坐标。"""

    id: str
    cable_id: str
    depth_m: float
    layer: GrainLayer
    # 沿电缆的点序（0 为最浅），用于"扩大风险区间"时定位相邻点
    cable_index: int = 0
    rated_range_c: tuple[float, float] = (-20.0, 60.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cable_id": self.cable_id,
            "depth_m": self.depth_m,
            "layer": self.layer.value,
            "cable_index": self.cable_index,
        }


@dataclass(frozen=True)
class SensorReading:
    sensor_id: str
    at: datetime
    temperature_c: float
    humidity_pct: float | None
    quality: SensorQuality

    def __post_init__(self) -> None:
        aware(self.at, f"测点 {self.sensor_id} 的读数时间")


@dataclass(frozen=True)
class WeatherPoint:
    """历史气象片段中的一个外界观测点。"""

    at: datetime
    temperature_c: float
    humidity_pct: float
    rain: bool = False
    wind_speed_mps: float = 0.0

    def __post_init__(self) -> None:
        aware(self.at, "气象观测时间")


@dataclass(frozen=True)
class DuctPressure:
    """风道静压采样。负压持续偏大提示风道堵塞/雨雪倒灌后的风阻异常。"""

    duct_id: str
    at: datetime
    static_pressure_pa: float       # 相对大气压的静压（通风时一般为负）
    reference_range_pa: tuple[float, float] = (-1200.0, -100.0)

    def __post_init__(self) -> None:
        aware(self.at, "风道压力采样时间")

    @property
    def in_range(self) -> bool:
        lo, hi = self.reference_range_pa
        return lo <= self.static_pressure_pa <= hi


@dataclass(frozen=True)
class Fan:
    id: str
    interlock_group: str   # 同组风机互锁：任一运行即禁止再启动同组其它风机
    rated_kw: float
    duct_id: str = ""

    @property
    def label(self) -> str:
        return f"{self.id}({self.interlock_group})"


class FanRuntimeState(str, Enum):
    STOPPED = "stopped"                       # 已证实停止（有关闭回执）
    STARTING = "starting"                     # 启动指令已发，待回执
    RUNNING = "running"                       # 运行（有开启回执）
    STOPPING = "stopping"                     # 停机指令已发，待回执
    FAULT = "fault"                           # 设备回执 NAK/故障
    PENDING_VERIFICATION = "pending"          # 重启后未证实关闭：待人工核实

    @property
    def presumed_on(self) -> bool:
        """保守语义：非 STOPPED 都不能当作安全停机。"""
        return self is not FanRuntimeState.STOPPED

    @property
    def awaiting_receipt(self) -> bool:
        return self in (FanRuntimeState.STARTING, FanRuntimeState.STOPPING)


class Action(str, Enum):
    START = "start"          # 建议启动通风
    DEFER = "defer"          # 暂不启动（结露/电价/风险未明），等待下一周期
    STOP = "stop"            # 建议停机


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"          # 直接禁止启动的硬性依据


@dataclass(frozen=True)
class Evidence:
    """一条可追溯的判断依据。"""

    code: str                       # 如 dewpoint_unsafe / fumigation / tariff_peak
    severity: Severity
    message: str
    values: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "values": self.values,
        }


class RecommendationStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    EXPIRED = "expired"


@dataclass
class Recommendation:
    """带依据的控制建议（建议方产出，等待有权限值班员确认）。"""

    id: str
    at: datetime
    action: Action
    fan_ids: list[str]
    evidence: list[Evidence]
    vetoes: list[Evidence]
    grain_dewpoint_c: float | None
    outside_dewpoint_c: float | None
    expected_delta_c: float          # 预期本轮可降温幅度
    expected_kwh: float              # 预期本轮能耗
    expected_cost: float             # 预期本轮电费
    valid_until: datetime
    status: RecommendationStatus = RecommendationStatus.PROPOSED
    decided_by: Actor | None = None
    decided_at: datetime | None = None
    note: str = ""

    def __post_init__(self) -> None:
        aware(self.at, "建议生成时间")
        aware(self.valid_until, "建议有效期")

    @property
    def blocked(self) -> bool:
        return any(e.severity is Severity.BLOCK for e in self.vetoes)

    @property
    def risk_sensor_ids(self) -> list[str]:
        ids: list[str] = []
        for e in self.evidence + self.vetoes:
            ids.extend(e.values.get("isolated_sensors", []))
            ids.extend(e.values.get("expanded_zone", []))
        return sorted(set(ids))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "at": self.at.isoformat(),
            "action": self.action.value,
            "fan_ids": list(self.fan_ids),
            "blocked": self.blocked,
            "evidence": [e.to_dict() for e in self.evidence],
            "vetoes": [e.to_dict() for e in self.vetoes],
            "grain_dewpoint_c": self.grain_dewpoint_c,
            "outside_dewpoint_c": self.outside_dewpoint_c,
            "expected_delta_c": self.expected_delta_c,
            "expected_kwh": self.expected_kwh,
            "expected_cost": self.expected_cost,
            "valid_until": self.valid_until.isoformat(),
            "status": self.status.value,
            "decided_by": self.decided_by.to_dict() if self.decided_by else None,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "note": self.note,
        }


class RestrictionKind(str, Enum):
    FUMIGATION = "fumigation"            # 熏蒸期：禁止启动
    RAIN_BACKFLOW = "rain_backflow"      # 雨雪倒灌风险窗口（人工划定）
    SEALED = "sealed"                    # 密闭储藏
    MAINTENANCE = "maintenance"          # 设备检修


@dataclass(frozen=True)
class Restriction:
    kind: RestrictionKind
    starts_at: datetime
    ends_at: datetime
    note: str = ""

    def __post_init__(self) -> None:
        aware(self.starts_at, "限制开始时间")
        aware(self.ends_at, "限制结束时间")
        if not self.starts_at < self.ends_at:
            raise ValueError("限制开始时间必须早于结束时间")

    def active_at(self, at: datetime) -> bool:
        aware(at, "判定时间")
        return self.starts_at <= at < self.ends_at

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
            "note": self.note,
        }


class ReceiptResult(str, Enum):
    ACK = "ack"        # 设备确认到位
    NAK = "nak"        # 设备拒绝/故障
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class CommandReceipt:
    """现场设备对控制指令的回执。"""

    command_id: str
    fan_id: str
    at: datetime
    result: ReceiptResult
    running: bool | None = None
    message: str = ""

    def __post_init__(self) -> None:
        aware(self.at, "回执时间")

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "fan_id": self.fan_id,
            "at": self.at.isoformat(),
            "result": self.result.value,
            "running": self.running,
            "message": self.message,
        }

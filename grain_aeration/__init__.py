"""粮库通风控制中枢。

模块划分：

- models         领域数据模型与事件
- psychrometrics 露点等空气物性计算
- tariff         分时电价
- config         仓房资料与安全阈值加载
- sensor_health  漂移传感器隔离与风险区间扩大
- advisor        带依据的控制建议（硬门禁 + 降温/露点/压力/电价综合判断）
- controller     建议 -> 批准 -> 指令 -> 回执 的控制闭环与紧急停机
- persistence    JSONL 事件日志（崩溃恢复的依据）
- audit          时间线回放与能耗/降温对标
"""

from __future__ import annotations

from .config import DomainConfig, SafetyConfig, load_domain
from .controller import (
    AerationController,
    AuthorizationError,
    SafetyViolation,
    StaleDataError,
)
from .models import (
    Action,
    Approval,
    ApprovalDecision,
    Command,
    CommandVerb,
    Evidence,
    FanState,
    FanStatusReceipt,
    Recommendation,
    SensorQuality,
    SensorReading,
    User,
    WeatherSnapshot,
)
from .persistence import JsonlEventStore
from .tariff import TimeOfUseTariff

__version__ = "0.1.0"

__all__ = [
    "Action",
    "AerationController",
    "Approval",
    "ApprovalDecision",
    "AuthorizationError",
    "Command",
    "CommandVerb",
    "DomainConfig",
    "Evidence",
    "FanState",
    "FanStatusReceipt",
    "JsonlEventStore",
    "Recommendation",
    "SafetyConfig",
    "SafetyViolation",
    "SensorQuality",
    "SensorReading",
    "StaleDataError",
    "TimeOfUseTariff",
    "User",
    "WeatherSnapshot",
    "load_domain",
]

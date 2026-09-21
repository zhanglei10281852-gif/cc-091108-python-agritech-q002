"""分时电价（TOU）。

以"峰/平/谷"三段描述一个营业日；``rate_at`` 给出任意时刻的千瓦时单价。
通风优化倾向于把可推迟的降温安排到谷段；但硬性安全（结露、熏蒸等）
永远优先于电价，电价只产生建议级依据。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum

from .models import aware


class TariffTier(str, Enum):
    PEAK = "peak"      # 峰
    FLAT = "flat"      # 平
    VALLEY = "valley"  # 谷


@dataclass(frozen=True)
class TariffSlot:
    tier: TariffTier
    start: time
    end: time
    price_cny_per_kwh: float


@dataclass(frozen=True)
class TariffSchedule:
    slots: tuple[TariffSlot, ...]

    def rate_at(self, at: datetime) -> float:
        aware(at, "电价查询时间")
        t = at.timetz().replace(tzinfo=None)
        for slot in self.slots:
            if slot.start <= t < slot.end:
                return slot.price_cny_per_kwh
        # 跨零点的谷段（如 23:00-07:00）
        for slot in self.slots:
            if slot.start > slot.end and (t >= slot.start or t < slot.end):
                return slot.price_cny_per_kwh
        raise ValueError(f"电价表未覆盖时刻 {t.isoformat()}")

    def tier_at(self, at: datetime) -> TariffTier:
        aware(at, "电价查询时间")
        t = at.timetz().replace(tzinfo=None)
        for slot in self.slots:
            spans_midnight = slot.start > slot.end
            if (slot.start <= t < slot.end) or (
                spans_midnight and (t >= slot.start or t < slot.end)
            ):
                return slot.tier
        raise ValueError(f"电价表未覆盖时刻 {t.isoformat()}")

    def estimate(self, at: datetime, hours: float, kw: float) -> tuple[float, float, TariffTier]:
        """估算给定时刻起 ``hours`` 小时的能耗与电费（短时按当前段近似）。"""
        rate = self.rate_at(at)
        tier = self.tier_at(at)
        kwh = kw * hours
        return kwh, kwh * rate, tier


# 华东常见工商业分时电价（元/kWh），可由配置覆盖
DEFAULT_TARIFF = TariffSchedule(
    slots=(
        TariffSlot(TariffTier.PEAK, time(8, 0), time(11, 0), 1.05),
        TariffSlot(TariffTier.PEAK, time(18, 0), time(21, 0), 1.05),
        TariffSlot(TariffTier.FLAT, time(7, 0), time(8, 0), 0.72),
        TariffSlot(TariffTier.FLAT, time(11, 0), time(18, 0), 0.72),
        TariffSlot(TariffTier.FLAT, time(21, 0), time(23, 0), 0.72),
        TariffSlot(TariffTier.VALLEY, time(23, 0), time(7, 0), 0.38),
    )
)

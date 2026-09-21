"""分时电价。

三段制：峰、平、谷。顾问在收益模型中使用当前时段单价，
并在“可通风但处于峰段”时输出 DEFER_TO_OFF_PEAK。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable


@dataclass(frozen=True)
class TariffBand:
    name: str             # "peak" | "flat" | "valley"
    price_per_kwh: float  # 元/kWh
    start: time           # 含
    end: time            # 不含


class TimeOfUseTariff:
    """按一天中的时间段查价（支持跨午夜的谷段）。"""

    def __init__(self, bands: Iterable[TariffBand]):
        bands = list(bands)
        if not bands:
            raise ValueError("tariff requires at least one band")
        self._bands: list[TariffBand] = bands

    @classmethod
    def default(cls) -> "TimeOfUseTariff":
        """工商业常见三段制示例（本地时间，夜间降温恰好落在谷段）。"""
        return cls([
            TariffBand("valley", 0.32, time(0, 0), time(6, 0)),
            TariffBand("flat", 0.72, time(6, 0), time(8, 0)),
            TariffBand("peak", 1.18, time(8, 0), time(11, 0)),
            TariffBand("flat", 0.72, time(11, 0), time(18, 0)),
            TariffBand("peak", 1.18, time(18, 0), time(21, 0)),
            TariffBand("flat", 0.72, time(21, 0), time(23, 0)),
            TariffBand("valley", 0.32, time(23, 0), time(23, 59, 59, 999999)),
        ])

    def band_at(self, at: datetime) -> TariffBand:
        if at.tzinfo is None:
            raise ValueError("datetime must carry an explicit timezone")
        t = at.timetz().replace(tzinfo=None)
        for band in self._bands:
            if band.start <= t < band.end:
                return band
        # 跨午夜兜底
        return self._bands[0]

    def price_at(self, at: datetime) -> float:
        return self.band_at(at).price_per_kwh

    def is_peak_at(self, at: datetime) -> bool:
        return self.band_at(at).name == "peak"

    def next_band_change(self, at: datetime) -> tuple[datetime, TariffBand]:
        """返回下一次时段切换的时刻与切换后的时段。"""
        day = at.date()
        # 收集今天剩余与明天全天的边界，取最近的未来边界
        candidates: list[tuple[datetime, TariffBand]] = []
        for offset in (0, 1):
            d = day + timedelta(days=offset)
            for band in self._bands:
                boundary = datetime.combine(d, band.start, tzinfo=at.tzinfo)
                if boundary > at:
                    candidates.append((boundary, band))
        candidates.sort(key=lambda x: x[0])
        return candidates[0]

    def cost_for_run(self, rated_kw: float, start: datetime, end: datetime) -> tuple[float, float]:
        """按恒功率运行积分各时段，返回 (用电量 kWh, 电费 元)。

        跨越峰/平/谷边界时，逐时段累计功率在该时段内的小时数。
        """
        if end <= start:
            return 0.0, 0.0
        energy = 0.0
        cost = 0.0
        cursor = start
        while cursor < end:
            change_at, _ = self.next_band_change(cursor)
            seg_end = min(end, change_at)
            hours = (seg_end - cursor).total_seconds() / 3600.0
            seg_energy = rated_kw * hours
            energy += seg_energy
            cost += seg_energy * self.price_at(cursor)
            cursor = seg_end
        return round(energy, 3), round(cost, 3)

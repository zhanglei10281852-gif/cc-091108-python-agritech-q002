"""空气物性计算：露点（Magnus 公式）与结露裕度。"""

from __future__ import annotations

import math

# Magnus 公式常数（适用 0–50 ℃ 水面）
_A = 17.625
_B = 243.12  # ℃


def saturation_vapor_pressure_hpa(t_c: float) -> float:
    """饱和水汽压（Tetens/Magnus 拟合）。"""
    if math.isnan(t_c):
        raise ValueError("temperature is NaN")
    return 6.112 * math.exp((_A * t_c) / (_B + t_c))


def dewpoint_from_rh(t_c: float, rh_pct: float) -> float:
    """由温度与相对湿度反算露点。

    夜间外温下降而空气绝对含湿量大致不变时，外界空气露点变化很小；
    若通入空气露点高于某层粮温，该层就会结露——
    值班员只盯“仓外温度下降”是不够的，必须看露点。
    """
    if not 0 < rh_pct <= 100:
        raise ValueError(f"relative humidity out of range: {rh_pct}")
    gamma = math.log(rh_pct / 100.0) + (_A * t_c) / (_B + t_c)
    return (_B * gamma) / (_A - gamma)


def condensation_margin_c(grain_t_c: float, air_dewpoint_c: float) -> float:
    """结露裕度 = 粮温 − 空气露点。

    返回值 <= 0 表示该粮层必然结露；
    正值但低于安全余量（如 2 ℃）属于高风险，不应贸然通风。
    """
    return grain_t_c - air_dewpoint_c

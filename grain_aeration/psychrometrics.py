"""湿空气计算：饱和水汽压与露点。

采用 Magnus 公式（Tetens 系数，水面）：

    gamma(T, RH) = a*T/(b+T) + ln(RH/100)
    Td = b*gamma / (a - gamma)

粮堆孔隙内空气按同样公式处理其露点（传感器同时给出温度与相对湿度）。
本模块只承担可解释的工程近似，不替代粮情专家判定。
"""

from __future__ import annotations

import math

_MAGNUS_A = 17.62
_MAGNUS_B = 243.12  # °C

# 结露安全裕量：入风露点需比粮堆最低温度低这么多（°C）
DEWPOINT_MARGIN_C = 2.0


def saturation_vapor_pressure(temp_c: float) -> float:
    """饱和水汽压（百帕 hPa）。"""
    return 6.112 * math.exp(_MAGNUS_A * temp_c / (_MAGNUS_B + temp_c))


def dew_point(temp_c: float, humidity_pct: float) -> float:
    """由干球温度与相对湿度求露点温度（°C）。"""
    if not 0.0 < humidity_pct <= 100.0:
        raise ValueError(f"相对湿度越界: {humidity_pct}")
    rh_ratio = humidity_pct / 100.0
    gamma = _MAGNUS_A * temp_c / (_MAGNUS_B + temp_c) + math.log(rh_ratio)
    return _MAGNUS_B * gamma / (_MAGNUS_A - gamma)


def condensation_risk(
    coldest_grain_c: float,
    outside_temp_c: float,
    outside_humidity_pct: float,
    margin_c: float = DEWPOINT_MARGIN_C,
) -> dict:
    """评估把外界空气送入粮堆是否有结露风险。

    判定量是**入风露点相对粮堆最低温度**的裕量：
    入风露点必须 <= 粮堆最低温度 - margin。只看"仓外温度下降了"
    （干球温度）会漏掉高湿度夜间露点逼近粮温的情形——这正是本次投诉的根因。
    """
    outside_dp = dew_point(outside_temp_c, outside_humidity_pct)
    headroom_c = coldest_grain_c - margin_c - outside_dp
    return {
        "outside_dewpoint_c": round(outside_dp, 2),
        "coldest_grain_c": coldest_grain_c,
        "margin_c": margin_c,
        "headroom_c": round(headroom_c, 2),
        "safe": headroom_c >= 0.0,
        "dry_bulb_drop_c": round(coldest_grain_c - outside_temp_c, 2),
    }

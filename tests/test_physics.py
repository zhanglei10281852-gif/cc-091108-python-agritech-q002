"""湿空气与分时电价计算测试。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from grain_aeration.psychrometrics import (
    condensation_risk,
    dew_point,
    saturation_vapor_pressure,
)
from grain_aeration.tariff import DEFAULT_TARIFF, TariffTier

TZ = ZoneInfo("Asia/Shanghai")


class PsychrometricsTest(unittest.TestCase):
    def test_dew_point_known_value(self):
        # 20°C / 50%RH 的露点约 9.27°C（工程常用对照值）
        self.assertAlmostEqual(dew_point(20.0, 50.0), 9.27, delta=0.1)

    def test_saturated_air_dewpoint_equals_drybulb(self):
        self.assertAlmostEqual(dew_point(25.0, 100.0), 25.0, delta=0.05)

    def test_svp_monotonic(self):
        self.assertLess(saturation_vapor_pressure(0.0), saturation_vapor_pressure(30.0))

    def test_condensation_safe_and_unsafe(self):
        safe = condensation_risk(
            coldest_grain_c=16.0, outside_temp_c=10.0, outside_humidity_pct=60.0
        )
        self.assertTrue(safe["safe"])
        self.assertGreater(safe["headroom_c"], 0.0)

        # 本次事故：19.2°C / 91% → 露点约 17.7°C，逼近 15.8°C 冷点
        risky = condensation_risk(
            coldest_grain_c=15.8, outside_temp_c=19.2, outside_humidity_pct=91.0
        )
        self.assertFalse(risky["safe"])
        self.assertLess(risky["headroom_c"], 0.0)
        # 干球温度看着在下降（15.8 vs 19.2 是粮温更高），干球温差不能作为理由
        self.assertIn("dry_bulb_drop_c", risky)

    def test_bad_humidity_rejected(self):
        with self.assertRaises(ValueError):
            dew_point(20.0, 0.0)
        with self.assertRaises(ValueError):
            dew_point(20.0, 120.0)


class TariffTest(unittest.TestCase):
    def test_tiers(self):
        self.assertIs(
            DEFAULT_TARIFF.tier_at(datetime(2026, 9, 11, 22, 0, tzinfo=TZ)),
            TariffTier.FLAT,
        )
        self.assertIs(
            DEFAULT_TARIFF.tier_at(datetime(2026, 9, 11, 23, 30, tzinfo=TZ)),
            TariffTier.VALLEY,
        )
        self.assertIs(
            DEFAULT_TARIFF.tier_at(datetime(2026, 9, 11, 19, 0, tzinfo=TZ)),
            TariffTier.PEAK,
        )
        # 跨零点谷段
        self.assertIs(
            DEFAULT_TARIFF.tier_at(datetime(2026, 9, 12, 2, 0, tzinfo=TZ)),
            TariffTier.VALLEY,
        )

    def test_valley_cheaper_than_peak(self):
        valley = DEFAULT_TARIFF.rate_at(datetime(2026, 9, 12, 3, 0, tzinfo=TZ))
        peak = DEFAULT_TARIFF.rate_at(datetime(2026, 9, 11, 19, 0, tzinfo=TZ))
        self.assertLess(valley, peak)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            DEFAULT_TARIFF.rate_at(datetime(2026, 9, 11, 22, 0))


if __name__ == "__main__":
    unittest.main()

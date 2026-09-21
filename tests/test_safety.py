"""安全否决门测试：熏蒸、雨雪倒灌、风道压力、风机互锁、急停。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from grain_aeration.models import (
    DuctPressure,
    Fan,
    FanRuntimeState,
    Restriction,
    RestrictionKind,
    WeatherPoint,
)
from grain_aeration.safety import SafetyContext, evaluate

TZ = ZoneInfo("Asia/Shanghai")
FANS = [
    Fan("F-1", "DUCT-N", 18.5, "DUCT-N"),
    Fan("F-2", "DUCT-N", 18.5, "DUCT-N"),
]


def ctx(at=None, weather=None, pressures=None, restrictions=None, states=None, emergency=False):
    return SafetyContext(
        at=at or datetime(2026, 9, 11, 22, 0, tzinfo=TZ),
        weather=weather,
        pressures=pressures or {},
        restrictions=restrictions or [],
        fan_states=states or {},
        emergency_stopped=emergency,
    )


class SafetyTest(unittest.TestCase):
    def test_fumigation_blocks(self):
        r = Restriction(
            RestrictionKind.FUMIGATION,
            datetime(2026, 9, 11, 18, 0, tzinfo=TZ),
            datetime(2026, 9, 12, 6, 0, tzinfo=TZ),
        )
        vetoes, blocked = evaluate(ctx(restrictions=[r]), FANS)
        self.assertTrue(any(v.code == "fumigation" for v in vetoes))
        self.assertEqual(blocked, {"F-1", "F-2"})

    def test_rain_blocks(self):
        w = WeatherPoint(datetime(2026, 9, 11, 22, 0, tzinfo=TZ), 19.2, 91, rain=True)
        vetoes, blocked = evaluate(ctx(weather=w), FANS)
        self.assertTrue(any(v.code == "rain_backflow" for v in vetoes))
        self.assertEqual(blocked, {"F-1", "F-2"})

    def test_missing_weather_blocks_conservatively(self):
        vetoes, blocked = evaluate(ctx(weather=None), FANS)
        self.assertTrue(any(v.code == "weather_unknown" for v in vetoes))

    def test_duct_pressure_offrange_blocks(self):
        w = WeatherPoint(datetime(2026, 9, 11, 22, 0, tzinfo=TZ), 12.0, 50, rain=False)
        p = {"DUCT-N": DuctPressure("DUCT-N", datetime(2026, 9, 11, 22, 0, tzinfo=TZ),
                                    -1480.0, (-1200.0, -100.0))}
        vetoes, blocked = evaluate(ctx(weather=w, pressures=p), FANS)
        self.assertTrue(any(v.code == "duct_pressure_offrange" for v in vetoes))
        self.assertEqual(blocked, {"F-1", "F-2"})

    def test_missing_pressure_blocks(self):
        w = WeatherPoint(datetime(2026, 9, 11, 22, 0, tzinfo=TZ), 12.0, 50, rain=False)
        vetoes, blocked = evaluate(ctx(weather=w, pressures={}), FANS)
        self.assertTrue(any(v.code == "duct_pressure_unknown" for v in vetoes))

    def test_interlock_blocks_neighbor_only(self):
        w = WeatherPoint(datetime(2026, 9, 11, 22, 0, tzinfo=TZ), 12.0, 50, rain=False)
        p = {"DUCT-N": DuctPressure("DUCT-N", datetime(2026, 9, 11, 22, 0, tzinfo=TZ),
                                    -600.0, (-1200.0, -100.0))}
        states = {"F-1": FanRuntimeState.RUNNING, "F-2": FanRuntimeState.STOPPED}
        vetoes, blocked = evaluate(ctx(weather=w, pressures=p, states=states), FANS)
        self.assertTrue(any(v.code == "fan_interlock" for v in vetoes))
        # F-1 正在运行，F-2 被互锁挡住；F-1 自身不在 blocked
        self.assertEqual(blocked, {"F-2"})

    def test_pending_verification_counts_as_busy(self):
        w = WeatherPoint(datetime(2026, 9, 11, 22, 0, tzinfo=TZ), 12.0, 50, rain=False)
        p = {"DUCT-N": DuctPressure("DUCT-N", datetime(2026, 9, 11, 22, 0, tzinfo=TZ),
                                    -600.0, (-1200.0, -100.0))}
        states = {"F-1": FanRuntimeState.PENDING_VERIFICATION}
        vetoes, blocked = evaluate(ctx(weather=w, pressures=p, states=states), FANS)
        self.assertIn("F-2", blocked)

    def test_emergency_stop_blocks_all(self):
        w = WeatherPoint(datetime(2026, 9, 11, 22, 0, tzinfo=TZ), 12.0, 50, rain=False)
        p = {"DUCT-N": DuctPressure("DUCT-N", datetime(2026, 9, 11, 22, 0, tzinfo=TZ),
                                    -600.0, (-1200.0, -100.0))}
        vetoes, blocked = evaluate(ctx(weather=w, pressures=p, emergency=True), FANS)
        self.assertTrue(any(v.code == "emergency_stop" for v in vetoes))
        self.assertEqual(blocked, {"F-1", "F-2"})

    def test_clean_conditions_pass(self):
        w = WeatherPoint(datetime(2026, 9, 11, 22, 0, tzinfo=TZ), 8.0, 55, rain=False)
        p = {"DUCT-N": DuctPressure("DUCT-N", datetime(2026, 9, 11, 22, 0, tzinfo=TZ),
                                    -600.0, (-1200.0, -100.0))}
        vetoes, blocked = evaluate(ctx(weather=w, pressures=p), FANS)
        hard = [v for v in vetoes if v.severity.value == "block"]
        self.assertEqual(hard, [])
        self.assertEqual(blocked, set())


if __name__ == "__main__":
    unittest.main()

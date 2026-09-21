"""传感器漂移隔离与风险区间扩大测试。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from grain_aeration.models import (
    GrainLayer,
    Sensor,
    SensorQuality,
    SensorReading,
)
from grain_aeration.sensors import assess_sensors, cold_layer_view

TZ = ZoneInfo("Asia/Shanghai")
AT = datetime(2026, 9, 11, 22, 0, tzinfo=TZ)


def make_sensors():
    return [
        Sensor("T-TOP-1", "C-1", 0.5, GrainLayer.TOP, 0),
        Sensor("T-MID-1", "C-1", 2.5, GrainLayer.MIDDLE, 1),
        Sensor("T-BOT-1", "C-1", 5.0, GrainLayer.BOTTOM, 2),
        Sensor("T-TOP-2", "C-2", 0.5, GrainLayer.TOP, 0),
        Sensor("T-MID-2", "C-2", 2.5, GrainLayer.MIDDLE, 1),
        Sensor("T-BOT-2", "C-2", 5.0, GrainLayer.BOTTOM, 2),
    ]


def r(sid, t, q=SensorQuality.GOOD, h=70.0):
    return SensorReading(sid, AT, t, h, q)


class SensorAssessmentTest(unittest.TestCase):
    def setUp(self):
        self.sensors = make_sensors()
        self.readings = {
            "T-TOP-1": r("T-TOP-1", 24.8),
            "T-MID-1": r("T-MID-1", 29.1, SensorQuality.DRIFTING, 66),
            "T-BOT-1": r("T-BOT-1", 16.2),
            "T-TOP-2": r("T-TOP-2", 25.3),
            "T-MID-2": r("T-MID-2", 28.6),
            "T-BOT-2": r("T-BOT-2", 15.8),
        }

    def test_drifting_sensor_is_isolated(self):
        a = assess_sensors(self.sensors, self.readings, AT)
        self.assertIn("T-MID-1", a.isolated)
        trusted_ids = {x.sensor_id for x in a.trusted}
        self.assertNotIn("T-MID-1", trusted_ids)
        self.assertIn("传感器漂移", a.isolation_reasons["T-MID-1"])

    def test_risk_zone_expands_along_cable_and_layer(self):
        a = assess_sensors(self.sensors, self.readings, AT)
        # 同电缆上下相邻点
        self.assertIn("T-TOP-1", a.expanded_zone)
        self.assertIn("T-BOT-1", a.expanded_zone)
        # 跨电缆同层最近点
        self.assertIn("T-MID-2", a.expanded_zone)
        # 隔离点自身不在扩大区间
        self.assertNotIn("T-MID-1", a.expanded_zone)

    def test_coldest_excludes_isolated(self):
        a = assess_sensors(self.sensors, self.readings, AT)
        # 漂移点 29.1 不影响最冷点；最冷是 T-BOT-2 15.8
        self.assertEqual(a.coldest.sensor_id, "T-BOT-2")

    def test_expanded_zone_gets_extra_margin(self):
        a = assess_sensors(self.sensors, self.readings, AT)
        self.assertGreater(a.extra_margin_c("T-BOT-1"), 0.0)
        self.assertEqual(a.extra_margin_c("T-BOT-2"), 0.0)

    def test_manual_isolation_by_admin(self):
        a = assess_sensors(
            self.sensors, self.readings, AT,
            manual_isolation={"T-BOT-2": "现场校准时读数跳变"},
        )
        self.assertIn("T-BOT-2", a.isolated)
        self.assertIn("T-BOT-2", a.manually_isolated)

    def test_all_isolated_blocks_decision(self):
        for sid in list(self.readings):
            self.readings[sid] = r(sid, 20.0, SensorQuality.INVALID)
        a = assess_sensors(self.sensors, self.readings, AT)
        self.assertEqual(a.trusted, [])

    def test_layer_view(self):
        a = assess_sensors(self.sensors, self.readings, AT)
        view = cold_layer_view(self.sensors, a)
        # 中层漂移点被隔离后，仍由同层可信点 T-MID-2 代表
        self.assertIn(GrainLayer.MIDDLE, view)
        self.assertEqual(view[GrainLayer.BOTTOM]["min_c"], 15.8)


if __name__ == "__main__":
    unittest.main()

"""控制中枢集成测试：建议-批准分离、执行前二次安全校验、急停、
设备回执状态机、周期复盘与重启待核实恢复。
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from grain_aeration.advisor import Advisor
from grain_aeration.controller import (
    AerationController,
    AuthorizationError,
    ControlRejected,
)
from grain_aeration.fans import (
    FanRuntimeState,
    SimulatedGateway,
)
from grain_aeration.models import (
    Actor,
    DuctPressure,
    Fan,
    GrainLayer,
    ReceiptResult,
    Restriction,
    RestrictionKind,
    Role,
    Sensor,
    SensorQuality,
    SensorReading,
    WeatherPoint,
)
from grain_aeration.store import EventStore

TZ = ZoneInfo("Asia/Shanghai")

OPERATOR = Actor("U1", "值班员", Role.OPERATOR)
VIEWER = Actor("U2", "实习生", Role.VIEWER)
MANAGER = Actor("U3", "质量经理", Role.QUALITY_MANAGER)
ADMIN = Actor("U0", "管理员", Role.ADMIN)


def sensors():
    return [
        Sensor("T-TOP-1", "C-1", 0.5, GrainLayer.TOP, 0),
        Sensor("T-MID-1", "C-1", 2.5, GrainLayer.MIDDLE, 1),
        Sensor("T-BOT-1", "C-1", 5.0, GrainLayer.BOTTOM, 2),
    ]


def fans():
    return [
        Fan("F-1", "DUCT-N", 10.0, "DUCT-N"),
        Fan("F-2", "DUCT-N", 10.0, "DUCT-N"),
    ]


def good_pressure(at):
    return {"DUCT-N": DuctPressure("DUCT-N", at, -600.0, (-1200.0, -100.0))}


def readings(at, mid_quality=SensorQuality.GOOD):
    return {
        "T-TOP-1": SensorReading("T-TOP-1", at, 23.0, 70, SensorQuality.GOOD),
        "T-MID-1": SensorReading("T-MID-1", at, 27.0, 65, mid_quality),
        "T-BOT-1": SensorReading("T-BOT-1", at, 15.0, 72, SensorQuality.GOOD),
    }


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "audit.jsonl"
        self.ctrl = AerationController(
            sensors=sensors(),
            fans=fans(),
            restrictions=[],
            gateway=SimulatedGateway(),
            store=EventStore(self.store_path),
            advisor=Advisor(),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def tick_start(self, at, weather=None):
        weather = weather or WeatherPoint(at, 9.0, 55, rain=False)
        return self.ctrl.tick(at, readings(at), weather, good_pressure(at))

    def test_incident_night_is_defer_with_vetoes(self):
        # 事故条件：熏蒸 + 降雨 + 高湿 + 异常风道压力
        at = datetime(2026, 9, 11, 22, 0, tzinfo=TZ)
        self.ctrl.restrictions = [
            Restriction(RestrictionKind.FUMIGATION,
                        at - timedelta(hours=4), at + timedelta(hours=8)),
        ]
        weather = WeatherPoint(at, 19.2, 91, rain=True)
        pressures = {"DUCT-N": DuctPressure("DUCT-N", at, -1480.0, (-1200.0, -100.0))}
        result = self.ctrl.tick(at, readings(at, SensorQuality.DRIFTING),
                                weather, pressures)
        rec = result.recommendation
        self.assertIs(rec.action.value, "defer")
        codes = {v.code for v in rec.vetoes}
        self.assertIn("fumigation", codes)
        self.assertIn("rain_backflow", codes)
        self.assertIn("dewpoint_unsafe", codes)
        self.assertIn("duct_pressure_offrange", codes)
        # 漂移点隔离与风险扩大
        self.assertIn("T-MID-1", result.assessment.isolated)
        self.assertTrue(result.assessment.expanded_zone)

    def test_viewer_cannot_approve(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        rec = self.tick_start(at).recommendation
        self.assertEqual(rec.action.value, "start")
        with self.assertRaises(AuthorizationError):
            self.ctrl.approve(rec.id, VIEWER, at)

    def test_full_start_approve_execute_receipts_and_cycle(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)  # 谷段
        rec = self.tick_start(at).recommendation
        # 互锁组两台风机，建议只选一台
        self.assertEqual(rec.fan_ids, ["F-1"])
        self.ctrl.approve(rec.id, OPERATOR, at + timedelta(minutes=1))
        receipts = self.ctrl.execute(rec.id, OPERATOR, at + timedelta(minutes=2))
        self.assertEqual(len(receipts), 1)
        self.assertIs(receipts[0].result, ReceiptResult.ACK)
        self.assertTrue(receipts[0].running)
        self.assertIs(self.ctrl.bank.state_of("F-1"), FanRuntimeState.RUNNING)

        # 运行 4 小时后收尾
        end = at + timedelta(hours=4)
        end_readings = {
            sid: SensorReading(sid, end, t.temperature_c - 2.0, t.humidity_pct,
                               t.quality)
            for sid, t in readings(at).items()
        }
        self.ctrl.bank.runtimes["F-1"].started_at = at + timedelta(minutes=2)
        report = self.ctrl.close_cycle(
            rec.id, end, end_readings, WeatherPoint(end, 9.0, 55)
        )
        self.assertAlmostEqual(report.actual_kwh, 39.7, delta=0.5)  # 10kW * ~3.97h
        self.assertAlmostEqual(report.actual_delta_c, 2.0, delta=0.01)
        self.assertIsNotNone(report.deviation["kwh_per_degree"])

    def test_second_approval_after_expiry_rejected(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        rec = self.tick_start(at).recommendation
        with self.assertRaises(ControlRejected):
            self.ctrl.approve(rec.id, OPERATOR, at + timedelta(minutes=20))
        self.assertEqual(rec.status.value, "expired")

    def test_second_safety_check_blocks_if_rain_starts_after_approval(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        rec = self.tick_start(at).recommendation
        self.ctrl.approve(rec.id, OPERATOR, at + timedelta(minutes=1))
        # 批准后、下发前开始下雨
        self.ctrl._latest_weather = WeatherPoint(
            at + timedelta(minutes=2), 12.0, 95, rain=True
        )
        with self.assertRaises(ControlRejected):
            self.ctrl.execute(rec.id, OPERATOR, at + timedelta(minutes=2))
        self.assertIs(self.ctrl.bank.state_of("F-1"), FanRuntimeState.STOPPED)

    def test_nak_fault_state(self):
        self.ctrl.gateway = SimulatedGateway(fail_fans={"F-1"})
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        rec = self.tick_start(at).recommendation
        self.ctrl.approve(rec.id, OPERATOR, at)
        receipts = self.ctrl.execute(rec.id, OPERATOR, at)
        self.assertIs(receipts[0].result, ReceiptResult.NAK)
        self.assertIs(self.ctrl.bank.state_of("F-1"), FanRuntimeState.FAULT)
        # 故障占用互锁组，下一周期 F-2 也不可启动
        result = self.tick_start(at + timedelta(minutes=5))
        self.assertEqual(result.recommendation.fan_ids, [])
        self.assertTrue(
            any(v.code == "fan_interlock" for v in result.recommendation.vetoes)
        )

    def test_interlock_prevents_both_fans(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        self.tick_start(at)
        self.ctrl.bank.runtimes["F-1"].state = FanRuntimeState.RUNNING
        result = self.tick_start(at + timedelta(minutes=5))
        # F-1 在运行，建议只能维持，不能再启动 F-2
        self.assertEqual(result.recommendation.fan_ids, [])

    def test_emergency_stop_immediate_and_locks(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        rec = self.tick_start(at).recommendation
        self.ctrl.approve(rec.id, OPERATOR, at)
        self.ctrl.execute(rec.id, OPERATOR, at)
        self.assertIs(self.ctrl.bank.state_of("F-1"), FanRuntimeState.RUNNING)

        stop_at = at + timedelta(minutes=20)
        receipts = self.ctrl.emergency_stop(MANAGER, stop_at)
        self.assertEqual(len(receipts), 1)
        self.assertFalse(receipts[0].running)
        self.assertIs(self.ctrl.bank.state_of("F-1"), FanRuntimeState.STOPPED)

        # 锁定后任何启动建议/批准都被拒
        later = self.tick_start(stop_at + timedelta(minutes=1)).recommendation
        self.assertEqual(later.action.value, "defer")
        # 手工把动作变成可批准也不行：直接验证 emergency 标志
        self.assertTrue(self.ctrl.emergency_stopped)

    def test_restart_marks_running_fan_pending_not_safe_off(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        rec = self.tick_start(at).recommendation
        self.ctrl.approve(rec.id, OPERATOR, at)
        self.ctrl.execute(rec.id, OPERATOR, at)
        # 进程在这里崩溃：最后回执是 running=True，没有停机回执

        # 新进程，同一事件日志
        ctrl2 = AerationController(
            sensors=sensors(),
            fans=fans(),
            gateway=SimulatedGateway(),
            store=EventStore(self.store_path),
            advisor=Advisor(),
        )
        pending = ctrl2.recover(datetime(2026, 9, 13, 8, 0, tzinfo=TZ))
        self.assertEqual(pending, ["F-1"])
        self.assertIs(ctrl2.bank.state_of("F-1"), FanRuntimeState.PENDING_VERIFICATION)
        # F-2 从未被操作，保持初始停机，不需要待核实
        self.assertIs(ctrl2.bank.state_of("F-2"), FanRuntimeState.STOPPED)
        # 急停/熏蒸等标志也被重放
        self.assertFalse(ctrl2.emergency_stopped)

    def test_restart_after_clean_stop_keeps_stopped(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        rec = self.tick_start(at).recommendation
        self.ctrl.approve(rec.id, OPERATOR, at)
        self.ctrl.execute(rec.id, OPERATOR, at)
        # 正常停机建议并执行
        stop_at = at + timedelta(hours=2)
        weather_rain = WeatherPoint(stop_at, 12.0, 96, rain=True)
        stop_rec = self.ctrl.tick(
            stop_at, readings(stop_at), weather_rain, good_pressure(stop_at)
        ).recommendation
        self.assertEqual(stop_rec.action.value, "stop")
        self.ctrl.approve(stop_rec.id, OPERATOR, stop_at)
        receipts = self.ctrl.execute(stop_rec.id, OPERATOR, stop_at)
        self.assertFalse(receipts[0].running)

        ctrl2 = AerationController(
            sensors=sensors(),
            fans=fans(),
            gateway=SimulatedGateway(),
            store=EventStore(self.store_path),
            advisor=Advisor(),
        )
        pending = ctrl2.recover(stop_at + timedelta(minutes=1))
        self.assertEqual(pending, [])
        self.assertIs(ctrl2.bank.state_of("F-1"), FanRuntimeState.STOPPED)

    def test_pending_resolution(self):
        at = datetime(2026, 9, 13, 8, 0, tzinfo=TZ)
        self.ctrl.bank.mark_pending_verification(at, {"F-1"}, force=True)
        state = self.ctrl.verify_pending(OPERATOR, "F-1", False,
                                         at + timedelta(minutes=2))
        self.assertIs(state, FanRuntimeState.STOPPED)

    def test_timeline_ordering(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        rec = self.tick_start(at).recommendation
        self.ctrl.approve(rec.id, OPERATOR, at)
        self.ctrl.execute(rec.id, OPERATOR, at)
        entries = self.ctrl.timeline(rec.id)
        types = [e.event_type for e in entries]
        self.assertLess(
            types.index("recommendation_proposed"),
            types.index("recommendation_approved"),
        )
        self.assertLess(
            types.index("recommendation_approved"),
            types.index("command_issued"),
        )
        self.assertLess(
            types.index("command_issued"),
            types.index("receipt_received"),
        )

    def test_running_fan_gets_stop_recommendation_when_rain_begins(self):
        at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
        rec = self.tick_start(at).recommendation
        self.ctrl.approve(rec.id, OPERATOR, at)
        self.ctrl.execute(rec.id, OPERATOR, at)
        # 运行中开始下雨
        rainy = WeatherPoint(at + timedelta(hours=1), 12.0, 96, rain=True)
        result = self.ctrl.tick(
            at + timedelta(hours=1), readings(at + timedelta(hours=1)),
            rainy, good_pressure(at + timedelta(hours=1))
        )
        self.assertEqual(result.recommendation.action.value, "stop")
        self.assertEqual(result.recommendation.fan_ids, ["F-1"])


if __name__ == "__main__":
    unittest.main()

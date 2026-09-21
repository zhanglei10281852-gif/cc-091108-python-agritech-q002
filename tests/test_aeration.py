"""通风控制中枢的安全语义测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from grain_aeration import (
    AerationController,
    ApprovalDecision,
    JsonlEventStore,
    SensorQuality,
    SensorReading,
    User,
    WeatherSnapshot,
    load_domain,
)
from grain_aeration.advisor import Advisor, AdvisorContext
from grain_aeration.audit import benchmark, build_timeline
from grain_aeration.config import SafetyConfig
from grain_aeration.hardware import SimulatedFanGateway
from grain_aeration.models import (
    Action,
    Command,
    CommandVerb,
    DuctPressure,
    FanState,
    FanStatusReceipt,
)
from grain_aeration.persistence import recover_state
from grain_aeration.psychrometrics import condensation_margin_c, dewpoint_from_rh
from grain_aeration.sensor_health import assess_sensor_health
from grain_aeration.tariff import TimeOfUseTariff, TariffBand

REFERENCE = Path(__file__).parents[1] / "reference" / "domain.json"
TZ = datetime.fromisoformat("2026-09-11T22:00:00+08:00").tzinfo


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def make_controller(path: Path, clock: Clock, gateway=None):
    domain = load_domain(REFERENCE)
    gw = gateway or SimulatedFanGateway([f.id for f in domain.fans], clock=clock)
    ctrl = AerationController(domain, JsonlEventStore(path), gw, clock=clock)
    return domain, gw, ctrl


def readings(at: datetime, top: float = 24.8,
             mid_q: SensorQuality = SensorQuality.DRIFTING) -> list[SensorReading]:
    return [
        SensorReading("T-TOP-1", 0.5, at, top, 70, SensorQuality.GOOD),
        SensorReading("T-MID-1", 2.5, at, 99.9, 66, mid_q),
    ]


def pressure(at: datetime, pa: float = 350.0, ok: bool = True) -> list[DuctPressure]:
    return [DuctPressure("DUCT-N", at, pa, ok)]


class PsychrometricsTest(unittest.TestCase):
    def test_dewpoint_belows_air_temp_and_margin(self):
        dp = dewpoint_from_rh(20.0, 60)
        self.assertAlmostEqual(dp, 12.0, delta=0.5)
        self.assertLess(dp, 20.0)
        self.assertAlmostEqual(condensation_margin_c(24.8, dp), 24.8 - dp)

    def test_dewpoint_rejects_bad_humidity(self):
        with self.assertRaises(ValueError):
            dewpoint_from_rh(20, 0)


class SensorHealthTest(unittest.TestCase):
    def test_drifting_point_isolated_and_band_expands_to_boundaries(self):
        at = datetime(2026, 9, 11, 22, tzinfo=TZ)
        h = assess_sensor_health(readings(at), pile_depth_m=5.5)
        self.assertEqual(h.isolated_ids, ("T-MID-1",))
        # 漂移点是最深的可信参考之外的点：下扩到仓底，上扩到上方相邻电缆
        self.assertEqual(h.overall_risk_band, (0.5, 5.5))
        # 冷端只取可信点
        self.assertEqual(h.conservative_cold_end_c(), 24.8)

    def test_band_expands_half_gap_with_cables_on_both_sides(self):
        at = datetime(2026, 9, 11, 22, tzinfo=TZ)
        data = [
            SensorReading("A", 0.5, at, 25, 70, SensorQuality.GOOD),
            SensorReading("B", 2.5, at, 99, 66, SensorQuality.DRIFTING),
            SensorReading("C", 4.5, at, 26, 60, SensorQuality.GOOD),
        ]
        h = assess_sensor_health(data, pile_depth_m=5.5)
        self.assertEqual(h.risk_bands, ((0.5, 4.5),))


class AdvisorTest(unittest.TestCase):
    def setUp(self):
        self.domain = load_domain(REFERENCE)
        self.clock = Clock(datetime(2026, 9, 11, 22, tzinfo=TZ))
        self.advisor = Advisor(self.domain)

    def _ctx(self, weather, running=frozenset(), at=None):
        at = at or self.clock.t
        return AdvisorContext(
            at=at,
            readings=tuple(readings(at)),
            weather=weather,
            pressures=tuple(pressure(at)),
            running_fan_ids=running,
        )

    def test_complaint_night_is_forbidden(self):
        rec = self.advisor.evaluate(
            self._ctx(WeatherSnapshot(self.clock.t, 19.2, 91, rain=True))
        )
        self.assertIs(rec.action, Action.FORBIDDEN)
        self.assertTrue(rec.blocked)
        codes = {e.code for e in rec.reasons}
        self.assertIn("RESTRICTION_FUMIGATION", codes)
        self.assertIn("WEATHER_RAIN_INGRESS", codes)

    def test_rain_alone_below_humidity_threshold_is_not_blocked(self):
        # 雨后低湿时段（熏蒸已结束）
        at = datetime(2026, 9, 12, 23, tzinfo=TZ)
        ctx = AdvisorContext(
            at=at, readings=tuple(readings(at)),
            weather=WeatherSnapshot(at, 17, 55, rain=True),
            pressures=tuple(pressure(at)),
        )
        rec = self.advisor.evaluate(ctx)
        self.assertFalse(any(e.blocks_start for e in rec.reasons))

    def test_dry_cold_valley_night_recommends_start_single_fan(self):
        at = datetime(2026, 9, 12, 23, tzinfo=TZ)
        ctx = AdvisorContext(
            at=at, readings=tuple(readings(at)),
            weather=WeatherSnapshot(at, 17.0, 65, rain=False),
            pressures=tuple(pressure(at)),
        )
        rec = self.advisor.evaluate(ctx)
        self.assertIs(rec.action, Action.START)
        starts = [p.fan_id for p in rec.fan_plans if p.start]
        # 同风道互锁：两台风机只允许启动一台
        self.assertEqual(starts, ["F-1"])
        self.assertGreater(rec.expected_energy_kwh, 0)

    def test_interlock_blocks_second_fan_when_one_running(self):
        at = datetime(2026, 9, 12, 23, tzinfo=TZ)
        ctx = AdvisorContext(
            at=at, readings=tuple(readings(at)),
            weather=WeatherSnapshot(at, 17.0, 65, rain=False),
            pressures=tuple(pressure(at)),
            running_fan_ids=frozenset({"F-1"}),
        )
        rec = self.advisor.evaluate(ctx)
        # 容量已满：不给出任何启动计划
        self.assertEqual([p for p in rec.fan_plans if p.start], [])

    def test_missing_pressure_sample_blocks(self):
        at = datetime(2026, 9, 12, 23, tzinfo=TZ)
        ctx = AdvisorContext(
            at=at, readings=tuple(readings(at)),
            weather=WeatherSnapshot(at, 17.0, 65, rain=False),
            pressures=(),
        )
        rec = self.advisor.evaluate(ctx)
        self.assertIs(rec.action, Action.FORBIDDEN)
        self.assertIn("DUCT_PRESSURE_MISSING", {e.code for e in rec.reasons})

    def test_pressure_out_of_range_blocks(self):
        at = datetime(2026, 9, 12, 23, tzinfo=TZ)
        ctx = AdvisorContext(
            at=at, readings=tuple(readings(at)),
            weather=WeatherSnapshot(at, 17.0, 65, rain=False),
            pressures=(DuctPressure("DUCT-N", at, 1500.0, True),),
        )
        rec = self.advisor.evaluate(ctx)
        self.assertTrue(rec.blocked)

    def test_dewpoint_reversal_while_running_recommends_stop(self):
        # 极潮湿空气：露点高于粮温
        at = datetime(2026, 9, 12, 23, tzinfo=TZ)
        ctx = AdvisorContext(
            at=at, readings=tuple(readings(at, top=20.0)),
            weather=WeatherSnapshot(at, 18.0, 99, rain=False),
            pressures=tuple(pressure(at)),
            running_fan_ids=frozenset({"F-1"}),
        )
        rec = self.advisor.evaluate(ctx)
        self.assertIs(rec.action, Action.STOP)
        self.assertTrue(any(p.fan_id == "F-1" and not p.start for p in rec.fan_plans))

    def test_insufficient_cooling_delta_holds(self):
        at = datetime(2026, 9, 12, 23, tzinfo=TZ)
        ctx = AdvisorContext(
            at=at, readings=tuple(readings(at, top=19.0)),
            weather=WeatherSnapshot(at, 18.0, 60, rain=False),
            pressures=tuple(pressure(at)),
        )
        rec = self.advisor.evaluate(ctx)
        self.assertIs(rec.action, Action.HOLD)

    def test_peak_tariff_defers(self):
        at = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)  # 峰段
        ctx = AdvisorContext(
            at=at, readings=tuple(readings(at)),
            weather=WeatherSnapshot(at, 14.0, 55, rain=False),
            pressures=tuple(pressure(at)),
        )
        rec = self.advisor.evaluate(ctx)
        self.assertIs(rec.action, Action.DEFER_TO_OFF_PEAK)

    def test_all_sensors_isolated_blocks(self):
        at = datetime(2026, 9, 12, 23, tzinfo=TZ)
        ctx = AdvisorContext(
            at=at,
            readings=(
                SensorReading("T-TOP-1", 0.5, at, 24, 70, SensorQuality.BAD),
                SensorReading("T-MID-1", 2.5, at, 24, 70, SensorQuality.DRIFTING),
            ),
            weather=WeatherSnapshot(at, 17.0, 65, rain=False),
            pressures=tuple(pressure(at)),
        )
        rec = self.advisor.evaluate(ctx)
        self.assertIs(rec.action, Action.FORBIDDEN)


class TariffTest(unittest.TestCase):
    def test_cost_for_run_spans_bands(self):
        tz = TZ
        tariff = TimeOfUseTariff.default()
        start = datetime(2026, 9, 12, 22, 0, tzinfo=tz)   # 22:00–23:00 flat 0.72
        end = datetime(2026, 9, 13, 2, 0, tzinfo=tz)      # 23:00 后 valley 0.32
        energy, cost = tariff.cost_for_run(10.0, start, end)
        self.assertAlmostEqual(energy, 40.0)
        self.assertAlmostEqual(cost, 10 * 1 * 0.72 + 10 * 3 * 0.32, places=2)

    def test_custom_band_lookup(self):
        tariff = TimeOfUseTariff([
            TariffBand("x", 0.9, datetime.strptime("00:00", "%H:%M").time(),
                       datetime.strptime("23:59:59.999999", "%H:%M:%S.%f").time()),
        ])
        at = datetime(2026, 9, 12, 12, tzinfo=TZ)
        self.assertEqual(tariff.price_at(at), 0.9)


class ControllerFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 9, 12, 23, tzinfo=TZ))
        self.domain, self.gw, self.ctrl = make_controller(
            Path(self.tmp.name) / "events.jsonl", self.clock
        )
        self.manager = User("zhao", "quality_manager")
        self.operator = User("sun", "operator")
        self.weather = WeatherSnapshot(self.clock.t, 17.0, 65, rain=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_operator_cannot_approve_but_can_emergency_stop(self):
        rec = self.ctrl.advise(readings(self.clock.t), self.weather, pressure(self.clock.t))
        with self.assertRaises(PermissionError):
            self.ctrl.approve(rec, self.operator, ApprovalDecision.APPROVE)
        # 但任何值班员都能紧急停机
        receipts = self.ctrl.emergency_stop(self.operator, reason="测试")
        self.assertEqual(receipts, [])  # 本就无风机运行

    def test_hard_gate_cannot_be_overridden(self):
        self.clock.t = datetime(2026, 9, 11, 22, tzinfo=TZ)
        rec = self.ctrl.advise(
            readings(self.clock.t),
            WeatherSnapshot(self.clock.t, 19.2, 91, rain=True),
            pressure(self.clock.t),
        )
        self.assertIs(rec.action, Action.FORBIDDEN)
        for decision in (ApprovalDecision.APPROVE, ApprovalDecision.FORCE_OVERRIDE):
            with self.assertRaises(Exception):
                self.ctrl.approve(rec, User("boss", "admin"), decision)
        self.assertFalse(self.gw.is_running("F-1"))

    def test_stale_recommendation_rejected(self):
        rec = self.ctrl.advise(readings(self.clock.t), self.weather, pressure(self.clock.t))
        self.clock.t += timedelta(seconds=rec.valid_for_seconds + 1)
        with self.assertRaises(Exception):
            self.ctrl.approve(rec, self.manager, ApprovalDecision.APPROVE)

    def test_full_start_stop_and_benchmark(self):
        rec = self.ctrl.advise(readings(self.clock.t), self.weather, pressure(self.clock.t))
        self.ctrl.approve(rec, self.manager, ApprovalDecision.APPROVE)
        self.assertIs(self.ctrl.fan_state("F-1"), FanState.RUNNING)

        self.clock.t += timedelta(hours=4)
        warmer = WeatherSnapshot(self.clock.t, 23.5, 70, rain=False)
        stop_rec = self.ctrl.advise(
            readings(self.clock.t, top=22.3), warmer, pressure(self.clock.t)
        )
        self.assertIs(stop_rec.action, Action.STOP)
        self.ctrl.approve(stop_rec, self.manager, ApprovalDecision.APPROVE)
        self.assertIs(self.ctrl.fan_state("F-1"), FanState.STOPPED)

        summary = self.ctrl.close_session(rec.rec_id, 24.8, 22.3)
        self.assertAlmostEqual(summary.actual_duration_h, 4.0)
        self.assertAlmostEqual(summary.actual_energy_kwh, 18.5 * 4)
        self.assertAlmostEqual(summary.actual_temp_drop_c, 2.5)

        rows = {r.rec_id: r for r in benchmark(self.ctrl.store)}
        self.assertIn(rec.rec_id, rows)
        self.assertAlmostEqual(rows[rec.rec_id].energy_deviation_pct, 0.0, places=1)

        # 时间线包含闭合本次运行的停机指令
        entries = build_timeline(self.ctrl.store, rec.rec_id)
        titles = " ".join(e.title for e in entries)
        self.assertIn("建议 START", titles)
        self.assertIn("STOP_FAN", titles)

    def test_post_approval_recheck_blocks_when_weather_turns(self):
        rec = self.ctrl.advise(readings(self.clock.t), self.weather, pressure(self.clock.t))
        # 批准时突降暴雨
        rainy = WeatherSnapshot(self.clock.t, 17.0, 95, rain=True)
        with self.assertRaises(Exception):
            self.ctrl.approve(
                rec, self.manager, ApprovalDecision.APPROVE,
                readings=readings(self.clock.t), weather=rainy,
                pressures=pressure(self.clock.t),
            )
        self.assertFalse(self.gw.is_running("F-1"))

    def test_emergency_stop_does_not_wait_and_is_logged(self):
        rec = self.ctrl.advise(readings(self.clock.t), self.weather, pressure(self.clock.t))
        self.ctrl.approve(rec, self.manager, ApprovalDecision.APPROVE)
        self.assertTrue(self.gw.is_running("F-1"))
        receipts = self.ctrl.emergency_stop(self.operator, reason="风道异响")
        self.assertEqual(len(receipts), 1)
        self.assertFalse(self.gw.is_running("F-1"))
        kinds = [e.kind for e in self.ctrl.store.read_all()]
        self.assertIn("emergency_stop", kinds)

    def test_unverified_fan_cannot_be_started_without_acknowledgement(self):
        # 先正常启动
        rec = self.ctrl.advise(readings(self.clock.t), self.weather, pressure(self.clock.t))
        self.ctrl.approve(rec, self.manager, ApprovalDecision.APPROVE)
        # 重启（日志里只有启动确认）=> F-1 待核实
        _, _, restarted = make_controller(
            Path(self.tmp.name) / "events.jsonl", self.clock
        )
        self.assertIs(restarted.fan_state("F-1"), FanState.UNVERIFIED)
        self.assertIs(restarted.fan_state("F-2"), FanState.STOPPED)
        self.assertEqual(restarted.unverified_fans, ["F-1"])
        # 经理核实后解除
        restarted.acknowledge_fan_state("F-1", running=True, by=self.manager)
        self.assertIs(restarted.fan_state("F-1"), FanState.RUNNING)

    def test_force_override_defers_tariff_but_starts_fan(self):
        # 峰段：顾问建议 DEFER，经理强制覆盖（安全条件本身良好）
        self.clock.t = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
        peak_weather = WeatherSnapshot(self.clock.t, 14.0, 55, rain=False)
        rec = self.ctrl.advise(readings(self.clock.t), peak_weather, pressure(self.clock.t))
        self.assertIs(rec.action, Action.DEFER_TO_OFF_PEAK)
        admin = User("boss", "admin")
        self.ctrl.approve(
            rec, admin, ApprovalDecision.FORCE_OVERRIDE,
            readings=readings(self.clock.t), weather=peak_weather,
            pressures=pressure(self.clock.t), note="生产急需",
        )
        self.assertTrue(self.gw.is_running("F-1"))

    def test_force_override_still_blocked_by_dewpoint_gate(self):
        # 峰段且空气潮湿（露点裕度不足）：即使强制也禁止
        self.clock.t = datetime(2026, 9, 12, 9, 30, tzinfo=TZ)
        humid = WeatherSnapshot(self.clock.t, 22.0, 95, rain=False)
        rec = self.ctrl.advise(readings(self.clock.t, top=20.0), humid,
                               pressure(self.clock.t))
        self.assertNotIn(rec.action, (Action.START,))
        admin = User("boss", "admin")
        with self.assertRaises(Exception):
            self.ctrl.approve(
                rec, admin, ApprovalDecision.FORCE_OVERRIDE,
                readings=readings(self.clock.t, top=20.0), weather=humid,
                pressures=pressure(self.clock.t),
            )
        self.assertFalse(self.gw.is_running("F-1"))

    def test_unconfirmed_receipt_keeps_fan_unverified(self):
        class FlakyGateway(SimulatedFanGateway):
            def send(self, command: Command):
                result = super().send(command)
                if command.verb is CommandVerb.START_FAN:
                    return FanStatusReceipt(
                        command_id=command.command_id, fan_id=command.fan_id,
                        at=self._clock(), running=False, confirmed=False,
                        message="应答超时",
                    )
                return result

        gw = FlakyGateway([f.id for f in self.domain.fans], clock=self.clock)
        ctrl = AerationController(
            self.domain, JsonlEventStore(Path(self.tmp.name) / "flaky.jsonl"),
            gw, clock=self.clock,
        )
        rec = ctrl.advise(readings(self.clock.t), self.weather, pressure(self.clock.t))
        ctrl.approve(rec, self.manager, ApprovalDecision.APPROVE)
        self.assertIs(ctrl.fan_state("F-1"), FanState.UNVERIFIED)


class RecoveryTest(unittest.TestCase):
    def test_clean_log_means_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            store = JsonlEventStore(Path(d) / "x.jsonl")
            state = recover_state(store, ["F-1", "F-2"])
            self.assertEqual(set(state.fan_states.values()), {FanState.STOPPED})

    def test_stop_command_without_receipt_is_unverified(self):
        with tempfile.TemporaryDirectory() as d:
            store = JsonlEventStore(Path(d) / "x.jsonl")
            at = datetime(2026, 9, 12, 23, tzinfo=TZ)
            store.append("command", {
                "command_id": "C1", "rec_id": "R1", "verb": "START_FAN",
                "fan_id": "F-1", "issued_at": at.isoformat(),
            }, at=at)
            at2 = at + timedelta(hours=1)
            store.append("command", {
                "command_id": "C2", "rec_id": "R2", "verb": "STOP_FAN",
                "fan_id": "F-1", "issued_at": at2.isoformat(),
            }, at=at2)
            # 没有停机确认回执：仍待核实
            state = recover_state(store, ["F-1", "F-2"])
            self.assertIs(state.fan_states["F-1"], FanState.UNVERIFIED)
            self.assertIs(state.fan_states["F-2"], FanState.STOPPED)

    def test_confirmed_stop_receipt_means_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            store = JsonlEventStore(Path(d) / "x.jsonl")
            at = datetime(2026, 9, 12, 23, tzinfo=TZ)
            store.append("command", {
                "command_id": "C1", "rec_id": "R1", "verb": "START_FAN",
                "fan_id": "F-1", "issued_at": at.isoformat(),
            }, at=at)
            store.append("receipt", {
                "command_id": "C1", "fan_id": "F-1", "running": True,
                "confirmed": True, "at": at.isoformat(),
            }, at=at)
            at2 = at + timedelta(hours=1)
            store.append("command", {
                "command_id": "C2", "rec_id": "R2", "verb": "STOP_FAN",
                "fan_id": "F-1", "issued_at": at2.isoformat(),
            }, at=at2)
            store.append("receipt", {
                "command_id": "C2", "fan_id": "F-1", "running": False,
                "confirmed": True, "at": at2.isoformat(),
            }, at=at2)
            state = recover_state(store, ["F-1"])
            self.assertIs(state.fan_states["F-1"], FanState.STOPPED)


if __name__ == "__main__":
    unittest.main()

"""端到端示例：夜间降温后的通风控制中枢。

运行：

    python3 examples/night_run.py

场景顺序：

1. 22:00 投诉夜复盘：外温确实下降，但降雨 + 熏蒸 + 高湿露点 -> 建议 FORBIDDEN；
2. 次日 23:00 谷段：干冷空气到位，中层测点漂移被隔离、风险区间扩大，
   质量经理批准后启动 F-1（F-2 因同风道互锁保持停用）；
3. 03:00 外温回升，系统建议 STOP，结算实际温降/能耗并与预期对标，回放时间线；
4. 紧急人工停机：值班员一键全停，不等优化周期；
5. 进程崩溃后重启：只有启动指令、没有停机确认的风机进入 UNVERIFIED 待核实。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grain_aeration import (  # noqa: E402
    AerationController,
    ApprovalDecision,
    DomainConfig,
    JsonlEventStore,
    SensorQuality,
    SensorReading,
    User,
    WeatherSnapshot,
    load_domain,
)
from grain_aeration.audit import benchmark, build_timeline, format_timeline  # noqa: E402
from grain_aeration.hardware import SimulatedFanGateway  # noqa: E402
from grain_aeration.models import DuctPressure  # noqa: E402

REFERENCE = Path(__file__).resolve().parents[1] / "reference" / "domain.json"
TZ = datetime.fromisoformat("2026-09-11T22:00:00+08:00").tzinfo


class MutableClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


def readings(at: datetime, top_temp: float) -> list[SensorReading]:
    return [
        SensorReading("T-TOP-1", 0.5, at, top_temp, 70, SensorQuality.GOOD),
        # 中层电缆漂移：必须隔离
        SensorReading("T-MID-1", 2.5, at, 99.9, 66, SensorQuality.DRIFTING),
    ]


def pressures(at: datetime, static_pa: float = 350.0, ok: bool = True) -> list[DuctPressure]:
    return [DuctPressure("DUCT-N", at, static_pa, ok)]


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_rec(rec) -> None:
    print(f"建议 {rec.rec_id} -> {rec.action.value} @ {rec.created_at:%Y-%m-%d %H:%M%z}")
    for e in rec.reasons:
        flag = "【门禁】" if e.blocks_start else "        "
        print(f"  {flag} {e.message}")
    if rec.isolated_sensors:
        print(f"  隔离测点: {rec.isolated_sensors}，扩大风险深度区间: "
              f"{rec.risk_depth_band[0]:.1f}–{rec.risk_depth_band[1]:.1f}m")
    if rec.fan_plans:
        print("  风机计划: " + ", ".join(
            f"{p.fan_id}{'启动' if p.start else '停机'}" for p in rec.fan_plans
        ))
    if rec.expected_energy_kwh:
        print(f"  预期: 温降 {rec.expected_temp_drop_c}℃ / "
              f"{rec.expected_duration_h}h / {rec.expected_energy_kwh}kWh / "
              f"{rec.expected_cost} 元")


# --------------------------------------------------------------------- #
def main() -> None:
    domain = load_domain(REFERENCE)
    tmp = Path(tempfile.mkdtemp(prefix="aeration-"))
    manager = User("zhao_quality", "quality_manager")
    operator = User("sun_operator", "operator")

    # ============ 场景 1：投诉夜 22:00，禁止启动 ============ #
    banner("场景 1  22:00 投诉夜：只看到外温下降就开机？系统说不")
    clock = MutableClock(datetime(2026, 9, 11, 22, 0, tzinfo=TZ))
    gw = SimulatedFanGateway([f.id for f in domain.fans], clock=clock)
    store = JsonlEventStore(tmp / "night.jsonl")
    ctrl = AerationController(domain, store, gw, clock=clock)

    rainy = WeatherSnapshot(clock.t, 19.2, 91, rain=True)
    rec = ctrl.advise(readings(clock.t, 24.8), rainy, pressures(clock.t))
    show_rec(rec)
    assert rec.action.value == "FORBIDDEN", rec.action
    try:
        ctrl.approve(rec, manager, ApprovalDecision.APPROVE)
    except Exception as exc:
        print(f"  >> 即使质量经理批准也被拦截: {exc}")

    # ============ 场景 2：次日 23:00 谷段，批准启动 ============ #
    banner("场景 2  次日 23:00 谷段：干冷无风，批准启动 F-1")
    clock.t = datetime(2026, 9, 12, 23, 0, tzinfo=TZ)
    dry_cold = WeatherSnapshot(clock.t, 17.0, 65, rain=False)
    rec2 = ctrl.advise(readings(clock.t, 24.8), dry_cold, pressures(clock.t))
    show_rec(rec2)
    assert rec2.action.value == "START"
    assert [p.fan_id for p in rec2.fan_plans if p.start] == ["F-1"], "互锁应只放行 F-1"

    receipts = ctrl.approve(rec2, manager, ApprovalDecision.APPROVE, note="按建议执行")
    print(f"  >> 已下发并收到 {len(receipts)} 条设备确认回执；"
          f"F-1 运行={gw.is_running('F-1')}，F-2 运行={gw.is_running('F-2')}")
    assert gw.is_running("F-1") and not gw.is_running("F-2")

    # ============ 场景 3：03:00 外温回升，停机并结算 ============ #
    banner("场景 3  03:00 外温回升：建议 STOP，结算与对标")
    clock.t = datetime(2026, 9, 13, 3, 0, tzinfo=TZ)
    warmer = WeatherSnapshot(clock.t, 23.5, 70, rain=False)
    rec3 = ctrl.advise(readings(clock.t, 22.3), warmer, pressures(clock.t))
    show_rec(rec3)
    assert rec3.action.value == "STOP"
    ctrl.approve(rec3, manager, ApprovalDecision.APPROVE)

    summary = ctrl.close_session(
        rec2.rec_id, cold_end_before_c=24.8, cold_end_after_c=22.3
    )
    print(f"  >> 实际运行 {summary.actual_duration_h:.1f}h，"
          f"耗电 {summary.actual_energy_kwh:.1f}kWh，"
          f"电费 {summary.actual_cost:.2f} 元，"
          f"冷端实际温降 {summary.actual_temp_drop_c:.1f}℃")

    banner("时间线回放（建议 → 批准 → 指令 → 回执）")
    print(format_timeline(build_timeline(store, rec_id=rec2.rec_id)))

    banner("预期 vs 实际对标")
    for row in benchmark(store):
        flag = "  ⚠ 偏离" if row.deviates else "  ✓ 符合"
        print(f"{flag} {row.rec_id} [{row.action}]")
        print(f"      温降 预期 {row.expected_temp_drop_c}℃ / 实际 "
              f"{row.actual_temp_drop_c}℃（偏差 {row.temp_drop_deviation_c}℃）")
        print(f"      能耗 预期 {row.expected_energy_kwh}kWh / 实际 "
              f"{row.actual_energy_kwh}kWh（{row.energy_deviation_pct}%）")
        print(f"      电费 预期 {row.expected_cost} 元 / 实际 {row.actual_cost} 元")

    # ============ 场景 4：紧急人工停机 ============ #
    banner("场景 4  紧急人工停机：值班员一键全停，不走优化/批准周期")
    clock.t = datetime(2026, 9, 13, 3, 30, tzinfo=TZ)
    gw4 = SimulatedFanGateway([f.id for f in domain.fans], clock=clock)
    store4 = JsonlEventStore(tmp / "emergency.jsonl")
    ctrl4 = AerationController(domain, store4, gw4, clock=clock)
    rec4 = ctrl4.advise(readings(clock.t, 24.0),
                        WeatherSnapshot(clock.t, 16.0, 60, rain=False),
                        pressures(clock.t))
    ctrl4.approve(rec4, manager, ApprovalDecision.APPROVE)
    assert gw4.is_running("F-1")
    # 一线值班员（无批准权）也可立即紧急停机
    stopped = ctrl4.emergency_stop(operator, reason="巡检发现风道异响")
    print(f"  >> 紧急停机回执 {len(stopped)} 条；F-1 运行={gw4.is_running('F-1')}")
    assert not gw4.is_running("F-1")

    # ============ 场景 5：崩溃恢复 -> UNVERIFIED ============ #
    banner("场景 5  进程意外退出后重启：未证实关闭的风机一律待核实")
    clock.t = datetime(2026, 9, 13, 4, 0, tzinfo=TZ)
    crash_path = tmp / "crash.jsonl"
    gw5 = SimulatedFanGateway([f.id for f in domain.fans], clock=clock)
    ctrl5 = AerationController(domain, JsonlEventStore(crash_path), gw5, clock=clock)
    rec5 = ctrl5.advise(readings(clock.t, 24.0),
                        WeatherSnapshot(clock.t, 15.0, 58, rain=False),
                        pressures(clock.t))
    ctrl5.approve(rec5, manager, ApprovalDecision.APPROVE)
    print("  >> 启动确认已落盘，进程在此崩溃（无停机指令、无停机回执）……")

    restarted = AerationController(
        domain, JsonlEventStore(crash_path),
        SimulatedFanGateway([f.id for f in domain.fans], clock=clock),
        clock=clock,
    )
    print(f"  >> 重启后风机状态: "
          + ", ".join(f"{fid}={restarted.fan_state(fid).value}" for fid in ("F-1", "F-2")))
    assert restarted.fan_state("F-1").value == "UNVERIFIED"
    assert restarted.fan_state("F-2").value == "STOPPED"

    # 现场核查：F-1 实际在转，先确认运行状态；值班员无核实权，需经理
    try:
        restarted.acknowledge_fan_state("F-1", running=True, by=operator)
    except PermissionError as exc:
        print(f"  >> 普通值班员无权核实: {exc}")
    restarted.acknowledge_fan_state("F-1", running=True, by=manager)
    print("  >> 经理现场核实 F-1 确在运行，待核实状态解除；随后可走正常停机流程")

    banner("全部场景通过")
    print(f"事件日志目录: {tmp}")


if __name__ == "__main__":
    main()

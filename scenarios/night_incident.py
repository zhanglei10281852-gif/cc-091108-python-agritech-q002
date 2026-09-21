"""夜间降温结露投诉 —— 控制中枢全流程演示。

运行：

    python3 -m scenarios.night_incident

包含四个片段：
1. 复盘 22:00：系统如何否决值班员"只看仓外降温就开风机"的操作
   （熏蒸期 + 降雨倒灌 + 露点逼近 + 风道压力异常，四重相反信号）；
2. 次日 23:30 安全窗口：带依据建议 START → 值班员确认 → 指令/回执 → 周期复盘；
3. 进程意外退出后重启：未证实关闭的风机进入"待核实"；
4. 紧急人工停机：不等待优化周期。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from grain_aeration.advisor import Advisor
from grain_aeration.controller import (
    AerationController,
    AuthorizationError,
    ControlRejected,
)
from grain_aeration.data import (
    latest_readings,
    load_domain,
    parse_fans,
    parse_pressures,
    parse_restrictions,
    parse_sensors,
    parse_tariff,
    parse_weather,
    weather_at,
)
from grain_aeration.fans import (
    FanRuntimeState,
    SimulatedGateway,
)
from grain_aeration.models import (
    Actor,
    DuctPressure,
    Role,
    SensorQuality,
    SensorReading,
    WeatherPoint,
)
from grain_aeration.store import EventStore
from grain_aeration.timeline import render_text

TZ = ZoneInfo("Asia/Shanghai")
DATA = load_domain(Path(__file__).parents[1] / "reference" / "domain.json")

OPERATOR = Actor("U-102", "夜班值班员·张伟", Role.OPERATOR)
VIEWER = Actor("U-099", "实习生·小李", Role.VIEWER)
MANAGER = Actor("U-001", "质量经理·王敏", Role.QUALITY_MANAGER)
ADMIN = Actor("U-000", "自控管理员·陈工", Role.ADMIN)


def build_controller(store_path=None):
    sensors = parse_sensors(DATA)
    fans = parse_fans(DATA)
    restrictions = parse_restrictions(DATA)
    tariff = parse_tariff(DATA)
    store = EventStore(store_path)
    ctrl = AerationController(
        sensors=sensors,
        fans=fans,
        restrictions=restrictions,
        gateway=SimulatedGateway(),
        store=store,
        advisor=Advisor(tariff=tariff),
    )
    return ctrl


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------- 片段 1

def segment_1_incident_review(ctrl: AerationController) -> None:
    banner("片段 1｜2026-09-11 22:00 事故时刻：系统建议（建议方，不驱动设备）")
    at = datetime(2026, 9, 11, 22, 0, tzinfo=TZ)
    readings = latest_readings(DATA, at)
    weather = weather_at(parse_weather(DATA), at)
    pressures = parse_pressures(DATA, at)

    result = ctrl.tick(at, readings, weather, pressures)
    rec = result.recommendation

    print(f"建议编号 {rec.id}  动作={rec.action.value}  目标风机={rec.fan_ids or '无'}")
    print(f"粮堆最冷点露点≈{rec.grain_dewpoint_c}°C  外界露点≈{rec.outside_dewpoint_c}°C")
    print("\n判断依据：")
    for e in rec.evidence:
        print(f"  [{e.severity.value:5}] {e.code}: {e.message}")
    print("\n硬性否决（任一成立即禁止启动）：")
    for v in rec.vetoes:
        if v.severity.value == "block":
            print(f"  [BLOCK] {v.code}: {v.message}")
    print("\n其它警告：")
    for v in rec.vetoes:
        if v.severity.value != "block":
            print(f"  [WARN ] {v.code}: {v.message}")

    print("\n隔离情况：漂移点 T-MID-1 被隔离，风险区间扩大到：",
          sorted(result.assessment.expanded_zone))

    print("\n若值班员只凭仓外降温强行操作：")
    try:
        ctrl.approve(rec.id, OPERATOR, at + timedelta(seconds=5))
    except ControlRejected as exc:
        print(f"  值班员确认被拒 → {exc}")

    # 即便建议被改为可批准，下发瞬间安全门仍会拦截（此处用越权演示权限分离）
    print("权限分离演示：实习生（viewer）尝试确认：")
    try:
        ctrl.approve(rec.id, VIEWER, at + timedelta(seconds=6))
    except AuthorizationError as exc:
        print(f"  {exc}")


# ---------------------------------------------------------------- 片段 2

def segment_2_safe_window(ctrl: AerationController) -> str:
    banner("片段 2｜次日 23:30 安全干燥窗口：建议→确认→指令→回执→复盘")
    at = datetime(2026, 9, 12, 23, 30, tzinfo=TZ)
    # 熏蒸与倒灌窗口均已结束；构造干冷夜间读数
    readings = {
        s.id: SensorReading(
            sensor_id=s.id,
            at=at,
            temperature_c={"top": 23.5, "middle": 27.0, "bottom": 15.5}[s.layer.value]
            + (0.4 if s.cable_id == "C-2" else 0.0),
            humidity_pct={"top": 70, "middle": 64, "bottom": 70}[s.layer.value],
            quality=s_quality(s.id),
        )
        for s in ctrl.sensors
    }
    weather = WeatherPoint(at=at, temperature_c=11.5, humidity_pct=62, rain=False,
                           wind_speed_mps=1.6)
    pressures = {"DUCT-N": DuctPressure("DUCT-N", at, -610.0, (-1200.0, -100.0))}

    result = ctrl.tick(at, readings, weather, pressures)
    rec = result.recommendation
    print(f"建议 {rec.id}：动作={rec.action.value}，风机={rec.fan_ids}")
    for e in rec.evidence:
        if e.severity.value in ("info", "warn"):
            print(f"  [{e.severity.value:5}] {e.code}: {e.message}")

    # 注意：同组 F-1/F-2 互锁，建议只选一台
    assert rec.fan_ids == ["F-1"], rec.fan_ids

    ctrl.approve(rec.id, OPERATOR, at + timedelta(minutes=1), note="谷段，露点安全，开 F-1")
    receipts = ctrl.execute(rec.id, OPERATOR, at + timedelta(minutes=2))
    print("\n设备回执：")
    for r in receipts:
        print(f"  {r.fan_id} {r.result.value.upper()} running={r.running}：{r.message}")

    # 运行 4 小时后收尾：温度下降，电表结算
    end = at + timedelta(hours=4)
    end_readings = {
        sid: SensorReading(sid, end, max(12.0, r.temperature_c - 2.4), r.humidity_pct,
                           r.quality)
        for sid, r in readings.items()
    }
    # 让运行段满 4 小时：把 started_at 锚到执行时刻（回执时间）
    ctrl.bank.runtimes["F-1"].started_at = at + timedelta(minutes=2)
    report = ctrl.close_cycle(rec.id, end, end_readings, weather)
    print("\n周期复盘：")
    for k, v in report.to_dict().items():
        print(f"  {k}: {v}")
    return rec.id


def s_quality(sid: str) -> SensorQuality:
    return SensorQuality.DRIFTING if sid == "T-MID-1" else SensorQuality.GOOD


# ---------------------------------------------------------------- 片段 3

def segment_3_restart(tmp_path: str) -> AerationController:
    banner("片段 3｜进程意外退出后重启：未证实关闭 → 待核实（而非安全停机）")
    at0 = datetime(2026, 9, 12, 23, 40, tzinfo=TZ)
    ctrl = build_controller(tmp_path)
    ctrl.recover(at0)
    print("重启后风机状态：")
    for fid, rt in ctrl.bank.runtimes.items():
        print(f"  {fid}: {rt.state.value}")
    pending = [fid for fid, rt in ctrl.bank.runtimes.items()
               if rt.state is FanRuntimeState.PENDING_VERIFICATION]
    print("待核实清单：", pending)
    assert pending == ["F-1"], pending

    print("\n值班员现场核实：F-1 确已停止（停电时正好处于停机间隙）")
    state = ctrl.verify_pending(OPERATOR, "F-1", confirmed_running=False,
                                at=at0 + timedelta(minutes=3))
    print(f"  F-1 → {state.value}")
    return ctrl


# ---------------------------------------------------------------- 片段 4

def segment_4_emergency(ctrl: AerationController) -> None:
    banner("片段 4｜运行中紧急人工停机：立即下发，不等优化周期")
    at = datetime(2026, 9, 13, 23, 20, tzinfo=TZ)
    readings = {
        s.id: SensorReading(s.id, at, 22.0, 68, s_quality(s.id))
        for s in ctrl.sensors
    }
    weather = WeatherPoint(at=at, temperature_c=9.0, humidity_pct=55, rain=False)
    pressures = {"DUCT-N": DuctPressure("DUCT-N", at, -590.0, (-1200.0, -100.0))}
    rec = ctrl.tick(at, readings, weather, pressures).recommendation
    if rec.action.value == "start":
        ctrl.approve(rec.id, OPERATOR, at)
        ctrl.execute(rec.id, OPERATOR, at + timedelta(minutes=1))

    print("巡视发现仓门密封异常 → 质量经理拍下急停")
    receipts = ctrl.emergency_stop(MANAGER, at + timedelta(minutes=20))
    for r in receipts:
        print(f"  {r.fan_id} {r.result.value.upper()}：{r.message}")
    print("急停后再尝试启动建议：")
    later = ctrl.tick(at + timedelta(minutes=21), readings, weather, pressures).recommendation
    print(f"  新建议动作={later.action.value}，veto 含 emergency_stop：",
          any(v.code == "emergency_stop" for v in later.vetoes))
    try:
        ctrl.approve(later.id, MANAGER, at + timedelta(minutes=22))
    except ControlRejected as exc:
        print(f"  确认即被拒：{exc}")


def main() -> None:
    import tempfile

    store_path = Path(tempfile.mkdtemp()) / "audit.jsonl"
    ctrl = build_controller(store_path)

    segment_1_incident_review(ctrl)
    rec_id = segment_2_safe_window(ctrl)
    restarted = segment_3_restart(store_path)
    segment_4_emergency(restarted)

    banner("片段 2 的完整时间线回放（建议→确认→指令→回执→复盘）")
    print(render_text(ctrl.timeline(rec_id)))

    print("\n重启恢复摘要（供接班查阅）：")
    summary = restarted.recovery_summary()
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

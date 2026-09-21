"""控制时间线回放：建议 → 批准 → 指令 → 回执。

质量经理复盘时按 seq（即真实发生顺序）取出与某条建议（或急停等全局动作）
相关的全部事件，生成可直接阅读的时间线条目。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import Event

# 事件类型 -> (中文动作名, 是否绑定某条建议)
_LABELS: dict[str, tuple[str, bool]] = {
    "recommendation_proposed": ("系统提出建议", True),
    "recommendation_approved": ("值班员确认", True),
    "recommendation_rejected": ("值班员驳回", True),
    "recommendation_executed": ("建议执行完成", True),
    "recommendation_expired": ("建议过期", True),
    "execute_blocked": ("下发被安全门拦截", True),
    "command_issued": ("控制指令下发", True),
    "receipt_received": ("设备回执", True),
    "emergency_stop_triggered": ("紧急人工停机", False),
    "emergency_stop_reset": ("急停复位", False),
    "sensor_isolated": ("测点隔离登记", False),
    "pending_verified": ("待核实风机现场核实", False),
    "recovery_pending": ("重启恢复：风机置待核实", False),
    "cycle_closed": ("周期收尾复盘", True),
}


@dataclass(frozen=True)
class TimelineEntry:
    seq: int
    at: str
    actor: str
    action_label: str
    detail: str
    event_type: str
    recommendation_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "actor": self.actor,
            "action": self.action_label,
            "detail": self.detail,
            "event_type": self.event_type,
            "recommendation_id": self.recommendation_id,
        }


def _rec_id_of(ev: Event) -> str | None:
    p = ev.payload
    if "recommendation_id" in p:
        return p["recommendation_id"]
    if "recommendation" in p:
        return p["recommendation"].get("id")
    if "report" in p:
        return p["report"].get("recommendation_id")
    return None


def _detail(ev: Event) -> str:
    p = ev.payload
    t = ev.type
    if t == "recommendation_proposed":
        r = p["recommendation"]
        veto_msgs = [v["message"] for v in r.get("vetoes", []) if v["severity"] == "block"]
        base = (
            f"建议动作={r['action']}，目标风机={','.join(r['fan_ids']) or '无'}；"
            f"粮堆露点≈{r.get('grain_dewpoint_c')}°C，外界露点≈{r.get('outside_dewpoint_c')}°C；"
            f"预期降温 {r.get('expected_delta_c')}°C / {r.get('expected_kwh')}kWh / "
            f"{r.get('expected_cost')} 元"
        )
        if veto_msgs:
            base += "；硬性否决：" + "；".join(veto_msgs)
        return base
    if t == "recommendation_approved":
        return f"确认通过" + (f"，备注：{p['note']}" if p.get("note") else "")
    if t == "recommendation_rejected":
        return f"驳回" + (f"，备注：{p['note']}" if p.get("note") else "")
    if t == "command_issued":
        emer = "（急停）" if p.get("emergency") else ""
        return f"{emer}指令 {p['command_id']}：{p['kind']} 风机 {p['fan_id']}"
    if t == "receipt_received":
        return (
            f"风机 {p['fan_id']} 回执 {p['result'].upper()}"
            f"（running={p['running']}）→ 状态 {p['new_state']}：{p.get('message', '')}"
        )
    if t == "execute_blocked":
        return "拦截原因：" + "；".join(v["message"] for v in p["vetoes"])
    if t == "emergency_stop_triggered":
        return f"{p['actor']['name']} 触发紧急停机，控制中枢锁定"
    if t == "emergency_stop_reset":
        return "急停复位，启动锁定解除"
    if t == "sensor_isolated":
        return f"测点 {p['sensor_id']} 隔离，原因：{p['reason']}"
    if t == "pending_verified":
        real = "确认在运行" if p["confirmed_running"] else "确认已停止"
        return f"风机 {p['fan_id']} 现场核实：{real} → {p['new_state']}"
    if t == "recovery_pending":
        return (
            f"重启后进入待核实的风机：{', '.join(p['pending_fans'])}；"
            f"规则：{p['rule']}"
        )
    if t == "cycle_closed":
        r = p["report"]
        d = r["deviation"]
        return (
            f"实际降温 {r['actual_delta_c']}°C（预期 {r['expected_delta_c']}°C，"
            f"偏差 {d['delta_diff_c']:+.1f}°C）；实际能耗 {r['actual_kwh']}kWh"
            f"（预期 {r['expected_kwh']}kWh，偏差 {d['kwh_diff']:+.1f}）；"
            f"实际电费 {r['actual_cost']} 元（偏差 {d['cost_diff']:+.2f}）"
            + (f"；{d['kwh_per_degree']} kWh/°C" if d.get("kwh_per_degree") else "")
        )
    if t == "recommendation_expired":
        return "建议超过有效期未被确认"
    if t == "recommendation_executed":
        return f"共 {len(p.get('receipts', []))} 台设备回执"
    return str(p)


def build_timeline(events: list[Event], rec_id: str | None = None) -> list[TimelineEntry]:
    rows: list[TimelineEntry] = []
    for ev in events:
        rid = _rec_id_of(ev)
        if rec_id is not None and rid != rec_id:
            # 全局事件（急停/恢复/隔离）在按建议过滤时仍保留其关联上下文
            if rid is not None:
                continue
            if ev.type not in ("emergency_stop_triggered", "recovery_pending"):
                continue
        label, _ = _LABELS.get(ev.type, (ev.type, False))
        actor = ev.actor["name"] if ev.actor else "系统"
        rows.append(
            TimelineEntry(
                seq=ev.seq,
                at=ev.at.isoformat(),
                actor=actor,
                action_label=label,
                detail=_detail(ev),
                event_type=ev.type,
                recommendation_id=rid,
            )
        )
    return rows


def render_text(entries: list[TimelineEntry]) -> str:
    """把时间线渲染为便于值班交接阅读的文本。"""
    lines = []
    for e in entries:
        lines.append(f"#{e.seq:<3} {e.at}  [{e.actor}] {e.action_label}")
        lines.append(f"      {e.detail}")
    return "\n".join(lines)


def summarize_recovery(events: list[Event]) -> dict:
    """汇总恢复视角信息：各风机最后一条回执与重启后的待核实清单。"""
    last_receipt: dict[str, dict] = {}
    for ev in events:
        if ev.type == "receipt_received":
            last_receipt[ev.payload["fan_id"]] = {
                "at": ev.at.isoformat(),
                "result": ev.payload["result"],
                "running": ev.payload["running"],
                "new_state": ev.payload["new_state"],
            }
    pending: list[str] = []
    rule = ""
    for ev in events:
        if ev.type == "recovery_pending":
            # 以最近一次恢复事件为准重置清单
            pending = list(ev.payload["pending_fans"])
            rule = ev.payload["rule"]
        elif ev.type == "pending_verified" and pending:
            # 事后现场核实的风机从待核实清单移除
            pending = [f for f in pending if f != ev.payload["fan_id"]]
    return {
        "last_receipt_per_fan": last_receipt,
        "pending_verification": pending,
        "rule": rule,
    }

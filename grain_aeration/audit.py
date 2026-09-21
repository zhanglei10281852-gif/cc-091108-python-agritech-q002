"""时间线回放与效果对标。

质量经理在每次控制结束后可以：

- 按时间线查看：建议（含依据）-> 批准 -> 指令 -> 设备回执；
- 比较实际温降、实际能耗与建议中的预期值，标出偏离。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import Event
from .persistence import JsonlEventStore


@dataclass(frozen=True)
class TimelineEntry:
    seq: int
    at: datetime
    kind: str
    title: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class BenchmarkRow:
    rec_id: str
    action: str
    expected_temp_drop_c: float
    actual_temp_drop_c: float | None
    expected_energy_kwh: float
    actual_energy_kwh: float | None
    expected_cost: float
    actual_cost: float | None
    temp_drop_deviation_c: float | None
    energy_deviation_pct: float | None

    @property
    def deviates(self) -> bool:
        if self.energy_deviation_pct is None:
            return False
        return abs(self.energy_deviation_pct) >= 15.0 or (
            self.temp_drop_deviation_c is not None
            and self.temp_drop_deviation_c < -1.0
        )


def build_timeline(store: JsonlEventStore, rec_id: str | None = None) -> list[TimelineEntry]:
    """把事件流整理成可读时间线；可按某次建议过滤。

    过滤某条 START 建议时，会同时包含“闭合该次运行”的后续 STOP 指令与回执，
    保证一次通风会话的时间线首尾完整。
    """
    events = store.read_all()
    # command_id -> rec_id
    command_rec: dict[str, str | None] = {}
    for ev in events:
        if ev.kind == "command":
            command_rec[ev.payload["command_id"]] = ev.payload.get("rec_id")

    related_commands: set[str] = set()
    if rec_id:
        # 该建议自己的指令，以及其启动确认被对应停机回执闭合的那些停机指令
        start_cmds = {
            ev.payload["command_id"]
            for ev in events
            if ev.kind == "command"
            and ev.payload.get("rec_id") == rec_id
            and ev.payload.get("verb") == "START_FAN"
        }
        open_fan: dict[str, str] = {}
        for ev in events:
            if ev.kind != "receipt":
                continue
            p = ev.payload
            fid = p["fan_id"]
            if p.get("confirmed") and p.get("running") is True:
                if p["command_id"] in start_cmds:
                    open_fan[fid] = p["command_id"]
            elif p.get("confirmed") and p.get("running") is False and fid in open_fan:
                open_fan.pop(fid)
                related_commands.add(p["command_id"])  # 闭合本次运行的停机指令

    def _keep(ev: Event) -> bool:
        if not rec_id:
            return True
        p = ev.payload
        if ev.kind == "receipt":
            cmd = p.get("command_id")
            return command_rec.get(cmd) == rec_id or cmd in related_commands
        if ev.kind == "command":
            return p.get("rec_id") == rec_id or p.get("command_id") in related_commands
        if ev.kind in ("recommendation", "approval", "session_summary"):
            return p.get("rec_id") == rec_id
        return False  # emergency_stop 为全局事件，过滤单次建议时不展示

    entries: list[TimelineEntry] = []
    for ev in events:
        if not _keep(ev):
            continue
        p = ev.payload
        if ev.kind == "recommendation":
            entries.append(TimelineEntry(
                ev.seq, ev.at, ev.kind,
                f"建议 {p['action']}（{p['rec_id']}）",
                {
                    "reasons": [r["message"] for r in p.get("reasons", [])],
                    "fan_plans": [
                        f"{x['fan_id']}:{'启动' if x['start'] else '停机'}"
                        for x in p.get("fan_plans", [])
                    ],
                    "expected_temp_drop_c": p.get("expected_temp_drop_c"),
                    "expected_energy_kwh": p.get("expected_energy_kwh"),
                    "expected_cost": p.get("expected_cost"),
                    "risk_depth_band": p.get("risk_depth_band"),
                    "isolated_sensors": p.get("isolated_sensors"),
                },
            ))
        elif ev.kind == "approval":
            entries.append(TimelineEntry(
                ev.seq, ev.at, ev.kind,
                f"批准 {p.get('decision')} by {p.get('approver', '-')}",
                {"rec_id": p.get("rec_id"), "role": p.get("approver_role"),
                 "note": p.get("note", "")},
            ))
        elif ev.kind == "command":
            entries.append(TimelineEntry(
                ev.seq, ev.at, ev.kind,
                f"指令 {p['verb']} -> {p.get('fan_id') or '全部风机'}",
                {"command_id": p["command_id"], "rec_id": p.get("rec_id"),
                 "issued_by": p.get("issued_by")},
            ))
        elif ev.kind == "emergency_stop":
            entries.append(TimelineEntry(
                ev.seq, ev.at, ev.kind,
                f"紧急人工停机 by {p.get('issued_by')}",
                {"reason": p.get("reason"), "command_id": p.get("command_id")},
            ))
        elif ev.kind == "receipt":
            entries.append(TimelineEntry(
                ev.seq, ev.at, ev.kind,
                f"回执 {p['fan_id']}: "
                + ("运行" if p.get("running") else "停机")
                + ("" if p.get("confirmed") else "（未确认！）"),
                {"command_id": p.get("command_id"), "message": p.get("message", "")},
            ))
        elif ev.kind == "session_summary":
            entries.append(TimelineEntry(
                ev.seq, ev.at, ev.kind,
                f"会话结果 {p['rec_id']}",
                {k: v for k, v in p.items() if k != "rec_id"},
            ))
    return entries


def format_timeline(entries: list[TimelineEntry]) -> str:
    lines: list[str] = []
    for e in entries:
        lines.append(f"[{e.at:%Y-%m-%d %H:%M:%S%z}] #{e.seq} {e.title}")
        if e.kind == "recommendation":
            for reason in e.detail.get("reasons", []):
                lines.append(f"    依据: {reason}")
            plans = e.detail.get("fan_plans") or []
            if plans:
                lines.append(f"    风机: {', '.join(plans)}")
            lines.append(
                f"    预期: 温降 {e.detail.get('expected_temp_drop_c')}℃ / "
                f"能耗 {e.detail.get('expected_energy_kwh')}kWh / "
                f"费用 {e.detail.get('expected_cost')}元"
            )
            iso = e.detail.get("isolated_sensors") or []
            if iso:
                lines.append(
                    f"    隔离测点: {', '.join(iso)}，"
                    f"扩大风险深度区间: {e.detail.get('risk_depth_band')}"
                )
        elif e.kind in ("approval", "command", "emergency_stop", "receipt"):
            msg = e.detail.get("message") or e.detail.get("reason") or e.detail.get("note")
            if msg:
                lines.append(f"    {msg}")
        elif e.kind == "session_summary":
            lines.append(f"    {e.detail}")
    return "\n".join(lines)


def benchmark(store: JsonlEventStore) -> list[BenchmarkRow]:
    """对标每条 START 建议的预期与实际（session_summary 事件）。"""
    recs: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for ev in store.read_all():
        if ev.kind == "recommendation":
            recs[ev.payload["rec_id"]] = ev.payload
        elif ev.kind == "session_summary":
            summaries[ev.payload["rec_id"]] = ev.payload

    rows: list[BenchmarkRow] = []
    for rec_id, p in recs.items():
        actual = summaries.get(rec_id)
        exp_drop = float(p.get("expected_temp_drop_c") or 0.0)
        exp_energy = float(p.get("expected_energy_kwh") or 0.0)
        exp_cost = float(p.get("expected_cost") or 0.0)
        # FORBIDDEN/HOLD 等未发生通风的建议不参与效果对标
        if actual is None and exp_energy == 0.0:
            continue
        act_drop = float(actual["actual_temp_drop_c"]) if actual else None
        act_energy = float(actual["actual_energy_kwh"]) if actual else None
        act_cost = float(actual["actual_cost"]) if actual else None
        drop_dev = None if act_drop is None else round(act_drop - exp_drop, 2)
        energy_dev = (
            None if not exp_energy or act_energy is None
            else round((act_energy - exp_energy) / exp_energy * 100.0, 1)
        )
        rows.append(BenchmarkRow(
            rec_id=rec_id,
            action=p["action"],
            expected_temp_drop_c=exp_drop,
            actual_temp_drop_c=act_drop,
            expected_energy_kwh=exp_energy,
            actual_energy_kwh=act_energy,
            expected_cost=exp_cost,
            actual_cost=act_cost,
            temp_drop_deviation_c=drop_dev,
            energy_deviation_pct=energy_dev,
        ))
    return rows

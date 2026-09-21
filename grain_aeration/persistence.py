"""JSONL 事件日志：时间线回放与崩溃恢复的唯一事实来源。

每个事件一行 JSON，seq 单调递增。状态恢复时完整重放：

- 最后一条 START_FAN 指令之后，若没有对应风机的“已停机确认回执”，
  该风机恢复为 UNVERIFIED（待核实），而不是 STOPPED。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Approval,
    Command,
    CommandVerb,
    Event,
    FanState,
    FanStatusReceipt,
)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class JsonlEventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        for event in self.read_all():
            self._seq = max(self._seq, event.seq)

    def append(self, kind: str, payload: dict[str, Any], at: datetime | None = None) -> Event:
        self._seq += 1
        event = Event(
            seq=self._seq,
            at=at or datetime.now().astimezone(),
            kind=kind,
            payload=payload,
        )
        line = json.dumps(
            {
                "seq": event.seq,
                "at": event.at.isoformat(),
                "kind": event.kind,
                "payload": event.payload,
            },
            ensure_ascii=False,
        )
        # 追加 + flush + fsync，尽量保证崩溃前落盘
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return event

    def read_all(self) -> list[Event]:
        if not self.path.exists():
            return []
        events: list[Event] = []
        for lineno, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                events.append(
                    Event(
                        seq=int(raw["seq"]),
                        at=_dt(raw["at"]),
                        kind=raw["kind"],
                        payload=raw["payload"],
                    )
                )
            except (json.JSONDecodeError, KeyError) as exc:
                raise CorruptLogError(f"corrupt event at line {lineno}: {exc}") from exc
        events.sort(key=lambda e: e.seq)
        return events

    # ---- 类型化便捷写入 ----
    def log_recommendation(self, rec) -> Event:
        from dataclasses import asdict

        payload = asdict(rec)
        payload["action"] = rec.action.value
        payload["created_at"] = rec.created_at.isoformat()
        if rec.based_on_weather_at:
            payload["based_on_weather_at"] = rec.based_on_weather_at.isoformat()
        if rec.based_on_sensor_at:
            payload["based_on_sensor_at"] = rec.based_on_sensor_at.isoformat()
        payload["reasons"] = [asdict(e) for e in rec.reasons]
        payload["fan_plans"] = [asdict(p) for p in rec.fan_plans]
        payload["risk_depth_band"] = list(rec.risk_depth_band)
        payload["isolated_sensors"] = list(rec.isolated_sensors)
        return self.append("recommendation", payload, at=rec.created_at)

    def log_approval(self, approval: Approval | None, rec=None) -> Event:
        from dataclasses import asdict

        if approval is None:
            return self.append("approval", {"rec_id": rec and rec.rec_id, "decision": None,
                                            "note": "rejected_or_expired"})
        payload = asdict(approval)
        payload["decision"] = approval.decision.value
        payload["approver"] = approval.approver.username
        payload["approver_role"] = approval.approver.role
        payload["at"] = approval.at.isoformat()
        return self.append("approval", payload, at=approval.at)

    def log_command(self, cmd: Command) -> Event:
        payload = {
            "command_id": cmd.command_id,
            "rec_id": cmd.rec_id,
            "verb": cmd.verb.value,
            "fan_id": cmd.fan_id,
            "interlock_group": cmd.interlock_group,
            "expected_rated_kw": cmd.expected_rated_kw,
            "issued_by": cmd.issued_by,
            "issued_at": cmd.issued_at.isoformat(),
        }
        return self.append("command", payload, at=cmd.issued_at)

    def log_receipt(self, receipt: FanStatusReceipt) -> Event:
        payload = {
            "command_id": receipt.command_id,
            "fan_id": receipt.fan_id,
            "running": receipt.running,
            "confirmed": receipt.confirmed,
            "message": receipt.message,
            "at": receipt.at.isoformat(),
        }
        return self.append("receipt", payload, at=receipt.at)

    def log_session_summary(self, summary) -> Event:
        payload = {
            "rec_id": summary.rec_id,
            "fan_ids": list(summary.fan_ids),
            "started_at": summary.started_at.isoformat(),
            "ended_at": summary.ended_at.isoformat(),
            "actual_duration_h": summary.actual_duration_h,
            "actual_energy_kwh": summary.actual_energy_kwh,
            "actual_cost": summary.actual_cost,
            "cold_end_before_c": summary.cold_end_before_c,
            "cold_end_after_c": summary.cold_end_after_c,
            "actual_temp_drop_c": summary.actual_temp_drop_c,
        }
        return self.append("session_summary", payload, at=summary.ended_at)


class CorruptLogError(RuntimeError):
    pass


# ---------------------------------------------------------------------- #
# 崩溃恢复：重放事件，推导风机状态
# ---------------------------------------------------------------------- #

@dataclass(frozen=True)
class RecoveredState:
    fan_states: dict[str, FanState]
    last_event_seq: int
    # 待核实风机 -> 最近一条相关指令（供现场核查使用）
    unverified_evidence: dict[str, dict[str, Any]]


def recover_state(store: JsonlEventStore, known_fan_ids: Iterable[str]) -> RecoveredState:
    """重放日志推导每台风机的状态。

    规则：只有“设备明确回报的停机确认回执 (running=False, confirmed=True)”
    才能把风机置为 STOPPED；任何在停机确认之后又出现启动指令、
    或日志结尾仍缺停机证据的情况，一律 UNVERIFIED。
    """
    events = store.read_all()
    # fan -> 最后一次“确定状态”事件
    last_command: dict[str, dict[str, Any]] = {}
    stopped_confirmed: set[str] = set()
    started_after_stop: set[str] = set()

    for ev in events:
        if ev.kind == "command":
            p = ev.payload
            fid = p.get("fan_id")
            if not fid:
                # FORCE_STOP_ALL 类组播指令：没有点名风机关联，见下文统一处理
                continue
            last_command[fid] = p
            if p.get("verb") == CommandVerb.START_FAN.value:
                started_after_stop.add(fid)
                stopped_confirmed.discard(fid)
            elif p.get("verb") == CommandVerb.STOP_FAN.value:
                # 只是“请求停机”，在回执确认前不视为停机
                started_after_stop.add(fid)
        elif ev.kind == "receipt":
            p = ev.payload
            fid = p["fan_id"]
            if p.get("confirmed") and p.get("running") is False:
                stopped_confirmed.add(fid)
                started_after_stop.discard(fid)
            elif p.get("confirmed") and p.get("running") is True:
                stopped_confirmed.discard(fid)
                started_after_stop.add(fid)

    # FORCE_STOP_ALL：同样必须等到每台风机的停机确认回执
    fan_states: dict[str, FanState] = {}
    unverified_evidence: dict[str, dict[str, Any]] = {}
    for fid in known_fan_ids:
        if fid in stopped_confirmed and fid not in started_after_stop:
            fan_states[fid] = FanState.STOPPED
        elif fid in last_command or fid in started_after_stop:
            fan_states[fid] = FanState.UNVERIFIED
            unverified_evidence[fid] = last_command.get(fid, {
                "note": "存在针对本风机的控制事件但缺少已关闭证据"
            })
        else:
            # 从无任何针对该风机的事件：可安全视为停机（尚未投运过）
            fan_states[fid] = FanState.STOPPED
    return RecoveredState(
        fan_states=fan_states,
        last_event_seq=events[-1].seq if events else 0,
        unverified_evidence=unverified_evidence,
    )

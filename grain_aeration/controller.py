"""通风控制中枢门面。

职责边界（需求核心）：
- **建议与执行分离**：Advisor 只给带依据的建议；execute 必须由有权限值班员
  对某一条具体建议确认后才下发指令；
- **执行前二次安全校验**：批准时刻与下发时刻之间条件可能恶化（开始下雨、
  进入熏蒸窗口、邻机启动），下发瞬间重跑安全门；
- **紧急人工停机不等待优化周期**：emergency_stop 立即对全部非停机风机下发停机，
  并锁定启动；
- **周期复盘**：close_cycle 记录实际降温与实际能耗，与建议预期对比；
- **重启恢复**：从仅追加事件日志重放；凡没有"已证实关闭"回执的风机一律
  进入待核实（PENDING_VERIFICATION），绝不默认安全停机。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from .advisor import Advisor, AdvisorInputs, AdvisorResult
from .fans import (
    RECEIPT_TIMEOUT,
    CommandKind,
    DeviceGateway,
    FanBank,
    IssuedCommand,
)
from .models import (
    Action,
    Actor,
    CommandReceipt,
    Fan,
    FanRuntimeState,
    Recommendation,
    RecommendationStatus,
    Restriction,
    Role,
    Sensor,
    SensorReading,
    WeatherPoint,
    aware,
)
from .safety import SafetyContext, evaluate as safety_evaluate
from .store import EventStore
from .timeline import build_timeline, summarize_recovery


class AuthorizationError(PermissionError):
    pass


class ControlRejected(Exception):
    """下发瞬间被安全门或状态校验拒绝。"""


@dataclass
class CycleReport:
    recommendation_id: str
    closed_at: datetime
    expected_delta_c: float
    actual_delta_c: float
    expected_kwh: float
    actual_kwh: float
    expected_cost: float
    actual_cost: float
    deviation: dict

    def to_dict(self) -> dict:
        return {
            "recommendation_id": self.recommendation_id,
            "closed_at": self.closed_at.isoformat(),
            "expected_delta_c": self.expected_delta_c,
            "actual_delta_c": self.actual_delta_c,
            "expected_kwh": self.expected_kwh,
            "actual_kwh": self.actual_kwh,
            "expected_cost": self.expected_cost,
            "actual_cost": self.actual_cost,
            "deviation": self.deviation,
        }


class AerationController:
    def __init__(
        self,
        sensors: list[Sensor],
        fans: list[Fan],
        restrictions: list[Restriction] | None = None,
        gateway: DeviceGateway | None = None,
        store: EventStore | None = None,
        advisor: Advisor | None = None,
    ) -> None:
        self.sensors = sensors
        self.fans = fans
        self.restrictions = restrictions or []
        self.bank = FanBank(fans)
        self.gateway = gateway
        self.store = store or EventStore()
        self.advisor = advisor or Advisor()

        self._latest_weather: WeatherPoint | None = None
        self._latest_readings: dict[str, SensorReading] = {}
        self._latest_pressures: dict = {}
        self._recommendations: dict[str, Recommendation] = {}
        self._cycle_start_mean: dict[str, float] = {}
        self.emergency_stopped = False
        self.manual_isolation: dict[str, str] = {}
        self.recovered = False

    # ------------------------------------------------------------------ 建议

    def tick(
        self,
        at: datetime,
        readings: dict[str, SensorReading],
        weather: WeatherPoint,
        pressures: dict,
    ) -> AdvisorResult:
        """一个优化周期：更新现场快照并产出建议（不驱动设备）。"""
        aware(at, "周期时间")
        self._latest_readings = dict(readings)
        self._latest_weather = weather
        self._latest_pressures = dict(pressures)

        inp = AdvisorInputs(
            at=at,
            sensors=self.sensors,
            readings=readings,
            weather=weather,
            fans=self.fans,
            fan_states=self.bank.states_snapshot(),
            pressures=pressures,
            restrictions=self.restrictions,
            emergency_stopped=self.emergency_stopped,
            manual_isolation=dict(self.manual_isolation),
        )
        result = self.advisor.build(inp)
        self._recommendations[result.recommendation.id] = result.recommendation
        self.store.append(
            "recommendation_proposed",
            {"recommendation": result.recommendation.to_dict()},
            at=at,
        )
        return result

    # ------------------------------------------------------------------ 批准

    def approve(
        self, rec_id: str, actor: Actor, at: datetime, note: str = ""
    ) -> Recommendation:
        """有权限值班员确认建议。只确认，不下发（下发是 execute）。"""
        aware(at, "批准时间")
        rec = self._require_rec(rec_id)
        if not actor.can_approve():
            raise AuthorizationError(
                f"{actor.name}({actor.role.value}) 无权确认通风指令，需要值班员及以上权限"
            )
        if rec.status is not RecommendationStatus.PROPOSED:
            raise ControlRejected(f"建议 {rec_id} 当前状态 {rec.status.value}，不可确认")
        if at >= rec.valid_until:
            rec.status = RecommendationStatus.EXPIRED
            self.store.append(
                "recommendation_expired",
                {"recommendation_id": rec_id},
                at=at,
                actor=actor,
            )
            raise ControlRejected(f"建议 {rec_id} 已过有效期（{rec.valid_until.isoformat()}）")
        if rec.action not in (Action.START, Action.STOP):
            raise ControlRejected(
                f"建议 {rec_id} 动作为 {rec.action.value}，无可执行指令"
            )
        if self.emergency_stopped and rec.action is Action.START:
            raise ControlRejected("紧急停机锁定中，不能确认启动")

        rec.status = RecommendationStatus.APPROVED
        rec.decided_by = actor
        rec.decided_at = at
        rec.note = note
        self.store.append(
            "recommendation_approved",
            {"recommendation_id": rec_id, "note": note},
            at=at,
            actor=actor,
        )
        return rec

    def reject(self, rec_id: str, actor: Actor, at: datetime, note: str = "") -> None:
        rec = self._require_rec(rec_id)
        if not actor.can_approve():
            raise AuthorizationError("无权驳回建议")
        rec.status = RecommendationStatus.REJECTED
        rec.decided_by = actor
        rec.decided_at = at
        rec.note = note
        self.store.append(
            "recommendation_rejected",
            {"recommendation_id": rec_id, "note": note},
            at=at,
            actor=actor,
        )

    # ------------------------------------------------------------------ 执行

    def execute(self, rec_id: str, actor: Actor, at: datetime) -> list[CommandReceipt]:
        """对已批准建议下发指令；下发瞬间重跑安全门。返回各风机设备回执。"""
        aware(at, "下发时间")
        if self.gateway is None:
            raise ControlRejected("未配置设备网关，无法下发指令")
        rec = self._require_rec(rec_id)
        if not actor.can_approve():
            raise AuthorizationError("无权执行通风指令")
        if rec.status is not RecommendationStatus.APPROVED:
            raise ControlRejected(
                f"建议 {rec_id} 未处于已批准状态（当前 {rec.status.value}）"
            )

        # 执行前二次安全校验：使用**当前**现场快照与风机状态
        ctx = SafetyContext(
            at=at,
            weather=self._latest_weather,
            pressures=self._latest_pressures,
            restrictions=self.restrictions,
            fan_states=self.bank.states_snapshot(),
            emergency_stopped=self.emergency_stopped,
        )
        vetoes, blocked = safety_evaluate(ctx, self.fans)
        hard = [v for v in vetoes if v.severity.value == "block"]
        if rec.action is Action.START:
            if self.emergency_stopped:
                raise ControlRejected("紧急停机锁定中，禁止启动")
            target_blocked = [fid for fid in rec.fan_ids if fid in blocked]
            if hard or target_blocked:
                self.store.append(
                    "execute_blocked",
                    {
                        "recommendation_id": rec_id,
                        "vetoes": [v.to_dict() for v in hard],
                        "blocked_fans": sorted(set(rec.fan_ids) & blocked),
                    },
                    at=at,
                    actor=actor,
                )
                raise ControlRejected(
                    "下发瞬间安全校验未通过："
                    + "；".join(v.message for v in hard)
                )

        kind = CommandKind.START if rec.action is Action.START else CommandKind.STOP
        receipts: list[CommandReceipt] = []
        for fid in rec.fan_ids:
            state = self.bank.state_of(fid)
            if kind is CommandKind.START and state is FanRuntimeState.RUNNING:
                continue
            if kind is CommandKind.STOP and state is FanRuntimeState.STOPPED:
                continue
            cmd = IssuedCommand(
                id=f"CMD-{uuid.uuid4().hex[:8]}",
                fan_id=fid,
                kind=kind,
                issued_at=at,
                deadline=at + RECEIPT_TIMEOUT,
            )
            self.bank.issue(cmd)
            self.store.append(
                "command_issued",
                {
                    "command_id": cmd.id,
                    "recommendation_id": rec_id,
                    "fan_id": fid,
                    "kind": kind.value,
                },
                at=at,
                actor=actor,
            )
            receipt = self.gateway.send(cmd)
            self._absorb_receipt(receipt, rec_id)
            receipts.append(receipt)

        rec.status = RecommendationStatus.EXECUTED
        self.store.append(
            "recommendation_executed",
            {"recommendation_id": rec_id, "receipts": [r.to_dict() for r in receipts]},
            at=at,
            actor=actor,
        )
        if kind is CommandKind.START and receipts:
            trusted_temps = [
                r.temperature_c
                for sid, r in self._latest_readings.items()
                if sid not in self.manual_isolation and r.quality.value == "good"
            ]
            if trusted_temps:
                self._cycle_start_mean[rec_id] = sum(trusted_temps) / len(trusted_temps)
        return receipts

    def _absorb_receipt(self, receipt: CommandReceipt, rec_id: str) -> None:
        new_state = self.bank.apply_receipt(receipt)
        rt = self.bank.runtimes[receipt.fan_id]
        if new_state is FanRuntimeState.RUNNING and rt.started_at is None:
            rt.started_at = receipt.at
        if new_state in (FanRuntimeState.STOPPED, FanRuntimeState.FAULT):
            self._checkpoint(receipt.at, receipt.fan_id)
        self.store.append(
            "receipt_received",
            {
                "recommendation_id": rec_id,
                "command_id": receipt.command_id,
                "fan_id": receipt.fan_id,
                "result": receipt.result.value,
                "running": receipt.running,
                "message": receipt.message,
                "new_state": new_state.value,
            },
            at=receipt.at,
        )

    # ------------------------------------------------------------------ 急停

    def emergency_stop(self, actor: Actor, at: datetime) -> list[CommandReceipt]:
        """紧急人工停机：不等待优化周期，立即对全部非停机风机下发停机并锁定。"""
        aware(at, "急停时间")
        self.emergency_stopped = True
        self.store.append(
            "emergency_stop_triggered",
            {"actor": actor.to_dict()},
            at=at,
            actor=actor,
        )
        receipts: list[CommandReceipt] = []
        if self.gateway is not None:
            for fid, rt in self.bank.runtimes.items():
                if rt.state in (
                    FanRuntimeState.RUNNING,
                    FanRuntimeState.STARTING,
                    FanRuntimeState.PENDING_VERIFICATION,
                ):
                    cmd = IssuedCommand(
                        id=f"CMD-{uuid.uuid4().hex[:8]}",
                        fan_id=fid,
                        kind=CommandKind.STOP,
                        issued_at=at,
                        deadline=at,
                    )
                    self.bank.issue(cmd)
                    self.store.append(
                        "command_issued",
                        {
                            "command_id": cmd.id,
                            "recommendation_id": None,
                            "fan_id": fid,
                            "kind": "stop",
                            "emergency": True,
                        },
                        at=at,
                        actor=actor,
                    )
                    receipt = self.gateway.send(cmd)
                    self._absorb_receipt(receipt, rec_id="EMERGENCY")
                    receipts.append(receipt)
        return receipts

    def reset_emergency(self, actor: Actor, at: datetime) -> None:
        """急停复位：需质量经理/管理员，且所有风机已处于已证实停机或已核实。"""
        if actor.role not in (Role.QUALITY_MANAGER, Role.ADMIN):
            raise AuthorizationError("只有质量经理或管理员可复位急停")
        unresolved = [
            fid
            for fid, rt in self.bank.runtimes.items()
            if rt.state in (
                FanRuntimeState.STARTING,
                FanRuntimeState.STOPPING,
                FanRuntimeState.PENDING_VERIFICATION,
            )
        ]
        if unresolved:
            raise ControlRejected(
                f"以下风机尚未证实安全，不能复位急停：{', '.join(unresolved)}"
            )
        self.emergency_stopped = False
        self.store.append("emergency_stop_reset", {}, at=at, actor=actor)

    # ------------------------------------------------------------ 测点管理

    def isolate_sensor(
        self, actor: Actor, sensor_id: str, reason: str, at: datetime
    ) -> None:
        if actor.role is not Role.ADMIN:
            raise AuthorizationError("只有管理员可登记测点隔离")
        if not any(s.id == sensor_id for s in self.sensors):
            raise ValueError(f"未知测点 {sensor_id}")
        self.manual_isolation[sensor_id] = reason
        self.store.append(
            "sensor_isolated",
            {"sensor_id": sensor_id, "reason": reason},
            at=at,
            actor=actor,
        )

    def verify_pending(
        self, actor: Actor, fan_id: str, confirmed_running: bool, at: datetime
    ) -> FanRuntimeState:
        """现场人工核实待核实风机的真实启闭状态。"""
        if not actor.can_approve():
            raise AuthorizationError("无权登记现场核实结果")
        state = self.bank.resolve_pending(fan_id, confirmed_running, at)
        self.store.append(
            "pending_verified",
            {"fan_id": fan_id, "confirmed_running": confirmed_running, "new_state": state.value},
            at=at,
            actor=actor,
        )
        return state

    # ------------------------------------------------------------------ 复盘

    def _checkpoint(self, at: datetime, fan_id: str) -> float:
        """把自上次起点以来的运行段能耗并入累计。

        停机/故障后起点清空；仍在运行时把起点移到 ``at``，使下一次结算
        只统计新增段，避免重复计量。
        """
        rt = self.bank.runtimes[fan_id]
        if rt.started_at is None:
            return 0.0
        hours = max(0.0, (at - rt.started_at).total_seconds() / 3600.0)
        kwh = self.bank.by_id[fan_id].rated_kw * hours
        rt.run_kwh += kwh
        rt.started_at = at if rt.state is FanRuntimeState.RUNNING else None
        return kwh

    def close_cycle(
        self,
        rec_id: str,
        at: datetime,
        readings: dict[str, SensorReading],
        weather: WeatherPoint,
    ) -> CycleReport:
        """周期收尾：累计实际能耗、对比实际降温与预期。"""
        aware(at, "收尾时间")
        rec = self._require_rec(rec_id)
        rate = self.advisor.tariff.rate_at(at)

        # 对仍在运行的风机先结算到收尾时刻（停机风机的能耗已在回执时入账）
        for fid in rec.fan_ids:
            if self.bank.runtimes[fid].state is FanRuntimeState.RUNNING:
                self._checkpoint(at, fid)
        actual_kwh = round(sum(self.bank.runtimes[fid].run_kwh for fid in rec.fan_ids), 1)
        # 一次性周期：清零本轮累计，避免重复计入
        for fid in rec.fan_ids:
            self.bank.runtimes[fid].run_kwh = 0.0

        end_temps = [
            r.temperature_c
            for sid, r in readings.items()
            if sid not in self.manual_isolation and r.quality.value == "good"
        ]
        start_mean = self._cycle_start_mean.pop(rec_id, None)
        actual_delta = 0.0
        if start_mean is not None and end_temps:
            actual_delta = round(start_mean - sum(end_temps) / len(end_temps), 2)
        actual_cost = round(actual_kwh * rate, 2)

        deviation = {
            "delta_diff_c": round(actual_delta - rec.expected_delta_c, 2),
            "kwh_diff": round(actual_kwh - rec.expected_kwh, 1),
            "cost_diff": round(actual_cost - rec.expected_cost, 2),
            "kwh_per_degree": (
                round(actual_kwh / actual_delta, 1) if actual_delta > 0 else None
            ),
        }
        report = CycleReport(
            recommendation_id=rec_id,
            closed_at=at,
            expected_delta_c=rec.expected_delta_c,
            actual_delta_c=actual_delta,
            expected_kwh=rec.expected_kwh,
            actual_kwh=round(actual_kwh, 1),
            expected_cost=rec.expected_cost,
            actual_cost=actual_cost,
            deviation=deviation,
        )
        self.store.append(
            "cycle_closed",
            {"report": report.to_dict()},
            at=at,
        )
        return report

    # ------------------------------------------------------------------ 时间线

    def timeline(self, rec_id: str | None = None):
        return build_timeline(self.store.events, rec_id)

    # ------------------------------------------------------------------ 恢复

    def recover(self, at: datetime) -> list[str]:
        """从事件日志重放运行时状态。

        规则：重放全部回执；重启后凡**没有"ACK 且已停止"回执作为最终处置**的
        风机，一律置为待核实——包括运行中、指令无回执、NAK 故障的风机。
        空日志视为全新仓房，保持初始停机。
        """
        aware(at, "恢复时间")
        if not self.store.events:
            self.recovered = True
            return []

        # 轻量状态重放（权威状态仍以设备回执序列为准）
        emergency = False
        for ev in self.store.events:
            if ev.type == "emergency_stop_triggered":
                emergency = True
            elif ev.type == "emergency_stop_reset":
                emergency = False
            elif ev.type == "sensor_isolated":
                self.manual_isolation[ev.payload["sensor_id"]] = ev.payload["reason"]
            elif ev.type == "recommendation_proposed":
                d = ev.payload["recommendation"]
                # 仅登记 id/动作/状态用于查询，完整对象在其本周期内由 tick 持有
                self._recommendations[d["id"]] = Recommendation(
                    id=d["id"],
                    at=datetime.fromisoformat(d["at"]),
                    action=Action(d["action"]),
                    fan_ids=list(d["fan_ids"]),
                    evidence=[],
                    vetoes=[],
                    grain_dewpoint_c=d.get("grain_dewpoint_c"),
                    outside_dewpoint_c=d.get("outside_dewpoint_c"),
                    expected_delta_c=d.get("expected_delta_c", 0.0),
                    expected_kwh=d.get("expected_kwh", 0.0),
                    expected_cost=d.get("expected_cost", 0.0),
                    valid_until=datetime.fromisoformat(d["valid_until"]),
                    status=RecommendationStatus(d["status"]),
                )
            elif ev.type in ("recommendation_approved", "recommendation_rejected",
                             "recommendation_executed", "recommendation_expired"):
                rid = ev.payload["recommendation_id"]
                if rid in self._recommendations:
                    self._recommendations[rid].status = {
                        "recommendation_approved": RecommendationStatus.APPROVED,
                        "recommendation_rejected": RecommendationStatus.REJECTED,
                        "recommendation_executed": RecommendationStatus.EXECUTED,
                        "recommendation_expired": RecommendationStatus.EXPIRED,
                    }[ev.type]
        self.emergency_stopped = emergency

        # 以每台风机**最后一条设备回执**判定是否已证实关闭；
        # 只有"被系统触碰过"（发过指令/回过执）的风机才需要证明自己已停。
        touched: set[str] = set()
        last_receipt: dict[str, dict] = {}
        for ev in self.store.events:
            if ev.type == "command_issued":
                touched.add(ev.payload["fan_id"])
            elif ev.type == "receipt_received":
                touched.add(ev.payload["fan_id"])
                last_receipt[ev.payload["fan_id"]] = ev.payload
            elif ev.type == "pending_verified":
                touched.add(ev.payload["fan_id"])
                last_receipt[ev.payload["fan_id"]] = {
                    "result": "ack",
                    "running": ev.payload["confirmed_running"],
                }

        proven_off = {
            fid
            for fid, p in last_receipt.items()
            if p.get("result") == "ack" and p.get("running") is False
        }
        # 发过指令但拿不出"停机到位"证据的，全部待核实
        unproven = touched - proven_off
        # 新进程内存中风机全是初始 STOPPED，但那不是证据：凡日志无法证明
        # "已收到停机到位回执"的风机，一律强制置为待核实。
        marked = self.bank.mark_pending_verification(at, unproven, force=True)
        self.recovered = True
        if marked:
            self.store.append(
                "recovery_pending",
                {
                    "pending_fans": sorted(marked),
                    "rule": "无已证实关闭回执的风机一律待人工核实",
                },
                at=at,
            )
        return marked

    def recovery_summary(self) -> dict:
        return summarize_recovery(self.store.events)

    # ------------------------------------------------------------------ 内部

    def _require_rec(self, rec_id: str) -> Recommendation:
        rec = self._recommendations.get(rec_id)
        if rec is None:
            raise KeyError(f"未知建议 {rec_id}")
        return rec

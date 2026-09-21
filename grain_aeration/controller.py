"""控制闭环：建议 -> 有权限值班员确认 -> 指令 -> 设备回执。

关键约束：

- 建议必须在有效期内、且批准时数据未失效（StaleDataError）；
- 硬门禁（FORBIDDEN/STOP）不可被任何角色强制覆盖；
- 执行前对互锁与门禁做最后复检（防止批准后条件变化）；
- 紧急人工停机 emergency_stop 立即下发 FORCE_STOP_ALL，
  不等待优化周期、不需要批准，但仍记录事件与回执；
- 崩溃重启后风机为 UNVERIFIED 时，必须先现场核实，不允许直接启动。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from .advisor import Advisor, AdvisorContext
from .config import DomainConfig, SafetyConfig
from .models import (
    Action,
    Approval,
    ApprovalDecision,
    Command,
    CommandVerb,
    DuctPressure,
    FanState,
    FanStatusReceipt,
    Recommendation,
    SensorReading,
    SessionSummary,
    User,
    WeatherSnapshot,
)
from .persistence import JsonlEventStore, RecoveredState, recover_state
from .tariff import TimeOfUseTariff


class AuthorizationError(PermissionError):
    """值班员无相应权限。"""


class StaleDataError(RuntimeError):
    """建议过期或其依据数据已失效。"""


class SafetyViolation(RuntimeError):
    """批准后复检发现硬门禁/互锁冲突，拒绝下发。"""


class DeviceGateway(Protocol):
    """设备网关协议：下发指令并返回设备回执。"""

    def send(self, command: Command) -> FanStatusReceipt: ...


@dataclass
class _FanRuntime:
    state: FanState = FanState.STOPPED
    running_since: datetime | None = None
    started_by_rec: str | None = None
    rated_kw: float = 0.0


class AerationController:
    def __init__(
        self,
        domain: DomainConfig,
        store: JsonlEventStore,
        gateway: DeviceGateway,
        tariff: TimeOfUseTariff | None = None,
        safety: SafetyConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.domain = domain
        self.store = store
        self.gateway = gateway
        self.tariff = tariff or TimeOfUseTariff.default()
        self.safety = safety or SafetyConfig()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.advisor = Advisor(domain, tariff, self.safety)

        self._runtime: dict[str, _FanRuntime] = {
            f.id: _FanRuntime() for f in domain.fans
        }
        self._last_recommendation: Recommendation | None = None
        self._meter_kwh: float = 0.0
        self._recover()

    # ------------------------------------------------------------------ #
    def _recover(self) -> None:
        """进程启动：重放日志，未证实关闭的风机进入待核实。"""
        recovered = recover_state(self.store, [f.id for f in self.domain.fans])
        for fid, state in recovered.fan_states.items():
            self._runtime[fid].state = state
        self._recovered: RecoveredState = recovered

    @property
    def recovered(self) -> RecoveredState:
        return self._recovered

    @property
    def unverified_fans(self) -> list[str]:
        return [fid for fid, rt in self._runtime.items() if rt.state is FanState.UNVERIFIED]

    def fan_state(self, fan_id: str) -> FanState:
        return self._runtime[fan_id].state

    def acknowledge_fan_state(self, fan_id: str, running: bool, by: User) -> FanStatusReceipt:
        """现场核实待核实风机后，补录一条确认回执，解除 UNVERIFIED。"""
        if not by.can_approve():
            raise AuthorizationError(f"{by.username} 无权核实风机状态")
        rt = self._runtime[fan_id]
        receipt = FanStatusReceipt(
            command_id=f"ACK-{uuid.uuid4().hex[:8]}",
            fan_id=fan_id,
            at=self._clock(),
            running=running,
            confirmed=True,
            message="现场人工核实",
        )
        self.store.log_receipt(receipt)
        rt.state = FanState.RUNNING if running else FanState.STOPPED
        return receipt

    # ------------------------------------------------------------------ #
    def advise(
        self,
        readings: list[SensorReading],
        weather: WeatherSnapshot,
        pressures: list[DuctPressure],
    ) -> Recommendation:
        running = frozenset(
            fid for fid, rt in self._runtime.items() if rt.state is FanState.RUNNING
        )
        ctx = AdvisorContext(
            at=self._clock(),
            readings=tuple(readings),
            weather=weather,
            pressures=tuple(pressures),
            running_fan_ids=running,
        )
        rec = self.advisor.evaluate(ctx)
        self.store.log_recommendation(rec)
        self._last_recommendation = rec
        return rec

    # ------------------------------------------------------------------ #
    def approve(
        self,
        rec: Recommendation,
        by: User,
        decision: ApprovalDecision,
        readings: list[SensorReading] | None = None,
        weather: WeatherSnapshot | None = None,
        pressures: list[DuctPressure] | None = None,
        note: str = "",
    ) -> list[FanStatusReceipt]:
        """值班员确认建议。APPROVE 通过后立即复检并下发指令。"""
        if not by.can_approve():
            raise AuthorizationError(
                f"{by.username}（{by.role}）无权批准通风指令，"
                "需质量经理或管理员确认"
            )

        now = self._clock()
        # 有效期校验
        if now > rec.expires_at:
            self.store.log_approval(None, rec)
            raise StaleDataError(
                f"建议 {rec.rec_id} 已于 {rec.expires_at:%H:%M:%S} 过期，"
                "请基于最新数据重新生成"
            )

        # 硬门禁拦截启动方向：
        # - FORBIDDEN：任何批准/强制都无效；
        # - 含启动计划且依据中存在 blocks_start：同样拒绝。
        # STOP 是安全方向（运行中出现风险时的建议），始终允许批准执行。
        has_start_plan = any(p.start for p in rec.fan_plans)
        if decision in (ApprovalDecision.APPROVE, ApprovalDecision.FORCE_OVERRIDE):
            if rec.action is Action.FORBIDDEN or (has_start_plan and rec.blocked):
                raise SafetyViolation(
                    f"建议 {rec.rec_id} 含硬门禁依据，禁止启动，人工强制无效"
                )

        if decision is ApprovalDecision.FORCE_OVERRIDE:
            if rec.action.value not in self.safety.force_allowed_actions:
                raise SafetyViolation(
                    f"动作 {rec.action.value} 不允许人工强制覆盖"
                )

        approval = Approval(
            rec_id=rec.rec_id,
            decision=decision,
            approver=by,
            at=now,
            note=note,
        )
        self.store.log_approval(approval)

        if decision is ApprovalDecision.REJECT:
            return []

        dispatch_rec = rec
        if decision is ApprovalDecision.FORCE_OVERRIDE:
            # 软建议（如 DEFER）本身不带风机计划：按最新现场数据生成强制启动计划，
            # 但安全门禁与互锁仍然不可绕过
            if readings is None or weather is None or pressures is None:
                raise ValueError("强制覆盖必须附带最新传感器/气象/压力数据")
            plans, hard_blocks = self.advisor.forced_start_plans(
                AdvisorContext(
                    at=now,
                    readings=tuple(readings),
                    weather=weather,
                    pressures=tuple(pressures),
                    running_fan_ids=frozenset(
                        fid for fid, rt in self._runtime.items()
                        if rt.state is FanState.RUNNING
                    ),
                )
            )
            if hard_blocks:
                raise SafetyViolation(
                    "强制覆盖触发硬门禁（"
                    + "；".join(e.message for e in hard_blocks)
                    + "），指令已拦截"
                )
            from dataclasses import replace

            dispatch_rec = replace(rec, action=Action.START, fan_plans=plans)
        elif readings is not None and weather is not None and pressures is not None:
            # 批准后、下发前最后复检：用最新数据再算一遍
            fresh = self.advisor.evaluate(
                AdvisorContext(
                    at=now,
                    readings=tuple(readings),
                    weather=weather,
                    pressures=tuple(pressures),
                    running_fan_ids=frozenset(
                        fid for fid, rt in self._runtime.items()
                        if rt.state is FanState.RUNNING
                    ),
                )
            )
            fresh_starts = any(p.start for p in fresh.fan_plans)
            # 原批准想启动，但复检出现门禁：拦截
            if has_start_plan and fresh.blocked:
                raise SafetyViolation(
                    "批准后复检触发硬门禁（"
                    + "；".join(e.message for e in fresh.reasons if e.blocks_start)
                    + "），指令已拦截"
                )
            # 条件在批准后转差（如降温收益消失）：按最新建议执行停机/保持
            if fresh.action in (Action.STOP, Action.HOLD) and not fresh_starts:
                dispatch_rec = fresh

        return self._dispatch(dispatch_rec, by)

    # ------------------------------------------------------------------ #
    def _dispatch(self, rec: Recommendation, by: User) -> list[FanStatusReceipt]:
        receipts: list[FanStatusReceipt] = []
        for plan in rec.fan_plans:
            rt = self._runtime[plan.fan_id]
            if rt.state is FanState.UNVERIFIED:
                raise SafetyViolation(
                    f"风机 {plan.fan_id} 处于待核实状态，必须先现场确认，"
                    "不得仅凭软件状态启动"
                )
            verb = CommandVerb.START_FAN if plan.start else CommandVerb.STOP_FAN
            if verb is CommandVerb.START_FAN and rt.state is FanState.RUNNING:
                continue
            if verb is CommandVerb.STOP_FAN and rt.state is not FanState.RUNNING:
                continue
            # 互锁最后防线
            if verb is CommandVerb.START_FAN:
                self._assert_interlock_clear(plan.interlock_group, plan.fan_id)

            cmd = Command(
                command_id=f"CMD-{uuid.uuid4().hex[:12]}",
                rec_id=rec.rec_id,
                verb=verb,
                fan_id=plan.fan_id,
                issued_at=self._clock(),
                issued_by=by.username,
                interlock_group=plan.interlock_group,
                expected_rated_kw=plan.rated_kw,
            )
            self.store.log_command(cmd)
            receipt = self.gateway.send(cmd)
            self.store.log_receipt(receipt)
            self._apply_receipt(receipt, plan.rated_kw)
            receipts.append(receipt)
        return receipts

    def _assert_interlock_clear(self, group: str, fan_id: str) -> None:
        for f in self.domain.fans:
            if f.interlock_group == group and f.id != fan_id:
                if self._runtime[f.id].state is FanState.RUNNING:
                    raise SafetyViolation(
                        f"互锁冲突：同风道 {group} 的 {f.id} 正在运行，"
                        f"禁止启动 {fan_id}"
                    )

    def _apply_receipt(self, receipt: FanStatusReceipt, rated_kw: float) -> None:
        if not receipt.confirmed:
            # 设备未确认：保持待核实，绝不假定成功
            self._runtime[receipt.fan_id].state = FanState.UNVERIFIED
            return
        rt = self._runtime[receipt.fan_id]
        rt.rated_kw = rated_kw
        if receipt.running:
            rt.state = FanState.RUNNING
            rt.running_since = receipt.at
        else:
            if rt.state is FanState.RUNNING and rt.running_since is not None:
                energy, _ = self.tariff.cost_for_run(
                    rated_kw, rt.running_since, receipt.at
                )
                self._meter_kwh += energy
            rt.state = FanState.STOPPED
            rt.running_since = None

    # ------------------------------------------------------------------ #
    def close_session(
        self,
        rec_id: str,
        cold_end_before_c: float,
        cold_end_after_c: float,
    ) -> SessionSummary:
        """一次通风控制结束后，按已确认回执结算实际时长/能耗/电费。

        实际能耗不是额定估算，而是按每台风机“启动确认回执 -> 停机确认回执”
        之间的运行时长在分时电价上积分。停机可能由后续的 STOP 建议或
        紧急停机触发，因此会话边界取自实际运行区间，而非单一建议。
        """
        runs, rated = self._replay_runs(rec_id)
        energy = cost = duration_h = 0.0
        fan_ids: list[str] = []
        for fid, intervals in runs.items():
            if intervals:
                fan_ids.append(fid)
            kw = rated.get(fid, 0.0)
            for began, ended in intervals:
                e, c = self.tariff.cost_for_run(kw, began, ended)
                energy += e
                cost += c
                duration_h += (ended - began).total_seconds() / 3600.0
        fan_ids.sort()
        all_bounds = [b for intervals in runs.values() for pair in intervals for b in pair]
        if not all_bounds:
            raise ValueError(f"no confirmed run intervals for rec {rec_id}")
        started_at, ended_at = min(all_bounds), max(all_bounds)
        summary = SessionSummary(
            rec_id=rec_id,
            fan_ids=tuple(fan_ids),
            started_at=started_at,
            ended_at=ended_at,
            actual_duration_h=round(duration_h, 3),
            actual_energy_kwh=round(energy, 3),
            actual_cost=round(cost, 3),
            cold_end_before_c=cold_end_before_c,
            cold_end_after_c=cold_end_after_c,
        )
        self.store.log_session_summary(summary)
        return summary

    def _replay_runs(
        self, rec_id: str
    ) -> tuple[dict[str, list[tuple[datetime, datetime]]], dict[str, float]]:
        """重放事件，配对该建议启动的每段运行（任何后续停机回执均可闭合）。"""
        from collections import defaultdict

        start_cmds: dict[str, str] = {}  # command_id -> fan_id
        rated: dict[str, float] = {}
        for ev in self.store.read_all():
            if ev.kind == "command" and ev.payload.get("rec_id") == rec_id:
                p = ev.payload
                if p.get("verb") == CommandVerb.START_FAN.value and p.get("fan_id"):
                    start_cmds[p["command_id"]] = p["fan_id"]
                    rated[p["fan_id"]] = float(p.get("expected_rated_kw") or 0.0)

        runs: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        open_run: dict[str, tuple[str, datetime]] = {}
        for ev in self.store.read_all():
            if ev.kind != "receipt":
                continue
            p = ev.payload
            fid = p["fan_id"]
            if p.get("confirmed") and p.get("running") is True:
                if p["command_id"] in start_cmds:
                    open_run[fid] = (p["command_id"], ev.at)
            elif p.get("confirmed") and p.get("running") is False:
                if fid in open_run:
                    _, began = open_run.pop(fid)
                    if ev.at > began:
                        runs[fid].append((began, ev.at))
        return dict(runs), rated

    # ------------------------------------------------------------------ #
    def emergency_stop(self, by: User, reason: str) -> list[FanStatusReceipt]:
        """紧急人工停机：立即、无条件、不等优化周期。

        即使调用者无普通批准权限也可执行（任何在岗值班员）。
        硬停机指令与每台风机的停机回执同样进入审计时间线。
        """
        if not by.can_emergency_stop():
            raise AuthorizationError(f"{by.username} 无权执行紧急停机")
        now = self._clock()
        cmd = Command(
            command_id=f"CMD-{uuid.uuid4().hex[:12]}",
            rec_id=None,
            verb=CommandVerb.FORCE_STOP_ALL,
            fan_id=None,
            issued_at=now,
            issued_by=by.username,
        )
        self.store.append(
            "emergency_stop",
            {"command_id": cmd.command_id, "issued_by": by.username, "reason": reason,
             "at": now.isoformat()},
            at=now,
        )
        self.store.log_command(cmd)

        receipts: list[FanStatusReceipt] = []
        for f in self.domain.fans:
            rt = self._runtime[f.id]
            if rt.state is FanState.STOPPED:
                continue
            receipt = self.gateway.send(
                Command(
                    command_id=cmd.command_id,
                    rec_id=None,
                    verb=CommandVerb.FORCE_STOP_ALL,
                    fan_id=f.id,
                    issued_at=now,
                    issued_by=by.username,
                    interlock_group=f.interlock_group,
                    expected_rated_kw=f.rated_kw,
                )
            )
            self.store.log_receipt(receipt)
            self._apply_receipt(receipt, f.rated_kw)
            receipts.append(receipt)
        return receipts

    @property
    def meter_kwh(self) -> float:
        return self._meter_kwh

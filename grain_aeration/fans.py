"""风机状态机与现场设备网关。

状态迁移：
    STOPPED ──start──▶ STARTING ──ACK(running)──▶ RUNNING
                        │  └─NAK/TIMEOUT────────▶ FAULT
    RUNNING ──stop───▶ STOPPING ──ACK(stopped)──▶ STOPPED
                        └─NAK/TIMEOUT──────────▶ FAULT

关键保守语义：
- 只有收到设备 ACK 且 ``running=False`` 才能进入 STOPPED；
- 进程重启后，凡不能证明"已关闭"的风机一律进入 PENDING_VERIFICATION，
  绝不能默认当作安全停机；待现场人工核实后再收敛到 RUNNING/STOPPED。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .models import (
    CommandReceipt,
    Fan,
    FanRuntimeState,
    ReceiptResult,
    aware,
)

RECEIPT_TIMEOUT = timedelta(seconds=30)


class CommandKind(str, Enum):
    START = "start"
    STOP = "stop"


@dataclass
class IssuedCommand:
    id: str
    fan_id: str
    kind: CommandKind
    issued_at: datetime
    deadline: datetime


@dataclass
class FanRuntime:
    state: FanRuntimeState = FanRuntimeState.STOPPED
    last_command: IssuedCommand | None = None
    last_receipt: CommandReceipt | None = None
    started_at: datetime | None = None        # 本轮启动时间（用于累计能耗）
    run_kwh: float = 0.0                      # 累计运行能耗（由控制器写入）
    verified_pending_since: datetime | None = None  # 进入待核实的时间


class DeviceGateway:
    """现场设备网关抽象。

    生产环境替换为 PLC/Modbus 实现；此处提供可注入故障的内存模拟，
    用于演练 NAK 与超时路径。
    """

    def send(self, cmd: IssuedCommand) -> CommandReceipt:  # pragma: no cover - 接口
        raise NotImplementedError

    def poll(self, fan_id: str, at: datetime) -> CommandReceipt | None:  # pragma: no cover
        raise NotImplementedError


class SimulatedGateway(DeviceGateway):
    def __init__(
        self,
        fail_fans: set[str] | None = None,
        seed: int = 7,
        latency_s: float = 2.0,
    ) -> None:
        self._fail_fans = fail_fans or set()
        self._latency = timedelta(seconds=latency_s)
        self._pending: dict[str, tuple[IssuedCommand, bool]] = {}
        self._rng = random.Random(seed)

    def send(self, cmd: IssuedCommand) -> CommandReceipt:
        fail = cmd.fan_id in self._fail_fans
        self._pending[cmd.fan_id] = (cmd, fail)
        # 模拟设备立即产生回执（故障设备回 NAK），真实实现可能延迟
        if fail:
            return CommandReceipt(
                command_id=cmd.id,
                fan_id=cmd.fan_id,
                at=cmd.issued_at + self._latency,
                result=ReceiptResult.NAK,
                running=None,
                message="设备拒动作：变频器故障",
            )
        running = cmd.kind is CommandKind.START
        return CommandReceipt(
            command_id=cmd.id,
            fan_id=cmd.fan_id,
            at=cmd.issued_at + self._latency,
            result=ReceiptResult.ACK,
            running=running,
            message="到位" if running else "已停机",
        )

    def poll(self, fan_id: str, at: datetime) -> CommandReceipt | None:
        item = self._pending.get(fan_id)
        if not item:
            return None
        cmd, fail = item
        if at < cmd.issued_at + self._latency:
            return None
        if fail:
            return CommandReceipt(
                command_id=cmd.id,
                fan_id=fan_id,
                at=at,
                result=ReceiptResult.NAK,
                running=None,
                message="设备拒动作：变频器故障",
            )
        running = cmd.kind is CommandKind.START
        return CommandReceipt(
            command_id=cmd.id,
            fan_id=fan_id,
            at=at,
            result=ReceiptResult.ACK,
            running=running,
            message="到位" if running else "已停机",
        )


@dataclass
class FanBank:
    """仓房全部风机的运行时状态集合。"""

    fans: list[Fan]
    runtimes: dict[str, FanRuntime] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for f in self.fans:
            self.runtimes.setdefault(f.id, FanRuntime())

    @property
    def by_id(self) -> dict[str, Fan]:
        return {f.id: f for f in self.fans}

    def state_of(self, fan_id: str) -> FanRuntimeState:
        return self.runtimes[fan_id].state

    def states_snapshot(self) -> dict[str, FanRuntimeState]:
        return {fid: rt.state for fid, rt in self.runtimes.items()}

    def issue(self, cmd: IssuedCommand) -> None:
        rt = self.runtimes[cmd.fan_id]
        rt.last_command = cmd
        rt.state = (
            FanRuntimeState.STARTING
            if cmd.kind is CommandKind.START
            else FanRuntimeState.STOPPING
        )

    def apply_receipt(self, receipt: CommandReceipt) -> FanRuntimeState:
        """按设备回执收敛状态。只有 ACK+running=False 才是已证实停机。"""
        rt = self.runtimes[receipt.fan_id]
        rt.last_receipt = receipt
        if receipt.result is ReceiptResult.ACK and receipt.running is True:
            rt.state = FanRuntimeState.RUNNING
        elif receipt.result is ReceiptResult.ACK and receipt.running is False:
            rt.state = FanRuntimeState.STOPPED
            rt.started_at = None
        else:  # NAK / TIMEOUT
            rt.state = FanRuntimeState.FAULT
        return rt.state

    def mark_pending_verification(
        self, at: datetime, fan_ids: set[str] | None = None, force: bool = False
    ) -> list[str]:
        """重启恢复：未证实关闭的风机进入待核实。返回被标记的风机。

        ``force=True`` 用于恢复场景：即使内存状态机显示 STOPPED（新进程的
        初始值），只要事件日志无法证明该机已关闭，也必须强制置为待核实。
        """
        marked: list[str] = []
        targets = set(self.runtimes) if fan_ids is None else fan_ids
        for fid in targets:
            rt = self.runtimes[fid]
            if force or rt.state is not FanRuntimeState.STOPPED:
                rt.state = FanRuntimeState.PENDING_VERIFICATION
                rt.verified_pending_since = at
                rt.last_command = None
                marked.append(fid)
        return marked

    def resolve_pending(self, fan_id: str, confirmed_running: bool, at: datetime) -> FanRuntimeState:
        """人工现场核实待核实风机：确认真在转→RUNNING；确认停止→STOPPED。"""
        rt = self.runtimes[fan_id]
        aware(at, "核实时间")
        if rt.state is not FanRuntimeState.PENDING_VERIFICATION:
            raise ValueError(f"风机 {fan_id} 当前不处于待核实状态")
        rt.state = FanRuntimeState.RUNNING if confirmed_running else FanRuntimeState.STOPPED
        if not confirmed_running:
            rt.started_at = None
        rt.verified_pending_since = None
        return rt.state

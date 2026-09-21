"""设备侧适配。

生产环境实现与 PLC/网关的通信；这里提供一个内存模拟实现，
用于联调与测试。关键约定：只有 confirmed 的回执才能改变控制器状态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .models import Command, CommandVerb, FanStatusReceipt


class SimulatedFanGateway:
    """记录每台风机实际运行状态的模拟网关。

    - fail_command_ids 中的指令返回 confirmed=False（设备未确认）；
    - clock 用于生成回执时间戳。
    """

    def __init__(
        self,
        fan_ids: list[str],
        clock: Callable[[], datetime] | None = None,
        fail_command_ids: set[str] | None = None,
    ):
        self._running = {fid: False for fid in fan_ids}
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._fail = fail_command_ids or set()
        self.sent: list[Command] = []

    def is_running(self, fan_id: str) -> bool:
        return self._running.get(fan_id, False)

    def send(self, command: Command) -> FanStatusReceipt:
        self.sent.append(command)
        fid = command.fan_id
        if fid is None:
            raise ValueError("group command must be expanded before reaching the gateway")

        now = self._clock()
        if command.command_id in self._fail:
            # 设备无效应答：状态未知，控制器必须保持待核实
            return FanStatusReceipt(
                command_id=command.command_id,
                fan_id=fid,
                at=now,
                running=self._running.get(fid, False),
                confirmed=False,
                message="设备应答超时",
            )

        if command.verb in (CommandVerb.START_FAN,):
            self._running[fid] = True
            running = True
            message = "启动确认"
        else:  # STOP_FAN / FORCE_STOP_ALL
            self._running[fid] = False
            running = False
            message = "停机确认" if command.verb is CommandVerb.STOP_FAN else "紧急停机确认"

        return FanStatusReceipt(
            command_id=command.command_id,
            fan_id=fid,
            at=now,
            running=running,
            confirmed=True,
            message=message,
        )

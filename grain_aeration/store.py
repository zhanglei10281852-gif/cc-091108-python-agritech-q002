"""仅追加（append-only）事件审计日志与重放恢复。

每一次"建议、批准、指令、回执、急停、隔离、待核实、周期收尾"都序列化为
一条不可变事件，落盘 JSONL。质量经理的时间线回放与重启恢复都以该日志为唯一事实源。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .models import Actor, aware


@dataclass(frozen=True)
class Event:
    seq: int
    id: str
    at: datetime
    type: str
    payload: dict[str, Any]
    actor: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "id": self.id,
            "at": self.at.isoformat(),
            "type": self.type,
            "payload": self.payload,
            "actor": self.actor,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Event":
        return Event(
            seq=d["seq"],
            id=d["id"],
            at=datetime.fromisoformat(d["at"]),
            type=d["type"],
            payload=d["payload"],
            actor=d.get("actor"),
        )


class EventStore:
    """内存日志 + 可选 JSONL 持久化。追加即 fsync，保证崩溃后日志不丢尾部意图。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._events: list[Event] = []
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(Event.from_dict(json.loads(line)))

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def append(
        self,
        type_: str,
        payload: dict[str, Any],
        at: datetime,
        actor: Actor | None = None,
    ) -> Event:
        aware(at, "事件时间")
        event = Event(
            seq=len(self._events) + 1,
            id=str(uuid.uuid4()),
            at=at,
            type=type_,
            payload=payload,
            actor=actor.to_dict() if actor else None,
        )
        self._events.append(event)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return event

    def since(self, seq: int) -> list[Event]:
        return [e for e in self._events if e.seq > seq]

    def filter(self, predicate: Callable[[Event], bool]) -> list[Event]:
        return [e for e in self._events if predicate(e)]

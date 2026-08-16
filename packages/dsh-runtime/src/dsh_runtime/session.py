"""BotSession 与定时调度。

Session 把 Profile（能力边界）、Memory（记忆）、EventLog（事件）和
插件工具绑在一起，Agent 的每个 tick 都在 Session 上下文中运行。
调度器只做固定间隔的主动运行，不做并发——单 Bot 串行 tick 即可，
避免同一信号被并发处理两次。
"""

import time
from dataclasses import dataclass
from typing import Protocol

from dsh_runtime.profile import Profile
from dsh_runtime.store import EventLog, Memory
from dsh_runtime.tasks import TaskStore


class Agent(Protocol):
    """插件 Agent 接口：实现 tick 即可被 DSH 调度。"""

    name: str

    def tick(self, session: "BotSession") -> None: ...


@dataclass
class BotSession:
    profile: Profile
    memory: Memory
    events: EventLog
    tasks: TaskStore

    @classmethod
    def for_profile(cls, profile: Profile) -> "BotSession":
        return cls(
            profile=profile,
            memory=Memory(bot=profile.name),
            events=EventLog(),
            tasks=TaskStore(bot=profile.name),
        )

    def use(self, tool: str) -> None:
        """插件调用工具前的能力检查：未声明或被禁止即抛错。"""
        self.profile.allow(tool)


def run_once(session: BotSession, agent: Agent) -> None:
    """执行一个 tick 并记录调度事件。tick 内异常不外抛——
    DSH 故障不能影响量化系统，也绝不能带着异常继续下一轮。"""
    session.events.emit(
        "bot/tick.started", session.profile.market, "system", agent.name, {}
    )
    try:
        agent.tick(session)
    except Exception as exc:
        session.memory.remember(
            f"tick failed: {exc}", kind="error", tags=["tick-failed"]
        )
        session.events.emit(
            "bot/tick.failed", session.profile.market, "system", agent.name,
            {"error": str(exc)},
        )
    finally:
        session.events.emit(
            "bot/tick.finished", session.profile.market, "system", agent.name, {}
        )


def run_forever(session: BotSession, agent: Agent, interval_seconds: float) -> None:
    """定时主动运行。Ctrl-C 停止。"""
    while True:
        run_once(session, agent)
        time.sleep(interval_seconds)

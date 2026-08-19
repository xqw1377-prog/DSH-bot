"""Bot Profile 加载。

Profile 是 Bot 的静态能力声明：能用什么工具、禁止什么动作。
运行时据此做能力检查——插件调用未声明工具即拒绝（失败关闭）。
"""

from dataclasses import dataclass
from pathlib import Path

import yaml


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    market: str
    primary_tools: frozenset[str]
    prohibited: frozenset[str]

    def allow(self, tool: str) -> None:
        if tool in self.prohibited:
            raise ProfileError(f"{self.name}: tool '{tool}' is prohibited by profile")
        if tool not in self.primary_tools:
            raise ProfileError(
                f"{self.name}: tool '{tool}' not declared in primary_tools"
            )


def load_profile(path: str | Path) -> Profile:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    required = {"name", "market", "primary_tools", "prohibited"}
    missing = required - set(raw or {})
    if missing:
        raise ProfileError(f"profile {path}: missing fields {sorted(missing)}")
    return Profile(
        name=raw["name"],
        description=raw.get("description", ""),
        market=raw["market"],
        primary_tools=frozenset(raw["primary_tools"]),
        prohibited=frozenset(raw["prohibited"]),
    )

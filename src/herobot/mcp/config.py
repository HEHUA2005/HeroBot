from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    enabled: bool = True
    required: bool = True
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    exposed_tools: list[str] = field(default_factory=list)
    hidden_tools: list[str] = field(default_factory=list)

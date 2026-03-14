"""
Reads Claude's ~/.claude.json and extracts MCP server configurations.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class ServerConfig:
    name: str
    transport: Literal["stdio", "http"]
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""  # for HTTP servers


def get_claude_config_path() -> Path:
    return Path.home() / ".claude.json"


def load_server_configs(skip_servers: set[str] | None = None) -> dict[str, ServerConfig]:
    """
    Load all MCP server configs from ~/.claude.json.
    Returns dict keyed by server name.
    """
    config_path = get_claude_config_path()
    if not config_path.exists():
        return {}

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)

    servers: dict[str, ServerConfig] = {}
    skip = skip_servers or set()

    raw_servers: dict = data.get("mcpServers", {})

    for name, cfg in raw_servers.items():
        if name in skip:
            continue

        transport = cfg.get("type", "stdio")

        if transport == "http":
            servers[name] = ServerConfig(
                name=name,
                transport="http",
                url=cfg.get("url", ""),
                env=cfg.get("env", {}),
            )
        else:
            servers[name] = ServerConfig(
                name=name,
                transport="stdio",
                command=cfg.get("command", ""),
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
            )

    return servers


def server_name_to_py(name: str) -> str:
    """Convert server name to a valid Python identifier."""
    return name.replace("-", "_").replace(".", "_").replace(" ", "_")

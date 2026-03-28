"""
Reads Claude's ~/.claude.json and extracts MCP server configurations.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


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


def get_project_config_path() -> Path:
    return Path.cwd() / ".claude" / "settings.json"


def _parse_servers(
    data: dict,
    skip: set[str],
    servers: dict[str, ServerConfig],
) -> None:
    """Parse mcpServers from a config dict into servers. Skips already-present names."""
    raw_servers: dict = data.get("mcpServers", {})

    for name, cfg in raw_servers.items():
        if name in skip or name in servers:
            continue

        if cfg.get("disabled", False):
            continue

        transport = cfg.get("type", "stdio")

        if transport == "http" and not cfg.get("env"):
            logger.info(f"Skipping HTTP server '{name}' (no auth headers)")
            continue

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


def load_server_configs(skip_servers: set[str] | None = None) -> dict[str, ServerConfig]:
    """
    Load MCP server configs from ~/.claude.json (global) and .claude/settings.json (project).
    Global servers take priority — project servers only add new names.
    """
    skip = skip_servers or set()
    servers: dict[str, ServerConfig] = {}

    # 1. Global config first (highest priority)
    global_path = get_claude_config_path()
    if global_path.exists():
        with open(global_path, encoding="utf-8") as f:
            _parse_servers(json.load(f), skip, servers)

    # 2. Project config — only adds servers not already present
    project_path = get_project_config_path()
    if project_path.exists():
        with open(project_path, encoding="utf-8") as f:
            project_data = json.load(f)
        before = set(servers)
        _parse_servers(project_data, skip, servers)
        added = set(servers) - before
        if added:
            logger.info(f"Added project-level servers: {sorted(added)}")

    return servers


def server_name_to_py(name: str) -> str:
    """Convert server name to a valid Python identifier."""
    return name.replace("-", "_").replace(".", "_").replace(" ", "_")

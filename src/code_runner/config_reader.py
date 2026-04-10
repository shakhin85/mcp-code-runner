"""
Reads Claude's ~/.claude.json and extracts MCP server configurations.

Supports:
- Global servers from ~/.claude.json
- Project servers from .claude/settings.json AND .mcp.json
- Auto-detection of project dir via CLAUDE_PROJECT_DIR env var
  or by walking the process tree on Linux (/proc)
"""

import json
import logging
import os
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


def _detect_project_dir() -> Path:
    """Detect the Claude Code session project directory.

    Priority:
    1. CLAUDE_PROJECT_DIR env var (explicit override)
    2. Walk process tree on Linux — find first ancestor whose cwd
       contains .claude/settings.json or .mcp.json
    3. Fall back to Path.cwd()
    """
    if env_dir := os.environ.get("CLAUDE_PROJECT_DIR"):
        path = Path(env_dir)
        if path.is_dir():
            logger.info(f"Project dir from CLAUDE_PROJECT_DIR: {path}")
            return path

    # Walk process tree on Linux
    try:
        pid = os.getpid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            proc_cwd = Path(f"/proc/{pid}/cwd").resolve()
            if (
                (proc_cwd / ".claude" / "settings.json").exists()
                or (proc_cwd / ".mcp.json").exists()
            ):
                logger.info(f"Project dir detected from pid {pid}: {proc_cwd}")
                return proc_cwd
            # Read parent PID from /proc/pid/status
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("PPid:"):
                    pid = int(line.split()[1])
                    break
            else:
                break
    except (OSError, ValueError):
        pass

    fallback = Path.cwd()
    logger.debug(f"Project dir fallback to cwd: {fallback}")
    return fallback


def _get_project_config_paths() -> list[Path]:
    """Return paths to project-level MCP config files."""
    project_dir = _detect_project_dir()
    return [
        project_dir / ".claude" / "settings.json",
        project_dir / ".mcp.json",
    ]


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
    Load MCP server configs from ~/.claude.json (global) and project configs.

    Global servers take priority — project servers only add new names.
    Project configs: .claude/settings.json + .mcp.json (both supported).
    """
    skip = skip_servers or set()
    servers: dict[str, ServerConfig] = {}

    # 1. Global config first (highest priority)
    global_path = get_claude_config_path()
    if global_path.exists():
        with open(global_path, encoding="utf-8") as f:
            _parse_servers(json.load(f), skip, servers)

    # 2. Project configs — only add servers not already present
    for config_path in _get_project_config_paths():
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                project_data = json.load(f)
            before = set(servers)
            _parse_servers(project_data, skip, servers)
            added = set(servers) - before
            if added:
                logger.info(
                    f"Added project-level servers from {config_path.name}: {sorted(added)}"
                )

    return servers


def server_name_to_py(name: str) -> str:
    """Convert server name to a valid Python identifier."""
    return name.replace("-", "_").replace(".", "_").replace(" ", "_")

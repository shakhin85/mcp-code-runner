import json
from pathlib import Path

from code_runner.config_reader import load_server_configs


def _write_config(tmp_path: Path, servers: dict) -> Path:
    config_path = tmp_path / ".claude.json"
    config_path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return config_path


def test_skip_disabled_server(tmp_path, monkeypatch):
    config = _write_config(tmp_path, {
        "active": {"command": "echo", "args": ["hi"]},
        "disabled-one": {"command": "echo", "args": ["no"], "disabled": True},
    })
    monkeypatch.setattr("code_runner.config_reader.get_claude_config_path", lambda: config)

    result = load_server_configs()
    assert "active" in result
    assert "disabled-one" not in result


def test_http_server_without_env_loads(tmp_path, monkeypatch):
    """HTTP MCP servers without an env dict must still be loaded.

    Cloud MCP servers (claude.ai Crypto.com, Figma, Zapier, etc.) authenticate
    via OAuth through Claude's proxy, not via env vars. An earlier filter that
    skipped HTTP servers lacking env was dropping all of them.
    """
    config = _write_config(tmp_path, {
        "no-auth-http": {"type": "http", "url": "https://example.com/mcp"},
        "with-auth-http": {
            "type": "http",
            "url": "https://example.com/mcp",
            "env": {"Authorization": "Bearer token123"},
        },
        "stdio-server": {"command": "echo", "args": ["hi"]},
    })
    monkeypatch.setattr("code_runner.config_reader.get_claude_config_path", lambda: config)

    result = load_server_configs()
    assert "no-auth-http" in result
    assert result["no-auth-http"].url == "https://example.com/mcp"
    assert "with-auth-http" in result
    assert "stdio-server" in result


def test_mcp_json_project_config_loaded(tmp_path, monkeypatch):
    """.mcp.json in the project dir must add its servers to the result."""
    global_config = _write_config(tmp_path, {
        "global-server": {"command": "echo", "args": ["global"]},
    })
    monkeypatch.setattr("code_runner.config_reader.get_claude_config_path", lambda: global_config)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    mcp_json = project_dir / ".mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "project-db": {"command": "echo", "args": ["db"]},
        }
    }), encoding="utf-8")

    monkeypatch.setattr(
        "code_runner.config_reader._detect_project_dir", lambda: project_dir
    )

    result = load_server_configs()
    assert "global-server" in result
    assert "project-db" in result


def test_global_server_wins_over_project(tmp_path, monkeypatch):
    """If a name exists in both global and project, global takes priority."""
    global_config = _write_config(tmp_path, {
        "shared": {"command": "echo", "args": ["global"]},
    })
    monkeypatch.setattr("code_runner.config_reader.get_claude_config_path", lambda: global_config)

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "shared": {"command": "echo", "args": ["project"]},
        }
    }), encoding="utf-8")

    monkeypatch.setattr(
        "code_runner.config_reader._detect_project_dir", lambda: project_dir
    )

    result = load_server_configs()
    assert result["shared"].args == ["global"]


def test_skip_servers_parameter(tmp_path, monkeypatch):
    config = _write_config(tmp_path, {
        "keep": {"command": "echo", "args": ["hi"]},
        "skip-me": {"command": "echo", "args": ["no"]},
    })
    monkeypatch.setattr("code_runner.config_reader.get_claude_config_path", lambda: config)

    result = load_server_configs(skip_servers={"skip-me"})
    assert "keep" in result
    assert "skip-me" not in result

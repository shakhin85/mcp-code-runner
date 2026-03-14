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


def test_skip_http_without_auth(tmp_path, monkeypatch):
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
    assert "no-auth-http" not in result
    assert "with-auth-http" in result
    assert "stdio-server" in result


def test_skip_servers_parameter(tmp_path, monkeypatch):
    config = _write_config(tmp_path, {
        "keep": {"command": "echo", "args": ["hi"]},
        "skip-me": {"command": "echo", "args": ["no"]},
    })
    monkeypatch.setattr("code_runner.config_reader.get_claude_config_path", lambda: config)

    result = load_server_configs(skip_servers={"skip-me"})
    assert "keep" in result
    assert "skip-me" not in result

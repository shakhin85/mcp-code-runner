# code-runner

MCP server that exposes a single Python `execute_code` tool. User code runs at module scope in a restricted namespace where every other connected MCP server is available as a Python proxy object (e.g. `await postgres_lime.execute_sql(sql=...)`). Server-side: AST validation, SAFE_BUILTINS, SIGALRM timeout, output truncation, auto-LIMIT for bare SELECTs, JSONL metrics.

## Modules

- `server.py` — FastMCP entry point. Tools: `list_available_tools`, `search_tools`, `execute_code`, `get_metrics`, `save_skill` (planned).
- `executor.py` — `CodeExecutor`: validates code, builds namespace, runs with top-level-await, persists user vars per `session_id`.
- `client_pool.py` — `MCPClientPool`: long-lived stdio/HTTP MCP client connections.
- `config_reader.py` — reads `~/.claude.json`, project `.claude/settings.json`, project `.mcp.json`. Detects project dir via `CLAUDE_PROJECT_DIR` or `/proc` walk.
- `schema_gen.py` — JSON-schema → Python stub strings for `search_tools`.
- `sql_limit.py` — sqlglot-based auto-LIMIT/TOP injection for bare SELECTs.
- `metrics.py` — JSONL recorder for `tool_call` and `execute_code` events.
- `workspace.py` (planned) — per-session filesystem at `~/.cache/code-runner/workspace/<session_id>/`, exposed via injected `open()`.
- `skills.py` (planned) — discovers `~/.claude/code-runner-skills/<name>/`, exposes `skills.<name>.<fn>` proxy.

## Dev

- `uv run pytest` — full test suite (135+ tests baseline)
- `uv run code-runner` — start server (stdio transport)
- `CODE_RUNNER_METRICS=0` disables the JSONL metrics recorder (default: enabled, writes to `~/.cache/code-runner/metrics.jsonl`)

## Security

- `.mcp.json` is gitignored — local file holds project-only MCP server config including API keys
- Sandbox forbids `import`, dunder access, file I/O outside workspace, subprocess
- Skills run with full builtins (trusted local code), user code in `execute_code` does not

## Opt-in features

- **Workspace** — set `session_id` to enable `open()` inside sandbox; files live in `~/.cache/code-runner/workspace/<session_id>/`. Cleared on session eviction (TTL or LRU).
- **Skills** — drop a directory into `~/.claude/code-runner-skills/<name>/` containing `script.py` + `SKILL.md`; functions become `skills.<name>.<fn>` in the sandbox. Use `save_skill` MCP tool to create one from inside `execute_code`.

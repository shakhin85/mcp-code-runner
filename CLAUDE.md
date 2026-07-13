# code-runner

MCP server that exposes a single Python `execute_code` tool. User code runs at module scope in a restricted namespace where every other connected MCP server is available as a Python proxy object (e.g. `await postgres_lime.execute_sql(sql=...)`). Server-side: AST validation, SAFE_BUILTINS, SIGALRM timeout, output truncation, auto-LIMIT for bare SELECTs, JSONL metrics.

## Modules

- `server.py` — FastMCP entry point. Tools: `list_available_tools`, `search_tools`, `execute_code`, `get_metrics`, `save_skill`.
- `executor.py` — `CodeExecutor`: validates code, builds namespace, runs with top-level-await, persists user vars per `session_id`.
- `client_pool.py` — `MCPClientPool`: long-lived stdio/HTTP MCP client connections.
- `config_reader.py` — reads `~/.claude.json`, project `.claude/settings.json`, project `.mcp.json`. Detects project dir via `CLAUDE_PROJECT_DIR` or `/proc` walk.
- `schema_gen.py` — JSON-schema → Python stub strings for `search_tools`.
- `sql_limit.py` — sqlglot-based auto-LIMIT/TOP injection for bare SELECTs.
- `metrics.py` — JSONL recorder for `tool_call` and `execute_code` events.
- `workspace.py` — injected `open()`: writes confined to `~/.cache/code-runner/workspace/<session_id>/` (relative paths), reads may also reach absolute paths under `CODE_RUNNER_READ_ROOTS` minus a secret deny-list. Text modes default to UTF-8.
- `skills.py` — discovers `~/.claude/code-runner-skills/<name>/`, exposes `skills.<name>.<fn>` proxy.

## Files and encoding

The sandbox exists to keep raw tool output out of the model's context, not to make
file access awkward. So:

- **Read** — an absolute path is fine as long as it resolves under a read-only root
  (`CODE_RUNNER_READ_ROOTS`, colon-separated; default `~/projects`) and its filename
  is not on the secret deny-list (`.env*`, `*.pem`, `.mcp.json`, `*secret*`, `*token*`, …).
  No need to copy project files into the workspace first.
- **Write** — relative paths only, landing in the session workspace. `session_id` is
  required. Absolute-path writes are always refused; copy the file out with Bash.
- **Encoding** — text modes default to UTF-8 rather than the host locale, and accept
  `encoding` / `errors` / `newline`. The data here is routinely Russian; locale-dependent
  decoding produced mojibake and UnicodeDecodeError.

## Cost receipt

Any run that calls MCP tools appends one line:

```
[cr] 3 tool calls · 120.4KB raw → 1.2KB in context (99% saved) · 840ms
```

A single call whose output passes through unaggregated also prints
`no aggregation happened here` — that run should have been a direct MCP call.
The receipt makes both the saving and its absence visible, so routing through the
sandbox stops being a matter of faith.

## Dev

- `uv run pytest` — full test suite
- `uv run code-runner` — start server (stdio transport)
- `CODE_RUNNER_METRICS=0` disables the JSONL metrics recorder (default: enabled, writes to `~/.cache/code-runner/metrics.jsonl`)
- `CODE_RUNNER_READ_ROOTS=/a:/b` overrides the read-only roots (default `~/projects`)

## Security

- `.mcp.json` is gitignored — local file holds project-only MCP server config including API keys
- Sandbox forbids `import`, dunder access, subprocess, writes outside the session workspace,
  and reads outside the read-only roots (or of secret-looking files inside them)
- Skills run with full builtins (trusted local code), user code in `execute_code` does not

## Opt-in features

- **Workspace** — set `session_id` to enable `open()` inside sandbox; files live in `~/.cache/code-runner/workspace/<session_id>/`. Cleared on session eviction (TTL or LRU).
- **Skills** — drop a directory into `~/.claude/code-runner-skills/<name>/` containing `script.py` + `SKILL.md`; functions become `skills.<name>.<fn>` in the sandbox. Use `save_skill` MCP tool to create one from inside `execute_code`.

## Installing bundled skills

Three reference skills ship under `skills_templates/`:
- **csv_export** — dump rows to CSV in the session workspace
- **snapshot_diff** — compare two row-lists, returns added/removed/changed
- **schema_dump** — render column metadata as a fixed-width table

Install them once into the user skills directory:

```bash
mkdir -p ~/.claude/code-runner-skills
cp -r skills_templates/* ~/.claude/code-runner-skills/
```

Restart code-runner so `SkillLoader` picks them up. Use `save_skill` from inside `execute_code` to add new skills without restarting.

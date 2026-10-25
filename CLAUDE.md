# code-runner

MCP server that exposes a single Python `execute_code` tool. User code runs at module scope in a restricted namespace where every other connected MCP server is available as a Python proxy object (e.g. `await postgres_lime.execute_sql(sql=...)`). Server-side: AST validation, SAFE_BUILTINS, SIGALRM timeout, output truncation, auto-LIMIT for bare SELECTs, JSONL metrics.

## Modules

- `server.py` — FastMCP entry point. Tools: `list_available_tools`, `search_tools`, `execute_code`, `get_metrics`, `save_skill`.
- `executor.py` — `CodeExecutor`: validates code, builds namespace, runs with top-level-await, persists user vars per `session_id`.
- `client_pool.py` — `MCPClientPool`: long-lived stdio/HTTP MCP client connections.
- `config_reader.py` — reads global `~/.claude/.config.json` (falls back to `~/.claude.json` when absent — same file Claude Code reads), project `.claude/settings.json`, project `.mcp.json`. Detects project dir via `CLAUDE_PROJECT_DIR` or `/proc` walk.
- `schema_gen.py` — JSON-schema → Python stub strings for `search_tools`.
- `sql_limit.py` — sqlglot-based auto-LIMIT/TOP injection for bare SELECTs.
- `metrics.py` — JSONL recorder for `tool_call` and `execute_code` events, plus
  `classify_error` / `summarize_errors` behind `get_metrics(summary=True)`: a failure
  breakdown by error class. A class that grows is a missing HINT; a growing
  `syntax_share` means calls are being routed here that should have been direct
  tool calls (7d baseline as of 2026-08-02: 274/1457 failed, SyntaxError share 0.0%).
  `skill_call` events (one per `skills.<name>.<fn>` call, child calls included) feed
  `summarize_skills` behind `get_metrics(skills=True)`: calls, error %, last call per
  function and trim candidates (unused / failing, thresholds `SKILL_TRIM_*`, SHA-484).
  `PastFixIndex` (SHA-483, lazy, cached by mtime/size) pairs an error (class + tool) with the
  next successful block of the session (`fix_line`, literals masked, ≤300) and appends one
  `HINT: в прошлый раз …` on a repeat; `summarize_errors` adds `repeat_error_share`.
- `workspace.py` — injected `open()`: writes confined to `~/.cache/code-runner/workspace/<session_id>/` (relative paths), reads may also reach absolute paths under `CODE_RUNNER_READ_ROOTS` minus a secret deny-list. Text modes default to UTF-8.
- `restore.py` — injected `restore(id, ...)`: lines or grep of a truncated block's full output (`out_<id>.txt`), built on the sandbox `open()`.
- `skills.py` — discovers `~/.claude/code-runner-skills/<name>/`, exposes `skills.<name>.<fn>` proxy.

## Deployment

Боевой режим — общий HTTP-демон под systemd (socket :8030 → backend :8031,
socket-activation + idle-stop), НЕ per-session stdio. Правки кода вступают
после `systemctl --user restart code-runner-backend.service`. Полная схема,
env и резолв проектных серверов через MCP roots: `docs/DAEMON.md`.

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

A third line, `[cr] ⚠ check before trusting the result: …`, names quiet failures —
results that parse fine but may be wrong: 0 rows, a saturated auto-LIMIT, a RAW-CAP
string, rows where every value is None. With `session_id` set, output over
`max_output_bytes` is also written in full to `out_<block>.txt` in the workspace, and
the truncation footer names the block id instead of advising a re-run. The next block
calls `restore(id, start, end)` for lines or `restore(id, grep='re', context=2)` for
matches (`restore.py`, SHA-482) — no downstream call is repeated.

## Dev

- `uv run pytest` — full test suite
- `uv run code-runner` — start server (stdio transport)
- `CODE_RUNNER_METRICS=0` disables the JSONL metrics recorder (default: enabled, writes to `~/.cache/code-runner/metrics.jsonl`)
- `CODE_RUNNER_READ_ROOTS=/a:/b` overrides the read-only roots (default `~/projects`)
- `CODE_RUNNER_ISOLATION=inprocess` runs `execute_code` in the server's own interpreter;
  the default is `subprocess` — a fresh, rlimited child per call. See Security.
- `CODE_RUNNER_MEM_LIMIT_MB` / `CODE_RUNNER_FSIZE_LIMIT_MB` — child `RLIMIT_AS` / `RLIMIT_FSIZE`
  caps under subprocess isolation (defaults 2048 / 64 MB)
- `CODE_RUNNER_START_METHOD` — child start method (default `forkserver`, ~14 ms/call;
  `spawn` is the same boundary at ~714 ms/call. Never plain `fork` — that one *does*
  inherit the parent's heap)

## Security

- `.mcp.json` is gitignored — local file holds project-only MCP server config including API keys
- Sandbox forbids `import`, dunder access (except `__name__`, which yields a plain string
  and opens no escape — `print(type(e).__name__)` is allowed), subprocess, writes outside
  the session workspace, and reads outside the read-only roots (or of secret-looking files
  inside them)
- The AST + safe-builtins layer is a **filter, not a boundary**: several SAFE_MODULES
  re-export os/sys/socket as ordinary attributes (`uuid.os`, `dataclasses.sys`, …), so each
  is wrapped in a `_RestrictedModule` that refuses module-typed attributes. The exception
  field is truncated (`MAX_ERROR_BYTES`) like the output, so a raised error can't be a
  context bomb.
- Error text of a downstream `isError` result is fenced in `<untrusted_tool_error>`
  inside the `MCPToolError` message (SHA-480), so it stays marked whether it surfaces
  in `error` or via `print(e)`; a closing tag inside the payload is defanged.
- **Subprocess isolation** (default; `CODE_RUNNER_ISOLATION=inprocess` opts out) is the real
  containment: user code runs in a per-call child with `RLIMIT_AS`/`RLIMIT_CPU`/`RLIMIT_FSIZE`
  and a scrubbed environment; MCP calls are proxied back to the parent (live sessions never
  reach the child). An escape, OOM, or CPU loop is confined to a killable process, not the
  server. Persisted session vars must be picklable to cross the process boundary.
  Downstream credentials (pool configs, session headers) never reach the child, so
  subprocess keeps them out of user code by construction. In-process relies on the
  AST rule plus a checked formatter: format strings resolve attributes at runtime, so
  `x.format` / `x.format_map` and `string.Formatter` refuse `_`-fields (SHA-487).
  The child is at feature parity with the in-process path — skills, prelude aliases,
  workspace `open()`, and the error HINTs all work there; only the str-result HINT is
  applied parent-side, since it needs the run's tool-call stats.
- Skills run with full builtins (trusted local code), user code in `execute_code` does not
- **Taint (SHA-481)** — run-level, within one `execute_code`: text returned by an untrusted server
  (`CODE_RUNNER_UNTRUSTED_SERVERS`, comma-separated fnmatch against the original and the py name;
  default `wigolo,firecrawl,exa,mcp-email*,twitter,youtube-transcript`) is remembered. A side-effecting
  call whose string args (nested dicts/lists walked) share a substring of `TAINT_MIN_MATCH` = 20 chars with it
  raises `PermissionError` even under `allow_side_effects`; `allow_tainted=["server.tool" | "server.*"]` lifts it.
  Limits: a paraphrased/re-encoded copy or pieces <20 chars slip through; state does not cross `execute_code` calls.
- **Side-effect budget (SHA-524)** — `SideEffectGate`: an `allow_side_effects` entry `srv.tool:N` / `srv.*:N` (shared counter)
  allows N calls per `execute_code` (skill-side and user proxies share it; `srv.*:N` caps the server even for a tool with its
  own entry; a ':' suffix with a dot is part of the name — server names may hold ':'). A gate refusal (deny-by-default, secret) is kept by
  fingerprint (server.tool + canonical args + caller) in the session; the same call under the same permissions is refused at once
  with `повтор отказа:`. Budget and taint refusals are run-level and not remembered.
- **Skill-side proxies (SHA-502)** — skills get their own server proxies via the builtin `_cr_servers`
  (`~/.claude/lib/skill_ns.py` resolves only from it, never from user frames). Only these let a `replaces:`
  owner through the side-effect gate without `allow_side_effects`; user code never holds one.
- **Secret gate (SHA-503)** — `secret_gate.py` loads the harness redactor (`CODE_RUNNER_REDACT_PATH`,
  default `~/.claude/tools/secret-redact/redact.py`) and refuses a side-effecting call whose args carry a
  secret. A skill-side call skips it only under explicit `allow_side_effects` (forgetful `redact=False` escape).

## Opt-in features

- **Workspace** — set `session_id` to enable `open()` inside sandbox; files live in `~/.cache/code-runner/workspace/<session_id>/`. Cleared on session eviction (TTL or LRU).
  Every proxied tool call of the session appends a line (server, tool, args ≤500 chars, duration, bytes, error) to `calls.jsonl` there — `open('calls.jsonl')` gives the repro of a hang (SHA-124).
- **`replaces:`** — optional frontmatter key in a skill's `SKILL.md`: `replaces: [server.tool:fn, ...]` names the raw tools the skill supersedes and the function to call instead (without `:fn` the HINT names only `skills.<name>`). When the side-effect gate refuses one of them, its PermissionError starts with `используй skills.<name>.<fn>(...)`; the `allow_side_effects` line follows. Tools without `replaces:` keep the plain message.
- **No-op import** — `import X` / `import X as Y` / `from X import a, b` of a module already in the namespace (`SAFE_MODULES` + `asyncio`) is rewritten to `Y = X` / `a = X.a` instead of `BlockedImport`; dotted, relative, `*`, `_`-names and any other module stay refused. The `_RestrictedModule` wrapper still guards the values.
- **`dir`/`globals`/`locals`/`vars`** stay undefined; the NameError gets a HINT pointing to `list_available_tools` and `skills.sandbox_introspect.list_all()`.
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

<!-- dgc-policy-v11 -->

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
- Sandbox forbids `import`, dunder access (except `__name__`, which yields a plain string
  and opens no escape — `print(type(e).__name__)` is allowed), subprocess, writes outside
  the session workspace, and reads outside the read-only roots (or of secret-looking files
  inside them)
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

<!-- dgc-policy-v11 -->
# Dual-Graph Context Policy

This project uses a local dual-graph MCP server for efficient context retrieval.

## MANDATORY: Always follow this order

1. **Call `graph_continue` first** — before any file exploration, grep, or code reading.

2. **If `graph_continue` returns `needs_project=true`**: call `graph_scan` with the
   current project directory (`pwd`). Do NOT ask the user.

3. **If `graph_continue` returns `skip=true`**: project has fewer than 5 files.
   Do NOT do broad or recursive exploration. Read only specific files if their names
   are mentioned, or ask the user what to work on.

4. **Read `recommended_files`** using `graph_read` — **one call per file**.
   - `graph_read` accepts a single `file` parameter (string). Call it separately for each
     recommended file. Do NOT pass an array or batch multiple files into one call.
   - `recommended_files` may contain `file::symbol` entries (e.g. `src/auth.ts::handleLogin`).
     Pass them verbatim to `graph_read(file: "src/auth.ts::handleLogin")` — it reads only
     that symbol's lines, not the full file.
   - Example: if `recommended_files` is `["src/auth.ts::handleLogin", "src/db.ts"]`,
     call `graph_read(file: "src/auth.ts::handleLogin")` and `graph_read(file: "src/db.ts")`
     as two separate calls (they can be parallel).

5. **Check `confidence` and obey the caps strictly:**
   - `confidence=high` -> Stop. Do NOT grep or explore further.
   - `confidence=medium` -> If recommended files are insufficient, call `fallback_rg`
     at most `max_supplementary_greps` time(s) with specific terms, then `graph_read`
     at most `max_supplementary_files` additional file(s). Then stop.
   - `confidence=low` -> Call `fallback_rg` at most `max_supplementary_greps` time(s),
     then `graph_read` at most `max_supplementary_files` file(s). Then stop.

## Token Usage

A `token-counter` MCP is available for tracking live token usage.

- To check how many tokens a large file or text will cost **before** reading it:
  `count_tokens({text: "<content>"})`
- To log actual usage after a task completes (if the user asks):
  `log_usage({input_tokens: <est>, output_tokens: <est>, description: "<task>"})`
- To show the user their running session cost:
  `get_session_stats()`

Live dashboard URL is printed at startup next to "Token usage".

## Rules

- Do NOT use `rg`, `grep`, or bash file exploration before calling `graph_continue`.
- Do NOT do broad/recursive exploration at any confidence level.
- `max_supplementary_greps` and `max_supplementary_files` are hard caps - never exceed them.
- Do NOT dump full chat history.
- Do NOT call `graph_retrieve` more than once per turn.
- After edits, call `graph_register_edit` with the changed files. Use `file::symbol` notation (e.g. `src/auth.ts::handleLogin`) when the edit targets a specific function, class, or hook.

## Context Store

Whenever you make a decision, identify a task, note a next step, fact, or blocker during a conversation, call `graph_add_memory`.

**To add an entry:**
```
graph_add_memory(type="decision|task|next|fact|blocker", content="one sentence max 15 words", tags=["topic"], files=["relevant/file.ts"])
```

**Do NOT write context-store.json directly** — always use `graph_add_memory`. It applies pruning and keeps the store healthy.

**Rules:**
- Only log things worth remembering across sessions (not every minor detail)
- `content` must be under 15 words
- `files` lists the files this decision/task relates to (can be empty)
- Log immediately when the item arises — not at session end

## Session End

When the user signals they are done (e.g. "bye", "done", "wrap up", "end session"), proactively update `CONTEXT.md` in the project root with:
- **Current Task**: one sentence on what was being worked on
- **Key Decisions**: bullet list, max 3 items
- **Next Steps**: bullet list, max 3 items

Keep `CONTEXT.md` under 20 lines total. Do NOT summarize the full conversation — only what's needed to resume next session.

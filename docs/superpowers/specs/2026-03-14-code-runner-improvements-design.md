# code-runner MCP Server Improvements

## Overview

Seven improvements to the code-runner MCP server addressing security, performance, token efficiency, and robustness. Inspired by Cloudflare's Code Mode architecture.

## Current State

- 5 source files, ~350 lines
- Connects to all MCP servers from `~/.claude.json` sequentially
- Exposes 3 tools: `list_available_tools`, `execute_code`, `get_tool_stub`
- No execution sandbox, no input validation
- Full tool stubs dumped on every `list_available_tools` call

---

## Change 1: config_reader.py — Skip disabled & unauthenticated HTTP servers

### Problem
- Servers with `"disabled": true` are still connected
- HTTP servers without auth headers (OAuth-managed by Claude.ai) always fail with 401

### Design
In `load_server_configs()`:
- Check `cfg.get("disabled", False)` — skip if true
- If `transport == "http"` and no `env` (auth headers) — skip with log warning
- Remove `"figma"` from hardcoded `SKIP_SERVERS` in `server.py` (now handled automatically)

`SKIP_SERVERS` retains only architectural skips: `{"code-runner", "serena"}`.

---

## Change 2: client_pool.py — Individual exit stacks + parallel connections

### Problem
- Sequential startup takes 30+ seconds for 9 servers
- Shared `AsyncExitStack` causes cascading failures when one server crashes

### Design

**Individual exit stacks**: Each server (stdio and HTTP) gets its own `AsyncExitStack`. Main stack stores `aclose()` callbacks for each child stack.

**Parallel connections**: Replace sequential loop with `asyncio.gather(*tasks, return_exceptions=True)`.

```
startup()
  └─ asyncio.gather(
       _safe_connect("local-rag", cfg),
       _safe_connect("mssql", cfg),
       ...
     )

_safe_connect(name, cfg)
  ├─ asyncio.wait_for(_connect(name, cfg), timeout=30)  # per-server timeout
  ├─ on success: log connected + tool count
  └─ except Exception: self.failed[name] = str(e)
      (catch Exception, not BaseException — let KeyboardInterrupt/SystemExit propagate)

_connect_stdio() / _connect_http()
  └─ each creates own AsyncExitStack, transfers to main on success
```

Thread safety: `self.sessions`, `self.tools`, `self.failed` are dict mutations from coroutines on the same event loop — safe without locks.

**Per-server connection timeout**: 30 seconds via `asyncio.wait_for`. Prevents one hanging server from blocking startup indefinitely.

---

## Change 3: executor.py — Sandbox (whitelist builtins + AST validation)

### Problem
`exec()` runs with full `__builtins__` — access to `open()`, `os`, `subprocess`, `__import__`.

### Design

**Safe builtins whitelist:**
```python
SAFE_BUILTINS = {
    "print", "len", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "list", "dict", "set", "tuple", "str",
    "int", "float", "bool", "isinstance",
    "min", "max", "sum", "abs", "round", "any", "all",
    "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "Exception",
}
```

Blocked: `open`, `exec`, `eval`, `compile`, `__import__`, `globals`, `locals`, `breakpoint`, `exit`, `quit`.
Also blocked (sandbox escape vectors): `getattr`, `hasattr`, `setattr`, `type` (3-arg form creates classes).
Note: `True`, `False`, `None` are Python keywords — available regardless of `__builtins__`.

**AST validation before exec (runs on raw user code BEFORE wrapping in async def):**
- Parse code with `ast.parse()`
- Walk AST, reject if contains:
  - `ast.Import` or `ast.ImportFrom` nodes
  - `ast.Attribute` where attr starts/ends with `__` (blocks `.__class__`, `.__subclasses__`)
- Return clear error message listing what was blocked

**Pipeline order:**
1. AST-validate raw user code (reject dangerous constructs)
2. Apply auto-display transform on raw code (Change 4) — before wrapping
3. Wrap in `async def __user_code__():`
4. Compile and exec

**Namespace:**
```python
namespace = {
    "__builtins__": {name: getattr(builtins, name) for name in SAFE_BUILTINS},
    "asyncio": _SAFE_ASYNCIO,  # only sleep, gather, wait_for
    "json": json,
    # + server namespaces
}
```

**Safe asyncio subset** — only expose safe primitives, not `asyncio.subprocess`:
```python
_SAFE_ASYNCIO = types.ModuleType("asyncio")
_SAFE_ASYNCIO.sleep = asyncio.sleep
_SAFE_ASYNCIO.gather = asyncio.gather
_SAFE_ASYNCIO.wait_for = asyncio.wait_for
```

**Security posture:** Best-effort sandbox for LLM-generated code, not adversarial-resistant. Sufficient for local tool where the LLM is trusted but code errors should be contained.

---

## Change 4: executor.py — Auto-display last expression

### Problem
Code ending with a bare expression (no `print`, no `return`) silently drops the result.

### Design
After wrapping user code in `async def __user_code__():`, parse the function body with `ast.parse()`. If the last statement is `ast.Expr` (bare expression), replace it with `ast.Return(value=expr.value)`.

```python
# Before transform:
async def __user_code__():
    result = await mssql.execute_sql(query="SELECT 1")
    result  # ast.Expr — bare expression

# After transform:
async def __user_code__():
    result = await mssql.execute_sql(query="SELECT 1")
    return result  # ast.Return
```

Implementation: ~10 lines using `ast.parse`, `ast.walk`, node replacement, `ast.fix_missing_locations`, `compile`.

Note: Auto-display only fires on bare expression statements (`ast.Expr`). Code ending with `if/for/while/try` blocks will not trigger it — this is expected and consistent with IPython behavior.

---

## Change 5: schema_gen.py — Richer stubs

### Problem
- Parameter descriptions lost (only types shown)
- Enum values not displayed
- Nested object structures collapsed to `dict`

### Design

**Parameter descriptions:**
```python
# Execute a SQL query against the database
# Args:
#   query (str): The SQL query to execute
#   timeout (int, optional): Query timeout in seconds
result = await mssql.execute_sql(query: str, timeout: int = None)
```

**Enum values** — append to param description:
```python
#   interval (str): Candle interval. Values: "1m", "5m", "15m", "1h", "4h", "1d"
```

**Nested objects** — show structure 1 level deep. Nesting beyond level 1 rendered as `dict`:
```python
#   options (dict): {key: str, value: str, enabled: bool}
```

Changes in `tool_to_stub()` and `json_type_to_py()`.

---

## Change 6: server.py — search_tools replaces full dump

### Problem
`list_available_tools` dumps all 61 tool stubs — massive token waste.

### Design

**`list_available_tools`** — changed to brief overview:
```
# Connected MCP servers:
# - mssql (1 tools): Execute SQL queries
# - filesystem (24 tools): File operations
# ...
# Use search_tools(query="...") to find specific tools
```

Server description = first tool's description truncated, or server name.

**New `search_tools(query: str)`** — keyword AND-match:
- Split query into lowercase words
- For each tool across all servers, check if ALL words appear in `tool.name + tool.description`
- Return full stubs (with improved format from Change 5) only for matches
- If no matches, suggest closest server names
- Edge cases: empty query returns brief overview (same as `list_available_tools`); single word = substring match

**Remove `get_tool_stub`** — its functionality is covered by `search_tools`.

Final tool set: `list_available_tools` (overview), `search_tools` (discovery), `execute_code` (execution).

---

## Files Changed

| File | Changes |
|------|---------|
| `config_reader.py` | Skip disabled servers, skip HTTP without auth |
| `client_pool.py` | Individual exit stacks, parallel connections |
| `executor.py` | Safe builtins whitelist, AST validation, auto-display last expr |
| `schema_gen.py` | Parameter descriptions, enums, nested objects |
| `server.py` | Brief overview, new `search_tools`, remove `get_tool_stub` |

## Non-Goals

- Full V8-style sandbox (overkill for local tool)
- Typed Python stubs with dataclasses (too complex for LLM consumption)
- Caching tool schemas (servers are long-lived connections)

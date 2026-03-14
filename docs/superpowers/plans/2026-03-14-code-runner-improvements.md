# code-runner Improvements Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve code-runner MCP server with sandbox security, parallel startup, token-efficient tool discovery, and richer stubs.

**Architecture:** Six source files, no new dependencies. Changes are isolated per-file: config filtering, parallel pool startup with individual exit stacks, AST-based sandbox + auto-display in executor, enriched stub generation, and search-based tool discovery replacing full dump.

**Tech Stack:** Python 3.11+, mcp SDK, asyncio, ast module, builtins module, types module

**Spec:** `docs/superpowers/specs/2026-03-14-code-runner-improvements-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `src/code_runner/config_reader.py` | Read ~/.claude.json, filter server configs | Modify |
| `src/code_runner/client_pool.py` | Manage MCP client connections | Modify |
| `src/code_runner/executor.py` | Sandbox + execute LLM code | Modify |
| `src/code_runner/schema_gen.py` | Generate Python stubs from tool schemas | Modify |
| `src/code_runner/server.py` | FastMCP server, tool definitions | Modify |
| `tests/test_config_reader.py` | Tests for config filtering | Create |
| `tests/test_executor.py` | Tests for sandbox + auto-display | Create |
| `tests/test_schema_gen.py` | Tests for stub generation | Create |
| `tests/test_search_tools.py` | Tests for search_tools logic | Create |

---

## Chunk 1: Setup + config_reader (Change 1)

### Task 1: Add pytest to dev dependencies

- [ ] **Step 1: Add pytest**

```bash
cd C:/mcp-servers/code-runner && uv add --dev pytest
```

- [ ] **Step 2: Create tests directory**

```bash
mkdir -p C:/mcp-servers/code-runner/tests
touch C:/mcp-servers/code-runner/tests/__init__.py
```

- [ ] **Step 3: Verify pytest runs**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/ -v`
Expected: "no tests ran" (0 collected), exit 5

- [ ] **Step 4: Commit**

```bash
cd C:/mcp-servers/code-runner
git add pyproject.toml uv.lock tests/
git commit -m "chore: add pytest dev dependency and tests directory"
```

---

### Task 2: config_reader — skip disabled and unauthenticated HTTP servers

- [ ] **Step 1: Write failing tests**

Create `tests/test_config_reader.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_config_reader.py -v`
Expected: `test_skip_disabled_server` FAILS (disabled server not filtered), `test_skip_http_without_auth` FAILS (HTTP without auth not filtered)

- [ ] **Step 3: Implement config filtering**

Modify `src/code_runner/config_reader.py` — in `load_server_configs()`, add after `if name in skip: continue`:

```python
import logging

logger = logging.getLogger(__name__)

# Inside the for loop, after skip check:
if cfg.get("disabled", False):
    continue

transport = cfg.get("type", "stdio")

if transport == "http" and not cfg.get("env"):
    logger.info(f"Skipping HTTP server '{name}' (no auth headers)")
    continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_config_reader.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Update server.py SKIP_SERVERS — remove "figma"**

Change `SKIP_SERVERS` in `src/code_runner/server.py`:

```python
SKIP_SERVERS: set[str] = {"code-runner", "serena"}
```

- [ ] **Step 6: Commit**

```bash
cd C:/mcp-servers/code-runner
git add src/code_runner/config_reader.py src/code_runner/server.py tests/test_config_reader.py
git commit -m "feat: skip disabled and unauthenticated HTTP servers in config"
```

---

## Chunk 2: client_pool parallel startup (Change 2)

### Task 3: Parallel connections with individual exit stacks

- [ ] **Step 1: Implement parallel startup**

Rewrite `src/code_runner/client_pool.py`:

```python
"""
Manages long-lived connections to all configured MCP servers.
Acts as MCP client to each server.
"""

import asyncio
import logging
import os
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import Tool

from .config_reader import ServerConfig, load_server_configs, server_name_to_py

logger = logging.getLogger(__name__)

CONNECTION_TIMEOUT = 30  # seconds per server


class MCPClientPool:
    def __init__(self):
        self._exit_stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, list[Tool]] = {}
        self.failed: dict[str, str] = {}

    async def startup(self, skip_servers: set[str] | None = None) -> None:
        """Connect to all configured MCP servers in parallel."""
        await self._exit_stack.__aenter__()
        configs = load_server_configs(skip_servers)

        tasks = [
            self._safe_connect(name, cfg)
            for name, cfg in configs.items()
        ]
        await asyncio.gather(*tasks)

    async def _safe_connect(self, name: str, cfg: ServerConfig) -> None:
        try:
            await asyncio.wait_for(
                self._connect(name, cfg),
                timeout=CONNECTION_TIMEOUT,
            )
            tool_count = len(self.tools.get(name, []))
            logger.info(f"Connected to '{name}' ({tool_count} tools)")
        except Exception as e:
            self.failed[name] = str(e)
            logger.warning(f"Failed to connect to '{name}': {e}")

    async def _connect(self, name: str, cfg: ServerConfig) -> None:
        if cfg.transport == "http":
            await self._connect_http(name, cfg)
        else:
            await self._connect_stdio(name, cfg)

    async def _connect_stdio(self, name: str, cfg: ServerConfig) -> None:
        merged_env = {**os.environ, **cfg.env}
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=merged_env,
        )

        stack = AsyncExitStack()
        await stack.__aenter__()

        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            result = await session.list_tools()
            self.sessions[name] = session
            self.tools[name] = result.tools

            self._exit_stack.push_async_callback(stack.aclose)
        except BaseException:
            await stack.aclose()
            raise

    async def _connect_http(self, name: str, cfg: ServerConfig) -> None:
        """Connect to HTTP/SSE MCP server using an isolated exit stack."""
        stack = AsyncExitStack()
        await stack.__aenter__()

        try:
            try:
                from mcp.client.streamable_http import streamablehttp_client

                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(cfg.url, headers=cfg.env or {})
                )
            except ImportError:
                from mcp.client.sse import sse_client

                read, write = await stack.enter_async_context(
                    sse_client(cfg.url)
                )

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            result = await session.list_tools()
            self.sessions[name] = session
            self.tools[name] = result.tools

            self._exit_stack.push_async_callback(stack.aclose)
        except BaseException:
            await stack.aclose()
            raise

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
        session = self.sessions.get(server_name)
        if session is None:
            raise RuntimeError(f"Server '{server_name}' is not connected")
        return await session.call_tool(tool_name, arguments)

    async def shutdown(self) -> None:
        await self._exit_stack.aclose()

    def get_all_tools(self) -> dict[str, list[Tool]]:
        return self.tools

    def connected_servers(self) -> list[str]:
        return list(self.sessions.keys())

    def py_name_map(self) -> dict[str, str]:
        """Map Python identifier -> original server name."""
        return {server_name_to_py(name): name for name in self.sessions}
```

- [ ] **Step 2: Smoke test — run code-runner and verify all servers connect**

Run: `cd C:/mcp-servers/code-runner && timeout 60 uv run code-runner 2>&1 | grep -E "(Connected|Failed)" | head -15`
Expected: All 9 servers show "Connected", no "Failed" entries (figma no longer attempted)

- [ ] **Step 3: Commit**

```bash
cd C:/mcp-servers/code-runner
git add src/code_runner/client_pool.py
git commit -m "feat: parallel server connections with individual exit stacks and 30s timeout"
```

---

## Chunk 3: Executor sandbox + auto-display (Changes 3 & 4)

### Task 4: AST validation (sandbox)

- [ ] **Step 1: Write failing tests for AST validation**

Create `tests/test_executor.py`:

```python
import ast
import pytest

# We'll test the validation function directly once extracted
from code_runner.executor import validate_code, CodeExecutor


class TestValidateCode:
    def test_allows_normal_code(self):
        validate_code("x = 1 + 2\nprint(x)")

    def test_allows_await(self):
        validate_code("result = await foo.bar(x=1)")

    def test_rejects_import(self):
        with pytest.raises(ValueError, match="import"):
            validate_code("import os")

    def test_rejects_from_import(self):
        with pytest.raises(ValueError, match="import"):
            validate_code("from os import path")

    def test_rejects_dunder_attribute(self):
        with pytest.raises(ValueError, match="__"):
            validate_code("x.__class__")

    def test_rejects_dunder_subclasses(self):
        with pytest.raises(ValueError, match="__"):
            validate_code("x.__subclasses__()")

    def test_allows_normal_attributes(self):
        validate_code("x.name\nx.value")

    def test_syntax_error_raises(self):
        with pytest.raises(ValueError, match="SyntaxError"):
            validate_code("def def def")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_executor.py::TestValidateCode -v`
Expected: ImportError — `validate_code` doesn't exist yet

- [ ] **Step 3: Implement `validate_code` in executor.py**

Add to top of `src/code_runner/executor.py`:

```python
import ast
import builtins
import types


def validate_code(code: str) -> None:
    """Validate user code AST. Raises ValueError if dangerous constructs found."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"SyntaxError: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                names = [node.module or ""]
            raise ValueError(
                f"import statements are not allowed: {', '.join(names)}"
            )

        if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            raise ValueError(
                f"dunder attribute access is not allowed: {node.attr}"
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_executor.py::TestValidateCode -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/mcp-servers/code-runner
git add src/code_runner/executor.py tests/test_executor.py
git commit -m "feat: add AST validation to reject imports and dunder access"
```

---

### Task 5: Safe builtins + safe asyncio

- [ ] **Step 1: Write failing tests for sandbox**

Append to `tests/test_executor.py`:

```python
import asyncio


class TestSandboxNamespace:
    @pytest.fixture
    def executor(self):
        """Executor with no MCP connections (empty pool mock)."""

        class FakePool:
            sessions = {}
            tools = {}

        return CodeExecutor(FakePool())

    def test_print_works(self, executor):
        result = asyncio.run(executor.execute("print('hello')"))
        assert result["success"] is True
        assert "hello" in result["output"]

    def test_open_blocked(self, executor):
        result = asyncio.run(executor.execute("open('test.txt')"))
        assert result["success"] is False

    def test_import_blocked(self, executor):
        result = asyncio.run(executor.execute("import os"))
        assert result["success"] is False
        assert "import" in result["error"].lower()

    def test_dunder_blocked(self, executor):
        result = asyncio.run(executor.execute("x = ''.__class__"))
        assert result["success"] is False
        assert "__" in result["error"]

    def test_json_available(self, executor):
        result = asyncio.run(executor.execute("print(json.dumps({'a': 1}))"))
        assert result["success"] is True
        assert '{"a": 1}' in result["output"]

    def test_asyncio_sleep_available(self, executor):
        result = asyncio.run(executor.execute("await asyncio.sleep(0)"))
        assert result["success"] is True

    def test_asyncio_subprocess_blocked(self, executor):
        result = asyncio.run(executor.execute(
            "p = await asyncio.create_subprocess_exec('echo', 'hi')"
        ))
        assert result["success"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_executor.py::TestSandboxNamespace -v`
Expected: Several FAIL (open not blocked, import not caught at exec level, asyncio.subprocess not blocked)

- [ ] **Step 3: Implement safe builtins and safe asyncio**

Update `src/code_runner/executor.py` — add constants and modify `_build_namespace`:

```python
SAFE_BUILTINS = {
    "print", "len", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "list", "dict", "set", "tuple", "str",
    "int", "float", "bool", "isinstance",
    "min", "max", "sum", "abs", "round", "any", "all",
    "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "Exception",
}

# Safe asyncio subset — no subprocess access
_SAFE_ASYNCIO = types.ModuleType("asyncio")
_SAFE_ASYNCIO.sleep = asyncio.sleep
_SAFE_ASYNCIO.gather = asyncio.gather
_SAFE_ASYNCIO.wait_for = asyncio.wait_for
```

Modify `_build_namespace`:

```python
def _build_namespace(self) -> dict[str, Any]:
    safe_builtins = {name: getattr(builtins, name) for name in SAFE_BUILTINS if hasattr(builtins, name)}
    namespace: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "asyncio": _SAFE_ASYNCIO,
        "json": json,
    }

    for server_name, session in self.pool.sessions.items():
        py_name = server_name_to_py(server_name)
        tools = self.pool.tools.get(server_name, [])
        namespace[py_name] = _ToolNamespace(server_name, session, tools)

    return namespace
```

Modify `execute` method — add validation before exec:

```python
async def execute(self, code: str, timeout: float = 60.0) -> dict[str, Any]:
    # Step 1: AST-validate raw user code
    try:
        validate_code(code)
    except ValueError as e:
        return {"success": False, "error": str(e), "output": ""}

    namespace = self._build_namespace()
    # ... rest unchanged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_executor.py -v`
Expected: All tests PASS (both TestValidateCode and TestSandboxNamespace)

- [ ] **Step 5: Commit**

```bash
cd C:/mcp-servers/code-runner
git add src/code_runner/executor.py tests/test_executor.py
git commit -m "feat: sandbox execution with safe builtins whitelist and restricted asyncio"
```

---

### Task 6: Auto-display last expression

- [ ] **Step 1: Write failing tests**

Append to `tests/test_executor.py`:

```python
class TestAutoDisplay:
    @pytest.fixture
    def executor(self):
        class FakePool:
            sessions = {}
            tools = {}
        return CodeExecutor(FakePool())

    def test_bare_expression_returned(self, executor):
        result = asyncio.run(executor.execute("x = 42\nx"))
        assert result["success"] is True
        assert "42" in result["output"]

    def test_bare_string_expression(self, executor):
        result = asyncio.run(executor.execute("'hello world'"))
        assert result["success"] is True
        assert "hello world" in result["output"]

    def test_assignment_no_auto_display(self, executor):
        result = asyncio.run(executor.execute("x = 42"))
        assert result["success"] is True
        assert result["output"] == ""

    def test_print_still_works(self, executor):
        result = asyncio.run(executor.execute("print('explicit')"))
        assert result["success"] is True
        assert "explicit" in result["output"]

    def test_dict_auto_display(self, executor):
        result = asyncio.run(executor.execute("{'a': 1, 'b': 2}"))
        assert result["success"] is True
        assert "a" in result["output"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_executor.py::TestAutoDisplay -v`
Expected: `test_bare_expression_returned` and `test_bare_string_expression` FAIL (output empty)

- [ ] **Step 3: Implement auto-display transform**

Add function to `src/code_runner/executor.py`:

```python
def _transform_last_expr(code: str) -> str:
    """If the last statement is a bare expression, convert to return statement."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    if not tree.body:
        return code

    last = tree.body[-1]
    if isinstance(last, ast.Expr):
        tree.body[-1] = ast.Return(value=last.value)
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    return code
```

Modify `execute` method — apply transform after wrapping:

```python
# In execute(), after wrapping in async def:
```python
# Pipeline order in execute():
# 1. validate_code(code) — already done above
# 2. Apply auto-display transform on raw code (before wrapping)
code = _transform_last_expr(code)
# 3. Wrap in async def
indented = textwrap.indent(code, "    ")
wrapped = f"async def __user_code__():\n{indented}\n"
# 4. Compile and exec (below)
```

Add transform function (works on raw user code before wrapping):

```python
def _transform_last_expr(code: str) -> str:
    """If last statement is a bare expression, convert to return for auto-display.
    Note: ast.unparse strips comments from user code. This is an accepted trade-off."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    if not tree.body:
        return code

    last = tree.body[-1]
    if isinstance(last, ast.Expr):
        ret = ast.Return(value=last.value)
        ast.copy_location(ret, last)  # copy_location(new_node, old_node)
        tree.body[-1] = ret
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    return code
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_executor.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/mcp-servers/code-runner
git add src/code_runner/executor.py tests/test_executor.py
git commit -m "feat: auto-display last bare expression (IPython-style)"
```

---

## Chunk 4: schema_gen richer stubs (Change 5)

### Task 7: Enriched stub generation

- [ ] **Step 1: Write failing tests**

Create `tests/test_schema_gen.py`:

```python
from mcp.types import Tool

from code_runner.schema_gen import tool_to_stub, generate_server_overview


def _make_tool(name: str, description: str, schema: dict) -> Tool:
    return Tool(name=name, description=description, inputSchema=schema)


class TestToolToStub:
    def test_param_description_included(self):
        tool = _make_tool("execute_sql", "Execute a SQL query", {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The SQL query to execute",
                },
            },
            "required": ["query"],
        })
        stub = tool_to_stub("mssql", tool)
        assert "The SQL query to execute" in stub

    def test_enum_values_shown(self):
        tool = _make_tool("get_candles", "Get candle data", {
            "type": "object",
            "properties": {
                "interval": {
                    "type": "string",
                    "description": "Candle interval",
                    "enum": ["1m", "5m", "1h", "1d"],
                },
            },
            "required": ["interval"],
        })
        stub = tool_to_stub("crypto", tool)
        assert "1m" in stub
        assert "1d" in stub

    def test_nested_object_shown(self):
        tool = _make_tool("create", "Create item", {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "enabled": {"type": "boolean"},
                    },
                },
            },
            "required": [],
        })
        stub = tool_to_stub("srv", tool)
        assert "key" in stub
        assert "enabled" in stub

    def test_optional_param_marked(self):
        tool = _make_tool("foo", "Do foo", {
            "type": "object",
            "properties": {
                "required_param": {"type": "string"},
                "optional_param": {"type": "integer"},
            },
            "required": ["required_param"],
        })
        stub = tool_to_stub("srv", tool)
        assert "optional" in stub.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_schema_gen.py -v`
Expected: FAIL — param descriptions not in output, enums not shown

- [ ] **Step 3: Implement enriched stubs**

Rewrite `src/code_runner/schema_gen.py`:

```python
"""
Converts MCP tool schemas into Python function stubs for LLM consumption.
"""

from mcp.types import Tool


_JSON_TYPE_MAP = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "null": "None",
}


def json_type_to_py(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):
        types = [_JSON_TYPE_MAP.get(x, "Any") for x in t if x != "null"]
        nullable = "null" in t
        result = " | ".join(types) if types else "Any"
        return f"{result} | None" if nullable else result
    return _JSON_TYPE_MAP.get(t, "Any")


def _describe_object_fields(schema: dict) -> str:
    """Render object properties as {key: type, ...} one level deep."""
    props = schema.get("properties", {})
    if not props:
        return "dict"
    fields = []
    for k, v in props.items():
        fields.append(f"{k}: {json_type_to_py(v)}")
    return "{" + ", ".join(fields) + "}"


def _param_line(name: str, schema: dict, is_required: bool) -> tuple[str, str]:
    """Return (signature_part, doc_line) for a parameter."""
    py_type = json_type_to_py(schema)
    desc = schema.get("description", "")

    # Enum values
    enum = schema.get("enum")
    if enum:
        enum_str = ", ".join(f'"{v}"' if isinstance(v, str) else str(v) for v in enum)
        desc = f"{desc}. Values: {enum_str}" if desc else f"Values: {enum_str}"

    # Nested object detail
    if schema.get("type") == "object" and schema.get("properties"):
        obj_detail = _describe_object_fields(schema)
        desc = f"{desc}. Structure: {obj_detail}" if desc else f"Structure: {obj_detail}"

    optional_tag = "" if is_required else ", optional"
    doc = f"#   {name} ({py_type}{optional_tag}): {desc}" if desc else f"#   {name} ({py_type}{optional_tag})"

    sig = f"{name}: {py_type}" if is_required else f"{name}: {py_type} = None"

    return sig, doc


def tool_to_stub(server_py_name: str, tool: Tool) -> str:
    """Generate a Python async function stub string with docs."""
    props: dict = {}
    required: list[str] = []

    if tool.inputSchema:
        props = tool.inputSchema.get("properties", {})
        required = tool.inputSchema.get("required", [])

    sig_parts = []
    doc_lines = []

    for param_name, param_schema in props.items():
        sig, doc = _param_line(param_name, param_schema, param_name in required)
        sig_parts.append(sig)
        doc_lines.append(doc)

    params_str = ", ".join(sig_parts)
    desc = (tool.description or "").strip()
    if len(desc) > 200:
        desc = desc[:197] + "..."

    lines = [f"# {desc}"]
    if doc_lines:
        lines.append("# Args:")
        lines.extend(doc_lines)
    lines.append(f"result = await {server_py_name}.{tool.name}({params_str})")

    return "\n".join(lines)


def generate_stubs_for_server(server_py_name: str, tools: list[Tool]) -> str:
    """Generate all stubs for a single server."""
    if not tools:
        return f"# {server_py_name}: no tools available"

    lines = [f"# === {server_py_name} ({len(tools)} tools) ==="]
    for tool in tools:
        lines.append(tool_to_stub(server_py_name, tool))
        lines.append("")
    return "\n".join(lines)


def generate_full_reference(tools_by_server: dict[str, list[Tool]], py_name_map: dict[str, str]) -> str:
    """Generate complete Python reference for all connected servers."""
    sections = []
    for py_name, server_name in sorted(py_name_map.items()):
        tools = tools_by_server.get(server_name, [])
        sections.append(generate_stubs_for_server(py_name, tools))
    return "\n\n".join(sections)


def generate_server_overview(tools_by_server: dict[str, list[Tool]], py_name_map: dict[str, str]) -> str:
    """Generate brief overview of connected servers (names + tool counts)."""
    lines = ["# Connected MCP servers:"]
    for py_name, server_name in sorted(py_name_map.items()):
        tools = tools_by_server.get(server_name, [])
        count = len(tools)
        desc = ""
        if tools and tools[0].description:
            desc = tools[0].description.split("\n")[0].strip()
            if len(desc) > 80:
                desc = desc[:77] + "..."
        line = f"# - {py_name} ({count} tools)"
        if desc:
            line += f": {desc}"
        lines.append(line)
    lines.append('#')
    lines.append('# Use search_tools(query="...") to find specific tools with full signatures.')
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_schema_gen.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/mcp-servers/code-runner
git add src/code_runner/schema_gen.py tests/test_schema_gen.py
git commit -m "feat: enriched stubs with param descriptions, enums, and nested object detail"
```

---

## Chunk 5: server.py — search_tools + brief overview (Change 6)

### Task 8: Replace full dump with overview + search

- [ ] **Step 1: Write failing tests**

Create `tests/test_search_tools.py`:

```python
from mcp.types import Tool

from code_runner.server import _search_tools_logic, _overview_logic


def _make_tool(name, description="", schema=None):
    return Tool(name=name, description=description or name, inputSchema=schema or {"type": "object", "properties": {}})


TOOLS_BY_SERVER = {
    "mssql": [_make_tool("execute_sql", "Execute a SQL query against the database")],
    "filesystem": [
        _make_tool("read_text_file", "Read a text file from disk"),
        _make_tool("write_file", "Write content to a file"),
    ],
    "context7": [_make_tool("query_docs", "Query documentation for a library")],
}

PY_NAME_MAP = {"mssql": "mssql", "filesystem": "filesystem", "context7": "context7"}


class TestSearchTools:
    def test_single_keyword(self):
        results = _search_tools_logic("sql", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "execute_sql" in results

    def test_multi_keyword_and(self):
        results = _search_tools_logic("text file", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "read_text_file" in results
        assert "execute_sql" not in results

    def test_no_match(self):
        results = _search_tools_logic("blockchain", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "No tools found" in results

    def test_empty_query_returns_overview(self):
        results = _search_tools_logic("", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "Connected MCP servers" in results

    def test_whitespace_query_returns_overview(self):
        results = _search_tools_logic("   ", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "Connected MCP servers" in results

    def test_single_word_matches_description(self):
        results = _search_tools_logic("database", TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "execute_sql" in results


class TestOverview:
    def test_shows_server_names(self):
        overview = _overview_logic(TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "mssql" in overview
        assert "filesystem" in overview
        assert "context7" in overview

    def test_shows_tool_counts(self):
        overview = _overview_logic(TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "2 tools" in overview  # filesystem

    def test_does_not_show_full_stubs(self):
        overview = _overview_logic(TOOLS_BY_SERVER, PY_NAME_MAP)
        assert "result = await" not in overview
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_search_tools.py -v`
Expected: ImportError — `_search_tools_logic` doesn't exist

- [ ] **Step 3: Implement search logic and update server tools**

Rewrite `src/code_runner/server.py`:

```python
"""
code-runner MCP server.

Exposes three tools to Claude:
  - list_available_tools  -> brief overview of connected servers
  - search_tools          -> keyword search returning full stubs
  - execute_code          -> run Python code with MCP tool access
"""

import logging
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import Tool

from .client_pool import MCPClientPool
from .schema_gen import generate_server_overview, generate_stubs_for_server
from .config_reader import server_name_to_py

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# Servers to skip (avoid self-reference and heavy servers)
SKIP_SERVERS: set[str] = {"code-runner", "serena"}


@asynccontextmanager
async def lifespan(server: FastMCP):
    pool = MCPClientPool()
    await pool.startup(skip_servers=SKIP_SERVERS)

    connected = pool.connected_servers()
    failed = pool.failed

    logger.info(f"Connected: {connected}")
    if failed:
        logger.warning(f"Failed: {failed}")

    try:
        yield {"pool": pool}
    finally:
        await pool.shutdown()


mcp = FastMCP("code-runner", lifespan=lifespan)


def _overview_logic(
    tools_by_server: dict[str, list[Tool]],
    py_name_map: dict[str, str],
) -> str:
    """Generate brief server overview. Extracted for testing."""
    return generate_server_overview(tools_by_server, py_name_map)


def _search_tools_logic(
    query: str,
    tools_by_server: dict[str, list[Tool]],
    py_name_map: dict[str, str],
) -> str:
    """Search tools by keyword. Extracted for testing."""
    if not query.strip():
        return _overview_logic(tools_by_server, py_name_map)

    keywords = query.lower().split()
    matches: dict[str, list[Tool]] = {}

    for server_name, tools in tools_by_server.items():
        py_name = server_name_to_py(server_name)
        for tool in tools:
            searchable = f"{tool.name} {tool.description or ''}".lower()
            if all(kw in searchable for kw in keywords):
                matches.setdefault(server_name, []).append(tool)

    if not matches:
        server_names = sorted(py_name_map.keys())
        return f"No tools found for '{query}'. Available servers: {', '.join(server_names)}"

    sections = []
    for server_name, tools in matches.items():
        py_name = server_name_to_py(server_name)
        sections.append(generate_stubs_for_server(py_name, tools))

    return "\n\n".join(sections)


@mcp.tool()
async def list_available_tools(ctx: Context) -> str:
    """
    List all connected MCP servers with tool counts.
    Returns a brief overview. Use search_tools(query) to find specific tools with full signatures.
    """
    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]
    py_name_map = pool.py_name_map()
    tools_by_server = pool.get_all_tools()

    if not py_name_map:
        return "No MCP servers connected."

    overview = _overview_logic(tools_by_server, py_name_map)

    if pool.failed:
        lines = ["", "# === Failed to connect ==="]
        for name, err in pool.failed.items():
            lines.append(f"# {name}: {err}")
        overview += "\n".join(lines)

    return overview


@mcp.tool()
async def search_tools(query: str, ctx: Context) -> str:
    """
    Search for MCP tools by keyword. Returns full Python stubs for matching tools.

    Args:
        query: Space-separated keywords. All keywords must match tool name or description.
               Examples: "sql query", "read file", "documentation"
    """
    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]
    return _search_tools_logic(query, pool.get_all_tools(), pool.py_name_map())


@mcp.tool()
async def execute_code(code: str, ctx: Context, timeout: float = 60.0) -> str:
    """
    Execute Python code with access to all connected MCP tools.

    Each MCP server is available as a Python object named after the server
    (hyphens replaced with underscores). Call tools using:
        result = await server_name.tool_name(param="value")

    Use list_available_tools first to discover servers, then
    search_tools to get full signatures for specific tools.

    Args:
        code: Python code to execute. Top-level await is supported.
        timeout: Maximum execution time in seconds (default 60).
    """
    from .executor import CodeExecutor

    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]
    executor = CodeExecutor(pool)

    result = await executor.execute(code, timeout=timeout)

    lines = []
    if result["output"]:
        lines.append(result["output"].rstrip())
    if not result["success"]:
        lines.append(f"\n[ERROR] {result['error']}")

    return "\n".join(lines) if lines else "(no output)"


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/test_search_tools.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Run all tests**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Smoke test — run code-runner end-to-end**

Run: `cd C:/mcp-servers/code-runner && timeout 60 uv run code-runner 2>&1 | grep -E "(Connected|Failed)" | head -15`
Expected: All servers connected, no failures

- [ ] **Step 7: Commit**

```bash
cd C:/mcp-servers/code-runner
git add src/code_runner/server.py tests/test_search_tools.py
git commit -m "feat: replace full tool dump with brief overview + keyword search"
```

---

## Final Verification

### Task 9: Full integration check

- [ ] **Step 1: Run all tests**

Run: `cd C:/mcp-servers/code-runner && uv run -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Run code-runner as MCP server — verify startup**

Run: `cd C:/mcp-servers/code-runner && timeout 60 uv run code-runner 2>&1 | grep -E "(Connected|Failed|INFO|WARNING)" | head -20`
Expected: All servers connected, clean startup

- [ ] **Step 3: Final commit if any loose changes**

```bash
cd C:/mcp-servers/code-runner
git status
# If clean: done. If changes: stage and commit.
```

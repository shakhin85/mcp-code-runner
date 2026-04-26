"""
Executes LLM-generated Python code with injected MCP tool wrappers.
Includes AST validation, safe builtins, and auto-display of last expression.
"""

import ast
import asyncio
import builtins
import collections
import datetime
import decimal
import json
import math
import re
import signal
import sys
import time
import traceback
import types
import uuid
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.types import Tool

from .config_reader import server_name_to_py
from .metrics import MetricsRecorder
from .sql_limit import inject_limit
from .workspace import WorkspaceManager, safe_open, WorkspaceError


SAFE_BUILTINS = {
    "print", "len", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "list", "dict", "set", "tuple", "str",
    "int", "float", "bool", "isinstance", "type", "repr",
    "min", "max", "sum", "abs", "round", "any", "all",
    "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "Exception",
}

# Safe asyncio subset — no subprocess access
_SAFE_ASYNCIO = types.ModuleType("asyncio")
_SAFE_ASYNCIO.sleep = asyncio.sleep
_SAFE_ASYNCIO.gather = asyncio.gather
_SAFE_ASYNCIO.wait_for = asyncio.wait_for

# Pre-imported stdlib modules available in sandbox without `import` statements.
# Each is filesystem/process-free and safe for arbitrary LLM-generated code.
SAFE_MODULES = {
    "re": re,
    "datetime": datetime,
    "decimal": decimal,
    "math": math,
    "collections": collections,
}

# Namespace for parsing Python-repr responses from MCP servers (e.g. postgres
# returns str(list_of_dicts) containing Decimal(...) and datetime literals).
_REPR_NAMESPACE: dict[str, Any] = {
    "Decimal": decimal.Decimal,
    "datetime": datetime,
    "UUID": uuid.UUID,
    "True": True,
    "False": False,
    "None": None,
}

_REPR_ALLOWED_NODES: tuple = (
    ast.Expression, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Name, ast.Load, ast.UnaryOp, ast.USub, ast.UAdd,
    ast.Call, ast.Attribute, ast.keyword,
)


def _validate_repr_ast(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _REPR_ALLOWED_NODES):
            raise ValueError(f"disallowed node: {type(node).__name__}")
        if isinstance(node, ast.Name):
            if node.id not in _REPR_NAMESPACE:
                raise ValueError(f"disallowed name: {node.id}")
        if isinstance(node, ast.Attribute):
            root: ast.AST = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if not isinstance(root, ast.Name) or root.id not in _REPR_NAMESPACE:
                raise ValueError("disallowed attribute root")


DEFAULT_MAX_OUTPUT_BYTES = 20000
DEFAULT_AUTO_LIMIT = 500

SESSION_TTL = 600.0  # seconds; idle sessions older than this are evicted
MAX_SESSIONS = 20    # LRU cap to bound memory

DEFAULT_WORKSPACE_ROOT = Path.home() / ".cache" / "code-runner" / "workspace"

# Server-name prefix → sqlglot dialect for auto-LIMIT injection.
# Exact-match "mssql" included; postgres variants matched by prefix.
_SQL_DIALECT_BY_SERVER: dict[str, str] = {"mssql": "mssql"}
_SQL_PREFIXES: tuple[str, ...] = ("postgres",)
_SQL_TOOL_NAMES: frozenset[str] = frozenset({"execute_sql"})
_SQL_ARG_NAMES: tuple[str, ...] = ("sql", "query")


def _dialect_for_server(server_name: str) -> str | None:
    if server_name in _SQL_DIALECT_BY_SERVER:
        return _SQL_DIALECT_BY_SERVER[server_name]
    for prefix in _SQL_PREFIXES:
        if server_name.startswith(prefix):
            return "postgres"
    return None


class _SessionState:
    __slots__ = ("user_vars", "last_access")

    def __init__(self) -> None:
        self.user_vars: dict[str, Any] = {}
        self.last_access: float = time.monotonic()


def _truncate_output(output: str, max_bytes: int) -> str:
    """Truncate output to max_bytes (UTF-8 safe) with an informative footer.

    Protects the model's context from runaway MCP responses (SELECT * without
    LIMIT, large file dumps, etc.). Pass max_bytes <= 0 to disable.
    """
    if max_bytes <= 0 or not output:
        return output
    encoded = output.encode("utf-8")
    total_bytes = len(encoded)
    if total_bytes <= max_bytes:
        return output
    kept = encoded[:max_bytes].decode("utf-8", errors="ignore")
    footer = (
        f"\n\n... [TRUNCATED: output was {total_bytes} bytes, kept first "
        f"{max_bytes}. Use SQL LIMIT/TOP, pagination, or narrow your query.]"
    )
    return kept + footer


class _SandboxTimeout(BaseException):
    """Raised by SIGALRM handler when user code exceeds its hard timeout.

    Inherits from BaseException (not Exception) so user code using
    `except Exception:` cannot accidentally swallow the timeout and keep
    spinning. KeyboardInterrupt and SystemExit use the same trick.
    """


_SIGNAL_AVAILABLE = sys.platform != "win32" and hasattr(signal, "SIGALRM")


def _sandbox_alarm_handler(signum, frame):
    raise _SandboxTimeout("CPU-bound execution exceeded hard timeout")


# Module-level storage for the previous handler so we can restore it.
_prev_alarm_handler: Any = None


def _arm_sandbox_alarm(seconds: float) -> bool:
    """Install SIGALRM backup timeout. Returns True if armed.

    Fails silently and returns False on Windows (no SIGALRM) or when called
    off the main thread (signal.signal raises ValueError).
    """
    global _prev_alarm_handler
    if not _SIGNAL_AVAILABLE:
        return False
    try:
        _prev_alarm_handler = signal.signal(signal.SIGALRM, _sandbox_alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.01))
        return True
    except (ValueError, OSError):
        _prev_alarm_handler = None
        return False


def _disarm_sandbox_alarm() -> None:
    global _prev_alarm_handler
    if not _SIGNAL_AVAILABLE:
        return
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if _prev_alarm_handler is not None:
            signal.signal(signal.SIGALRM, _prev_alarm_handler)
    except (ValueError, OSError):
        pass
    finally:
        _prev_alarm_handler = None


def _parse_python_repr(text: str) -> Any:
    """Safely evaluate a Python repr string containing Decimal/datetime/UUID.

    Validates the AST against a strict whitelist before evaluation so no
    arbitrary code can run — only literal nodes and calls to known safe types.
    Raises ValueError on any disallowed construct or SyntaxError.
    """
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"repr parse error: {e}") from e
    _validate_repr_ast(tree)
    return eval(
        compile(tree, "<mcp-repr>", "eval"),
        {"__builtins__": {}},
        _REPR_NAMESPACE,
    )


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


_RESULT_SENTINEL = "__cr_result__"


def _transform_last_expr(code: str) -> str:
    """If last statement is a bare expression, assign it to a sentinel for auto-display.

    The code runs at top level (not inside a function), so we cannot use `return`.
    Instead we rewrite `x + 1` into `__cr_result__ = x + 1`, then pick the sentinel
    out of the namespace after execution.

    Note: ast.unparse strips comments from user code. Accepted trade-off.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    if not tree.body:
        return code

    last = tree.body[-1]
    if isinstance(last, ast.Expr):
        assign = ast.Assign(
            targets=[ast.Name(id=_RESULT_SENTINEL, ctx=ast.Store())],
            value=last.value,
        )
        ast.copy_location(assign, last)
        tree.body[-1] = assign
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    return code


class _ToolNamespace:
    """Proxy object representing a single MCP server's tools in exec namespace."""

    def __init__(
        self,
        server_name: str,
        session: ClientSession,
        tools: list[Tool],
        auto_limit: int = 0,
        stats: dict[str, int] | None = None,
        recorder: "MetricsRecorder | None" = None,
    ):
        self._server_name = server_name
        self._session = session
        self._tools = {t.name: t for t in tools}
        self._auto_limit = auto_limit
        self._sql_dialect = _dialect_for_server(server_name) if auto_limit > 0 else None
        self._stats = stats
        self._recorder = recorder

        for tool in tools:
            py_attr = tool.name.replace("-", "_")
            setattr(self, py_attr, self._make_wrapper(tool.name))

    def _maybe_inject_limit(self, tool_name: str, kwargs: dict) -> bool:
        """Mutate kwargs to add default LIMIT/TOP. Returns True if changed."""
        if self._sql_dialect is None or tool_name not in _SQL_TOOL_NAMES:
            return False
        for arg in _SQL_ARG_NAMES:
            original = kwargs.get(arg)
            if isinstance(original, str) and original:
                rewritten = inject_limit(
                    original, self._auto_limit, self._sql_dialect
                )
                if rewritten != original:
                    kwargs[arg] = rewritten
                    return True
                return False
        return False

    def _make_wrapper(self, tool_name: str):
        session = self._session
        server = self._server_name

        async def wrapper(**kwargs):
            limit_applied = self._maybe_inject_limit(tool_name, kwargs)
            if limit_applied and self._stats is not None:
                self._stats["auto_limit_hits"] += 1
            start = time.monotonic()
            success = True
            error: str | None = None
            out_bytes = 0
            try:
                result = await session.call_tool(tool_name, kwargs)
                texts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        texts.append(content.text)
                    elif hasattr(content, "data"):
                        texts.append(json.dumps(content.data, ensure_ascii=False))
                combined = "\n".join(texts)
                out_bytes = len(combined.encode("utf-8"))
                stripped = combined.strip()
                # Try JSON first (fast path, most MCP servers), then fall back to
                # the safe Python-repr parser for servers like postgres that return
                # str(list_of_dicts) with Decimal(...) and single-quoted strings.
                if stripped.startswith(("{", "[")):
                    try:
                        return json.loads(combined)
                    except json.JSONDecodeError:
                        pass
                    try:
                        return _parse_python_repr(stripped)
                    except ValueError:
                        pass
                return combined
            except BaseException as e:
                success = False
                error = f"{type(e).__name__}: {e}"
                raise
            finally:
                if self._stats is not None:
                    self._stats["tool_calls"] += 1
                if self._recorder is not None:
                    try:
                        self._recorder.record({
                            "kind": "tool_call",
                            "server": server,
                            "tool": tool_name,
                            "duration_ms": round((time.monotonic() - start) * 1000, 2),
                            "success": success,
                            "bytes": out_bytes,
                            "limit_applied": limit_applied,
                            "error": error,
                        })
                    except Exception:
                        pass

        wrapper.__name__ = tool_name
        wrapper.__qualname__ = f"{server}.{tool_name}"
        doc = self._tools[tool_name].description or ""
        wrapper.__doc__ = doc
        return wrapper

    def __repr__(self):
        tool_names = list(self._tools.keys())
        return f"<MCP:{self._server_name} tools={tool_names}>"


class CodeExecutor:
    def __init__(
        self,
        pool,
        recorder: "MetricsRecorder | None" = None,
        workspace: "WorkspaceManager | None" = None,
    ):
        self.pool = pool
        self.recorder = recorder
        self.workspace = workspace if workspace is not None else WorkspaceManager(DEFAULT_WORKSPACE_ROOT)
        # Signals are process-global and can only be armed from the main
        # thread — serialize executions so two concurrent calls can't clobber
        # each other's SIGALRM state.
        self._exec_lock = asyncio.Lock()
        self._sessions: dict[str, _SessionState] = {}

    def _evict_expired_sessions(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s.last_access > SESSION_TTL
        ]
        for sid in expired:
            del self._sessions[sid]
            self.workspace.cleanup_session(sid)

    def _evict_lru_if_over_capacity(self) -> None:
        while len(self._sessions) > MAX_SESSIONS:
            lru_sid = min(
                self._sessions.items(),
                key=lambda item: item[1].last_access,
            )[0]
            del self._sessions[lru_sid]
            self.workspace.cleanup_session(lru_sid)

    def _get_or_create_session(self, session_id: str) -> _SessionState:
        self._evict_expired_sessions()
        if session_id not in self._sessions:
            self._sessions[session_id] = _SessionState()
            self._evict_lru_if_over_capacity()
        state = self._sessions[session_id]
        state.last_access = time.monotonic()
        return state

    def _build_namespace(
        self,
        session_id: str | None = None,
        auto_limit: int = 0,
        stats: dict[str, int] | None = None,
    ) -> tuple[dict[str, Any], set[str]]:
        # Server-side use of getattr/hasattr to build whitelist — NOT exposed to user sandbox
        safe_builtins = {name: getattr(builtins, name) for name in SAFE_BUILTINS if hasattr(builtins, name)}
        namespace: dict[str, Any] = {
            "__builtins__": safe_builtins,
            "asyncio": _SAFE_ASYNCIO,
            "json": json,
            **SAFE_MODULES,
        }

        # Workspace-bound open(): when session_id is set, writes go into
        # <workspace>/<session_id>/. When unset, the call raises so user code
        # can't accidentally touch the host FS.
        if session_id is not None:
            wm = self.workspace
            sid = session_id
            def _user_open(path, mode="r", *, max_bytes=None):
                kwargs = {} if max_bytes is None else {"max_bytes": max_bytes}
                return safe_open(wm, sid, path, mode, **kwargs)
            namespace["open"] = _user_open
        else:
            def _denied(*_a, **_kw):
                raise WorkspaceError("open() requires session_id")
            namespace["open"] = _denied

        for server_name, session in self.pool.sessions.items():
            py_name = server_name_to_py(server_name)
            tools = self.pool.tools.get(server_name, [])
            namespace[py_name] = _ToolNamespace(
                server_name, session, tools,
                auto_limit=auto_limit,
                stats=stats,
                recorder=self.recorder,
            )

        # Snapshot framework-provided names so we can later diff to
        # extract only the user's own variables for persistence.
        framework_names = set(namespace.keys())

        # Inject persistent user vars AFTER framework so user can shadow.
        if session_id is not None:
            state = self._get_or_create_session(session_id)
            namespace.update(state.user_vars)

        return namespace, framework_names

    def _extract_user_vars(
        self,
        namespace: dict[str, Any],
        framework_names: set[str],
    ) -> dict[str, Any]:
        return {
            k: v for k, v in namespace.items()
            if k not in framework_names
            and k != "print"
            and not (k.startswith("__") and k.endswith("__"))
        }

    async def execute(
        self,
        code: str,
        timeout: float = 60.0,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        session_id: str | None = None,
        auto_limit: int = DEFAULT_AUTO_LIMIT,
    ) -> dict[str, Any]:
        # Signals are process-global, so only one execution may arm SIGALRM
        # at a time. The lock is also cheap for the common single-client
        # MCP stdio case.
        async with self._exec_lock:
            return await self._execute_locked(
                code, timeout, max_output_bytes, session_id, auto_limit
            )

    async def _execute_locked(
        self,
        code: str,
        timeout: float,
        max_output_bytes: int,
        session_id: str | None,
        auto_limit: int,
    ) -> dict[str, Any]:
        start_exec = time.monotonic()
        stats: dict[str, int] = {"tool_calls": 0, "auto_limit_hits": 0}

        def finalize(success: bool, output: str, error: str | None) -> dict[str, Any]:
            truncated = _truncate_output(output, max_output_bytes)
            result = {"success": success, "output": truncated, "error": error}
            if self.recorder is not None:
                try:
                    raw_bytes = len(output.encode("utf-8")) if output else 0
                    sent_bytes = len(truncated.encode("utf-8")) if truncated else 0
                    self.recorder.record({
                        "kind": "execute_code",
                        "duration_ms": round((time.monotonic() - start_exec) * 1000, 2),
                        "success": success,
                        "output_bytes_raw": raw_bytes,
                        "output_bytes_sent": sent_bytes,
                        "truncated": raw_bytes > sent_bytes,
                        "tool_calls": stats["tool_calls"],
                        "auto_limit_hits": stats["auto_limit_hits"],
                        "session_id": session_id,
                        "error": error,
                    })
                except Exception:
                    pass
            return result

        # Pipeline: 1. validate → 2. transform → 3. wrap → 4. exec
        try:
            validate_code(code)
        except ValueError as e:
            return finalize(False, "", str(e))

        code = _transform_last_expr(code)

        namespace, framework_names = self._build_namespace(
            session_id, auto_limit, stats
        )

        output_lines: list[str] = []

        def captured_print(*args, sep=" ", end="\n", **_kwargs):
            output_lines.append(sep.join(str(a) for a in args) + end)

        namespace["print"] = captured_print
        # Clear any leftover auto-display sentinel from a previous exec in the
        # same session so its presence truly reflects the current run.
        namespace.pop(_RESULT_SENTINEL, None)

        # Compile with top-level-await support so user code runs at module
        # scope. Assignments like `x = 42` land directly in `namespace`,
        # which is how persistent sessions see them across calls.
        try:
            compiled = compile(
                code,
                "<user>",
                "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            )
        except SyntaxError as e:
            return finalize(False, "", f"SyntaxError: {e}")
        except Exception as e:
            return finalize(False, "", f"CompileError: {e}\n{traceback.format_exc()}")

        # Arm SIGALRM as a backup hard timeout so pure-CPU loops in user code
        # (which never yield to the event loop, making asyncio.wait_for
        # ineffective) can still be interrupted at the OS level.
        alarm_armed = _arm_sandbox_alarm(timeout + 0.5)

        try:
            # eval() on a top-level-await code object returns a coroutine if
            # the source contained `await`; otherwise the synchronous code
            # runs during eval and None is returned.
            maybe_coro = eval(compiled, namespace)
            if asyncio.iscoroutine(maybe_coro):
                await asyncio.wait_for(maybe_coro, timeout=timeout)

            output = "".join(output_lines)
            result = namespace.pop(_RESULT_SENTINEL, None)
            if result is not None:
                if isinstance(result, (dict, list)):
                    output += json.dumps(result, ensure_ascii=False, indent=2)
                else:
                    output += str(result)
            if session_id is not None:
                state = self._sessions[session_id]
                state.user_vars = self._extract_user_vars(namespace, framework_names)
                state.last_access = time.monotonic()
            return finalize(True, output, None)

        except _SandboxTimeout:
            return finalize(
                False,
                "".join(output_lines),
                f"Execution timed out after {timeout}s (CPU-bound loop detected by SIGALRM)",
            )
        except asyncio.TimeoutError:
            return finalize(
                False,
                "".join(output_lines),
                f"Execution timed out after {timeout}s",
            )
        except Exception as e:
            return finalize(
                False,
                "".join(output_lines),
                f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
            )
        finally:
            if alarm_armed:
                _disarm_sandbox_alarm()

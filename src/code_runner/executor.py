"""
Executes LLM-generated Python code with injected MCP tool wrappers.
Includes AST validation, safe builtins, and auto-display of last expression.
"""

import ast
import asyncio
import base64
import bisect
import builtins
import collections
import dataclasses
import datetime
import decimal
import functools
import hashlib
import heapq
import itertools
import json
import math
import random
import re
import signal
import statistics
import string
import sys
import textwrap
import time
import traceback
import types
import uuid
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.types import Tool

from . import prelude as _prelude
from .config_reader import server_name_to_py
from .metrics import MetricsRecorder
from .sql_limit import inject_limit
from .skills import SkillsNamespace
from .workspace import WorkspaceManager, safe_open, WorkspaceError


SAFE_BUILTINS = {
    "print", "len", "range", "enumerate", "zip", "map", "filter",
    "sorted", "reversed", "list", "dict", "set", "tuple", "str",
    "int", "float", "bool", "isinstance", "type", "repr",
    "min", "max", "sum", "abs", "round", "any", "all",
    # iterators / numeric / data — pure, no reflection or FS/process reach
    "next", "iter", "divmod", "pow", "hex", "bin", "oct", "chr", "ord",
    "complex", "frozenset", "bytes", "bytearray", "slice", "hash",
    "callable", "format",
    "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "Exception",
    "NameError", "AttributeError", "StopIteration",
    "ZeroDivisionError", "AssertionError", "NotImplementedError",
    # DELIBERATELY EXCLUDED (sandbox escape via runtime-string dunder access,
    # which bypasses the AST dunder guard): getattr, hasattr, setattr, delattr,
    # vars, globals, locals, dir, eval, exec, compile, open, __import__, input.
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
    "time": time,
    "json": json,
    # Pure compute/data stdlib — no filesystem, process, or network reach.
    "itertools": itertools,
    "functools": functools,
    "statistics": statistics,
    "random": random,
    "string": string,
    "textwrap": textwrap,
    "dataclasses": dataclasses,
    "uuid": uuid,
    "hashlib": hashlib,
    "base64": base64,
    "heapq": heapq,
    "bisect": bisect,
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
# Per-single-tool-call byte cap. auto_limit caps ROWS; this caps BYTES, so a
# `SELECT * LIMIT 500` over wide/jsonb columns can't still materialize MBs.
# When a single tool response exceeds this, it is returned as a TRUNCATED STRING
# (not parsed into objects) with a marker, forcing the model to narrow the query.
# Pass 0 to disable.
DEFAULT_RAW_CAP = 262144  # 256 KB

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

# The filename passed to compile() for user code. Frames with this filename are
# the only ones worth showing — everything else (executor, asyncio internals) is
# harness noise that bloats the error and obscures the real line.
_USER_FILENAME = "<user>"


def _format_user_traceback(exc: BaseException) -> str:
    """Format an exception showing only the user-code frames.

    A raw ``traceback.format_exc()`` leaks the executor's own frames
    (``_execute_locked`` → ``asyncio.wait_for`` → ``return await fut``) before
    reaching ``<user>``. Those frames are pure noise: they never point at the
    user's bug and cost tokens on every error. We keep only ``<user>`` frames so
    the message reads as the exception line plus the relevant source lines.

    Falls back to the full traceback if no user frame is present (e.g. an error
    raised entirely inside the harness), so we never hide a genuine internal bug.
    """
    header = f"{type(exc).__name__}: {exc}"
    tb = exc.__traceback__
    if tb is None:
        return header
    user_frames = [fs for fs in traceback.extract_tb(tb) if fs.filename == _USER_FILENAME]
    if not user_frames:
        return f"{header}\n{traceback.format_exc()}"
    lines = ["Traceback (most recent call last):\n"]
    lines.extend(traceback.format_list(user_frames))
    return "".join(lines) + header


# Substrings of the exact TypeError messages CPython raises when code indexes or
# iterates a str/bytes as if it were a dict or list of dicts — the signature of
# "I assumed a tool result was an object but it came back as a string".
_SUBSCRIPT_ERROR_MARKERS = (
    "string indices must be integers",
    "'str' object is not subscriptable",
    "byte indices must be integers",
    "'bytes' object is not subscriptable",
)

_STR_RESULT_HINT = (
    "\n\nHINT: a tool result this run was returned as a STRING, not parsed into "
    "dict/list — either it exceeded raw_cap (truncated) or the server returned "
    "non-JSON. Indexing it like an object raises this error. Check `type(result)` "
    "and the RAW-CAP note in the result; narrow columns / add LIMIT, then re-run."
)


def _str_result_hint(exc: BaseException, stats: dict[str, int]) -> str:
    """Append a footgun hint iff a subscript error coincides with a str tool result.

    Fires only when both signals are present in the same run, so it never adds
    noise to an unrelated TypeError.
    """
    if stats.get("str_results", 0) <= 0:
        return ""
    msg = str(exc)
    if any(marker in msg for marker in _SUBSCRIPT_ERROR_MARKERS):
        return _STR_RESULT_HINT
    return ""


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
        raw_cap: int = 0,
    ):
        self._server_name = server_name
        self._session = session
        self._tools = {t.name: t for t in tools}
        self._auto_limit = auto_limit
        self._sql_dialect = _dialect_for_server(server_name) if auto_limit > 0 else None
        self._stats = stats
        self._recorder = recorder
        self._raw_cap = raw_cap

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
        raw_cap = self._raw_cap

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
                # Byte cap at the SOURCE: a single tool response over raw_cap is
                # returned as a truncated STRING (not parsed). out_bytes still
                # records the true raw size for metrics (see finally block).
                if raw_cap > 0 and out_bytes > raw_cap:
                    if self._stats is not None:
                        self._stats["str_results"] += 1
                    kept = combined.encode("utf-8")[:raw_cap].decode("utf-8", errors="ignore")
                    return (
                        kept
                        + f"\n\n...[RAW-CAP: {server}.{tool_name} returned {out_bytes} bytes, "
                        f"kept first {raw_cap}. Returned as a TRUNCATED STRING (not parsed into "
                        "objects) — narrow columns, aggregate, or add LIMIT, then re-run.]"
                    )
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
                if self._stats is not None:
                    self._stats["str_results"] += 1
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
        skills: "SkillsNamespace | None" = None,
    ):
        self.pool = pool
        self.recorder = recorder
        self.workspace = workspace if workspace is not None else WorkspaceManager(DEFAULT_WORKSPACE_ROOT)
        self.skills = skills
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
        raw_cap: int = 0,
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
                raw_cap=raw_cap,
            )

        if self.skills is not None:
            namespace["skills"] = self.skills
            # Skill code calling open(...) goes through the same workspace-bound
            # safe_open as the user's top-level code. The exec lock serializes
            # runs so this per-call rebind is safe.
            self.skills.bind("open", namespace["open"])
            # Short aliases for hot skill functions (fg_query, md_table, probe, ...).
            # See prelude.ALIASES for the full list. Missing skills are skipped.
            namespace.update(_prelude.build(self.skills))

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
            and k != "print_rows"
            and not (k.startswith("__") and k.endswith("__"))
        }

    async def execute(
        self,
        code: str,
        timeout: float = 60.0,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        session_id: str | None = None,
        auto_limit: int = DEFAULT_AUTO_LIMIT,
        raw_cap: int = DEFAULT_RAW_CAP,
    ) -> dict[str, Any]:
        # Signals are process-global, so only one execution may arm SIGALRM
        # at a time. The lock is also cheap for the common single-client
        # MCP stdio case.
        async with self._exec_lock:
            return await self._execute_locked(
                code, timeout, max_output_bytes, session_id, auto_limit, raw_cap
            )

    async def _execute_locked(
        self,
        code: str,
        timeout: float,
        max_output_bytes: int,
        session_id: str | None,
        auto_limit: int,
        raw_cap: int = DEFAULT_RAW_CAP,
    ) -> dict[str, Any]:
        start_exec = time.monotonic()
        stats: dict[str, int] = {"tool_calls": 0, "auto_limit_hits": 0, "str_results": 0}

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
            session_id, auto_limit, stats, raw_cap
        )

        output_lines: list[str] = []

        def captured_print(*args, sep=" ", end="\n", **_kwargs):
            output_lines.append(sep.join(str(a) for a in args) + end)

        namespace["print"] = captured_print

        def _print_rows(obj, n: int = 10):
            """Compact preview of tabular results: shape + head(n) + tail(3).

            Use instead of `print(rows)` when a query may return many/wide rows:
            it prints row/column shape and a sample, not the full dump.
            """
            rows = obj
            if isinstance(obj, dict):
                rows = obj.get("rows", obj.get("data", obj))
            if not isinstance(rows, list):
                captured_print(obj)
                return
            total = len(rows)
            cols = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else None
            header = f"[{total} rows" + (f" × {len(cols)} cols: {cols}]" if cols else "]")
            captured_print(header)
            for r in rows[:n]:
                captured_print(r)
            if total > n + 3:
                captured_print(f"... ({total - n - 3} more rows) ...")
                for r in rows[-3:]:
                    captured_print(r)
            elif total > n:
                for r in rows[n:]:
                    captured_print(r)

        namespace["print_rows"] = _print_rows
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
                _format_user_traceback(e) + _str_result_hint(e, stats),
            )
        finally:
            if alarm_armed:
                _disarm_sandbox_alarm()

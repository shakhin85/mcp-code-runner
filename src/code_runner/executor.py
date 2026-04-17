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
import textwrap
import traceback
import types
import uuid
from typing import Any

from mcp import ClientSession
from mcp.types import Tool

from .config_reader import server_name_to_py


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


DEFAULT_MAX_OUTPUT_BYTES = 20000


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
        ast.copy_location(ret, last)
        tree.body[-1] = ret
        ast.fix_missing_locations(tree)
        return ast.unparse(tree)

    return code


class _ToolNamespace:
    """Proxy object representing a single MCP server's tools in exec namespace."""

    def __init__(self, server_name: str, session: ClientSession, tools: list[Tool]):
        self._server_name = server_name
        self._session = session
        self._tools = {t.name: t for t in tools}

        for tool in tools:
            py_attr = tool.name.replace("-", "_")
            setattr(self, py_attr, self._make_wrapper(tool.name))

    def _make_wrapper(self, tool_name: str):
        session = self._session
        server = self._server_name

        async def wrapper(**kwargs):
            result = await session.call_tool(tool_name, kwargs)
            texts = []
            for content in result.content:
                if hasattr(content, "text"):
                    texts.append(content.text)
                elif hasattr(content, "data"):
                    texts.append(json.dumps(content.data, ensure_ascii=False))
            combined = "\n".join(texts)
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

        wrapper.__name__ = tool_name
        wrapper.__qualname__ = f"{server}.{tool_name}"
        doc = self._tools[tool_name].description or ""
        wrapper.__doc__ = doc
        return wrapper

    def __repr__(self):
        tool_names = list(self._tools.keys())
        return f"<MCP:{self._server_name} tools={tool_names}>"


class CodeExecutor:
    def __init__(self, pool):
        self.pool = pool
        # Signals are process-global and can only be armed from the main
        # thread — serialize executions so two concurrent calls can't clobber
        # each other's SIGALRM state.
        self._exec_lock = asyncio.Lock()

    def _build_namespace(self) -> dict[str, Any]:
        # Server-side use of getattr/hasattr to build whitelist — NOT exposed to user sandbox
        safe_builtins = {name: getattr(builtins, name) for name in SAFE_BUILTINS if hasattr(builtins, name)}
        namespace: dict[str, Any] = {
            "__builtins__": safe_builtins,
            "asyncio": _SAFE_ASYNCIO,
            "json": json,
            **SAFE_MODULES,
        }

        for server_name, session in self.pool.sessions.items():
            py_name = server_name_to_py(server_name)
            tools = self.pool.tools.get(server_name, [])
            namespace[py_name] = _ToolNamespace(server_name, session, tools)

        return namespace

    async def execute(
        self,
        code: str,
        timeout: float = 60.0,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        # Signals are process-global, so only one execution may arm SIGALRM
        # at a time. The lock is also cheap for the common single-client
        # MCP stdio case.
        async with self._exec_lock:
            return await self._execute_locked(code, timeout, max_output_bytes)

    async def _execute_locked(
        self,
        code: str,
        timeout: float,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        def finalize(success: bool, output: str, error: str | None) -> dict[str, Any]:
            return {
                "success": success,
                "output": _truncate_output(output, max_output_bytes),
                "error": error,
            }

        # Pipeline: 1. validate → 2. transform → 3. wrap → 4. exec
        try:
            validate_code(code)
        except ValueError as e:
            return finalize(False, "", str(e))

        code = _transform_last_expr(code)

        namespace = self._build_namespace()

        output_lines: list[str] = []

        def captured_print(*args, sep=" ", end="\n", **_kwargs):
            output_lines.append(sep.join(str(a) for a in args) + end)

        namespace["print"] = captured_print

        indented = textwrap.indent(code, "    ")
        wrapped = f"async def __user_code__():\n{indented}\n"

        try:
            exec(wrapped, namespace)
        except SyntaxError as e:
            return finalize(False, "", f"SyntaxError: {e}")
        except Exception as e:
            return finalize(False, "", f"CompileError: {e}\n{traceback.format_exc()}")

        # Arm SIGALRM as a backup hard timeout so pure-CPU loops in user code
        # (which never yield to the event loop, making asyncio.wait_for
        # ineffective) can still be interrupted at the OS level.
        alarm_armed = _arm_sandbox_alarm(timeout + 0.5)

        try:
            result = await asyncio.wait_for(
                namespace["__user_code__"](),
                timeout=timeout,
            )
            output = "".join(output_lines)
            if result is not None:
                if isinstance(result, (dict, list)):
                    output += json.dumps(result, ensure_ascii=False, indent=2)
                else:
                    output += str(result)
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

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
import contextlib
import dataclasses
import datetime
import decimal
import difflib
import functools
import hashlib
import heapq
import inspect
import itertools
import json
import math
import multiprocessing
import os
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
from typing import Any, Protocol

from mcp import ClientSession
from mcp.types import Tool

from . import prelude as _prelude
from .config_reader import server_name_to_py
from .metrics import MetricsRecorder, classify_error, code_fingerprint
from .skills import SkillsNamespace
from .sql_limit import inject_limit
from .workspace import WorkspaceError, WorkspaceManager, safe_open

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
# These do no filesystem/process/network work *themselves*, but several
# re-export os/sys/socket/etc. as ordinary attributes (uuid.os, dataclasses.sys,
# random._os, …) — a live escape until wrapped. They are NOT exposed raw: each
# is placed in the namespace as a _RestrictedModule (see below), which refuses
# any attribute that is itself a module. Dunder access is blocked too, because
# attributes like __class__, __globals__ and __subclasses__ walk out of the
# sandbox. __name__ does not: it yields a plain string. Blocking it only forced
# `print(type(e).__name__)` — the ordinary way to name an exception — to be
# rewritten, which is friction with no security to show for it.
ALLOWED_DUNDER_ATTRS = frozenset({"__name__"})

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

class _RestrictedModule:
    """Attribute-restricted view over a stdlib module exposed in the sandbox.

    The AST guard only blocks dunder attributes, but the pure-compute modules in
    SAFE_MODULES re-export *other* modules as ordinary, non-dunder attributes:
    ``uuid.os``, ``uuid.sys``, ``dataclasses.sys``, ``statistics.sys``,
    ``random._os``, ``collections._sys``, ``json.codecs`` … Any one of them is a
    one-liner to ``sys.modules`` and from there to ``os``/``socket``/``builtins``
    — a full escape that ignores every workspace and read-root control.

    This wrapper closes the class of vector by TYPE rather than by name: any
    attribute whose value is itself a module is refused, so ``uuid.uuid4`` still
    works while ``uuid.os`` raises AttributeError. Dunder access is refused too
    (belt-and-suspenders with the AST guard), except the harmless ``__name__``.
    """

    __slots__ = ("_mod", "_name")

    def __init__(self, mod: types.ModuleType) -> None:
        object.__setattr__(self, "_mod", mod)
        object.__setattr__(self, "_name", getattr(mod, "__name__", "?"))

    def __getattr__(self, attr: str) -> Any:
        # Only fires for names not in __slots__, so _mod/_name never recurse.
        if attr.startswith("__") and attr.endswith("__") and attr not in ALLOWED_DUNDER_ATTRS:
            raise AttributeError(
                f"dunder attribute '{self._name}.{attr}' is blocked in the sandbox"
            )
        value = getattr(self._mod, attr)
        if isinstance(value, types.ModuleType):
            raise AttributeError(
                f"module attribute '{self._name}.{attr}' is blocked in the sandbox "
                "(re-exported modules are an escape vector)"
            )
        return value

    def __repr__(self) -> str:
        return f"<sandboxed module '{self._name}'>"


# Built once at import — the wrappers are stateless, so a single instance per
# module is shared across all sandbox namespaces. Used in _build_namespace.
_SAFE_MODULES_WRAPPED: dict[str, _RestrictedModule] = {
    name: _RestrictedModule(mod) for name, mod in SAFE_MODULES.items()
}
_SAFE_ASYNCIO_WRAPPED = _RestrictedModule(_SAFE_ASYNCIO)
_JSON_WRAPPED = _RestrictedModule(json)


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
        if isinstance(node, ast.Name) and node.id not in _REPR_NAMESPACE:
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

# Cap on the error text carried in an MCPToolError message. A misbehaving
# backend can put a full traceback in an error result; keep it out of the
# model's context while still showing enough to diagnose.
MAX_ERROR_DETAIL = 4000

# Cap on the `error` field of a run result. `output` is already truncated, but
# the error was not: `raise ValueError('q' * 300000)` put 300 KB straight into
# the model's context. A raised exception's message is attacker- and
# accident-reachable, so cap it the same way — enough to diagnose, not a bomb.
MAX_ERROR_BYTES = 8192

SESSION_TTL = 600.0  # seconds; idle sessions older than this are evicted
MAX_SESSIONS = 20    # LRU cap to bound memory

DEFAULT_WORKSPACE_ROOT = Path.home() / ".cache" / "code-runner" / "workspace"

# --- Subprocess isolation (P0.1) -------------------------------------------
# Running user code in the server's own interpreter means one escape, OOM, or
# off-main-thread CPU loop takes the server (and every live MCP session's
# credentials) with it. When enabled, each call runs in a fresh, rlimited,
# env-scrubbed child process instead; MCP calls are proxied back to the parent.
# Default since the child reached feature parity (skills/prelude) and the
# forkserver start method brought per-call cost to ~14 ms; set
# CODE_RUNNER_ISOLATION=inprocess to run in the server's own interpreter.
_ISOLATION_DEFAULT = os.environ.get("CODE_RUNNER_ISOLATION", "subprocess").strip().lower()
_MEM_LIMIT_MB_DEFAULT = int(os.environ.get("CODE_RUNNER_MEM_LIMIT_MB", "2048"))
_FSIZE_LIMIT_MB_DEFAULT = int(os.environ.get("CODE_RUNNER_FSIZE_LIMIT_MB", "64"))

# rlimits live in the `resource` module (POSIX only), so isolation is POSIX-only.
_SUBPROCESS_AVAILABLE = sys.platform != "win32"

# Start method matters for the security boundary, not just speed. Plain `fork`
# is never acceptable: the child would inherit the parent's heap — live MCP
# sessions and credentials — readable via /proc/self/mem after an escape.
# `forkserver` does NOT: its server process is fork+exec'd into a brand-new
# interpreter, so children inherit only what that clean ancestor preloaded.
# Measured on this repo (probe: `fork` sees a parent-only string, `forkserver`
# and `spawn` do not): spawn 714 ms/call, forkserver 14 ms/call. Same boundary,
# 50× cheaper — so forkserver is the default and spawn stays available for hosts
# where it is unavailable or distrusted.
_START_METHOD_DEFAULT = (
    os.environ.get("CODE_RUNNER_START_METHOD", "forkserver").strip().lower()
)
_AVAILABLE_START_METHODS = multiprocessing.get_all_start_methods()
if _START_METHOD_DEFAULT in _AVAILABLE_START_METHODS:
    _MP_START_METHOD = _START_METHOD_DEFAULT
elif "spawn" in _AVAILABLE_START_METHODS:
    _MP_START_METHOD = "spawn"
else:
    _MP_START_METHOD = _AVAILABLE_START_METHODS[0]
if _MP_START_METHOD == "forkserver":
    # Preload the worker (and with it this module) in the clean ancestor, so
    # each child forks with the imports already done. Explicit, so the default
    # `__main__` preload — which would import and construct the whole MCP
    # server in the ancestor — never runs.
    with contextlib.suppress(Exception):
        multiprocessing.get_context("forkserver").set_forkserver_preload(
            ["code_runner.subprocess_worker"]
        )
_EOF = object()  # sentinel: the worker pipe closed (child killed or crashed)

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

    A truncated result always carries a machine-readable
    ``<system_hint>truncated: shown {shown_bytes} of {total_bytes} bytes</system_hint>``
    marker, so a consuming model can detect partial output instead of
    silently treating the truncated slice as the whole result.
    """
    if max_bytes <= 0 or not output:
        return output
    encoded = output.encode("utf-8")
    total_bytes = len(encoded)
    if total_bytes <= max_bytes:
        return output
    kept = encoded[:max_bytes].decode("utf-8", errors="ignore")
    shown_bytes = len(kept.encode("utf-8"))
    footer = (
        f"\n\n... [TRUNCATED: output was {total_bytes} bytes, kept first "
        f"{max_bytes}. Use SQL LIMIT/TOP, pagination, or narrow your query.]"
        f"\n<system_hint>truncated: shown {shown_bytes} of {total_bytes} bytes</system_hint>"
    )
    return kept + footer


def _truncate_error(error: str | None, max_bytes: int) -> str | None:
    """Cap the run's error text (UTF-8 safe) with a marker. None/short passes through."""
    if error is None or max_bytes <= 0:
        return error
    encoded = error.encode("utf-8")
    total = len(encoded)
    if total <= max_bytes:
        return error
    kept = encoded[:max_bytes].decode("utf-8", errors="ignore")
    kept_bytes = len(kept.encode("utf-8"))
    return (
        kept
        + f"\n...[ERROR TRUNCATED: {total} bytes, kept first {max_bytes}]"
        + f"\n<system_hint>error truncated: shown {kept_bytes} of {total} bytes</system_hint>"
    )


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _append_cost_footer(output: str, stats: dict[str, int], elapsed_ms: float) -> str:
    """Append a one-line cost receipt when the run actually called MCP tools.

    The whole point of running code here is that raw tool output stays in the
    sandbox and only the summary reaches the model's context. That saving was
    invisible — so was its absence. This line makes both visible: a single call
    whose raw bytes land in context unchanged is a run that should have been a
    direct MCP call, and the footer says so instead of quietly looking useful.
    """
    calls = stats.get("tool_calls", 0)
    if calls <= 0:
        return output

    raw = stats.get("raw_tool_bytes", 0)
    sent = len(output.encode("utf-8")) if output else 0
    line = (
        f"[cr] {calls} tool call{'s' if calls != 1 else ''} · "
        f"{_fmt_bytes(raw)} raw → {_fmt_bytes(sent)} in context"
    )
    if raw > 0:
        saved = 100 * (1 - min(sent, raw) / raw)
        line += f" ({saved:.0f}% saved)"
    line += f" · {elapsed_ms:.0f}ms"

    if calls == 1 and raw > 0 and sent >= raw * 0.9:
        line += (
            "\n[cr] no aggregation happened here — one tool call, output passed through. "
            "A direct MCP call would have cost the same and been simpler."
        )
    return f"{output}\n{line}" if output else line


class _SandboxTimeout(BaseException):
    """Raised by SIGALRM handler when user code exceeds its hard timeout.

    Inherits from BaseException (not Exception) so user code using
    `except Exception:` cannot accidentally swallow the timeout and keep
    spinning. KeyboardInterrupt and SystemExit use the same trick.
    """


class MCPToolError(RuntimeError):
    """Raised when an MCP tool call returns an error result (isError=True).

    A tool that fails puts the failure detail in `content` and sets isError,
    rather than raising over the wire. Without this the wrapper would return
    that error text as an ordinary string — indistinguishable from a real
    result — so the model would treat a failure as data. Subclasses
    RuntimeError so sandboxed user code can catch it by builtin name
    (`except RuntimeError` / `except Exception`); the class itself is not
    importable inside the sandbox, but `type(e).__name__` still reads
    "MCPToolError" for diagnosis.
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
            preloaded = {"asyncio", *SAFE_MODULES}
            already = [n for n in names if n.split(".")[0] in preloaded]
            hint = ""
            if already:
                hint = (
                    f" — {', '.join(already)} уже preloaded в sandbox: "
                    f"убери import и используй напрямую (top-level await поддержан)"
                )
            else:
                # `import os` alone is 17 of 52 import-failures over 7 days:
                # without the roster the caller retries with another module and
                # burns a round-trip per guess.
                hint = (
                    " — sandbox не даёт os/сеть/произвольные модули; preloaded: "
                    f"{', '.join(sorted(preloaded))}. Файлы — через open() "
                    "(session-workspace), MCP-серверы — через их прокси-объекты"
                )
            raise ValueError(
                f"import statements are not allowed: {', '.join(names)}{hint}"
            )

        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and node.attr.endswith("__")
            and node.attr not in ALLOWED_DUNDER_ATTRS
        ):
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


def _str_result_hint(exc: "BaseException | str", stats: dict[str, int]) -> str:
    """Append a footgun hint iff a subscript error coincides with a str tool result.

    Fires only when both signals are present in the same run, so it never adds
    noise to an unrelated TypeError. Accepts an already-formatted error string
    too: under subprocess isolation the exception dies with the child, but the
    parent holds the stats, so this is the one hint applied on the parent side.
    """
    if stats.get("str_results", 0) <= 0:
        return ""
    msg = str(exc)
    if any(marker in msg for marker in _SUBSCRIPT_ERROR_MARKERS):
        return _STR_RESULT_HINT
    return ""


# CPython names the function but never its accepted parameters, so a caller who
# guessed a kwarg has to guess again on the next run. These match the two ways
# that guess fails: an extra kwarg, or a positional the caller passed as kwarg.
_SIG_ERROR_RES = (
    re.compile(r"(\w+)\(\) got an unexpected keyword argument '([^']+)'"),
    re.compile(r"(\w+)\(\) missing \d+ required positional argument"),
    re.compile(r"(\w+)\(\) takes \d+ positional argument"),
)


def _signature_hint(exc: BaseException, skills: "SkillsNamespace | None") -> str:
    """Append the real signature when a skill call fails on its arguments.

    Signatures live in a cheatsheet the caller may not have read; without this
    the only feedback is "unexpected keyword argument 'X'", which costs one
    round-trip per wrong guess. Resolving the name against the loaded skills
    turns that into a single corrected call.
    """
    if skills is None or not isinstance(exc, TypeError):
        return ""
    msg = str(exc)
    fn_name = ""
    for pattern in _SIG_ERROR_RES:
        m = pattern.search(msg)
        if m:
            fn_name = m.group(1)
            break
    if not fn_name:
        return ""
    lines = []
    for skill_name, fn in skills.find_callables(fn_name):
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        lines.append(f"skills.{skill_name}.{fn_name}{sig}")
    if not lines:
        return ""
    # Named after the skill path, not "принимаемые параметры": a user-defined
    # function can share the name, and then these are merely the skills that
    # also answer to it — the caller must see which object the signature is for.
    return "\n\nHINT: сигнатуры skills-функций с этим именем —\n  " + "\n  ".join(lines)


_NAME_ERROR_RE = re.compile(r"name '([^']+)' is not defined")


def _name_error_hint(exc: BaseException, namespace: dict[str, Any]) -> str:
    """Append the sandbox roster when user code references an unknown name.

    The dominant NameError in production is a guessed MCP-proxy name
    (``postgres_lime`` vs ``postgres_lime_prod``): the caller never sees the
    real roster, so each guess costs a full round-trip. Close matches go
    first; the proxy list follows because that is the namespace the caller
    was almost certainly reaching for.
    """
    if not isinstance(exc, NameError):
        return ""
    m = _NAME_ERROR_RE.search(str(exc))
    if not m:
        return ""
    missing = m.group(1)
    visible = sorted(
        name for name in namespace
        if not name.startswith("_") and name != "__builtins__"
    )
    close = difflib.get_close_matches(missing, visible, n=3, cutoff=0.6)
    proxies = sorted(
        name for name, value in namespace.items()
        if isinstance(value, _ToolNamespace)
    )
    parts = []
    if close:
        parts.append(f"похожие имена в песочнице: {', '.join(close)}")
    if proxies:
        parts.append(f"доступные MCP-прокси: {', '.join(proxies)}")
    if not parts:
        return ""
    return "\n\nHINT: имя '" + missing + "' в песочнице не определено; " + "; ".join(parts)


# The exact shape skill hard-validators raise: "context: <=500 chars, got 608".
# The constraint is machine-readable, so the fix (slice to the limit) can be
# stated instead of leaving the caller to re-derive it.
_LIMIT_ERROR_RE = re.compile(r"<=\s*(\d+)\s+chars, got (\d+)")


def _limit_error_hint(exc: BaseException) -> str:
    """Turn a skills length-validator ValueError into an actionable fix.

    The validator names the limit but not the remedy; without this the whole
    execute_code block dies and the caller retries blind. Truncation is never
    done silently — the hint tells the caller to slice explicitly so the cut
    is visible in their own code.
    """
    if not isinstance(exc, ValueError):
        return ""
    m = _LIMIT_ERROR_RE.search(str(exc))
    if not m:
        return ""
    limit = m.group(1)
    return (
        f"\n\nHINT: жёсткий валидатор skills — значение длиннее {limit} символов. "
        f"Обрежь его явно перед вызовом (value[:{limit}]) и перезапусти блок; "
        f"авто-truncate намеренно не выполняется, чтобы обрезка была видна в коде."
    )


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


class _ToolCaller(Protocol):
    """Что _ToolNamespace требует от пула: один способ позвать инструмент."""

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict): ...


class _DirectSessionCaller:
    """Адаптер «пул из одной сессии»: тот же call_tool, но без реконнекта.

    Нужен там, где пула нет или он — тестовый дубль, стабящий только
    sessions/tools. Благодаря ему боевой путь вызова остаётся БЕЗУСЛОВНЫМ:
    ветка «пул или сессия» решается один раз при сборке namespace, а не на
    каждом вызове инструмента."""

    def __init__(self, session: ClientSession):
        self._session = session

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
        return await self._session.call_tool(tool_name, arguments)


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
        pool: "_ToolCaller | None" = None,
    ):
        self._server_name = server_name
        self._session = session
        self._tools = {t.name: t for t in tools}
        self._auto_limit = auto_limit
        self._sql_dialect = _dialect_for_server(server_name) if auto_limit > 0 else None
        self._stats = stats
        self._recorder = recorder
        self._raw_cap = raw_cap
        # Вызовы идут через пул, а не через захваченную при сборке сессию:
        # мёртвая/перезапущенная MCP-сессия так переподключается прозрачно
        # (MCPClientPool.call_tool = reconnect + один retry).
        # Пула нет или это дубль без call_tool → адаптер над сессией; выбор
        # делается ЗДЕСЬ, один раз, чтобы в горячем пути ветки не было.
        self._caller: _ToolCaller = (
            pool
            if callable(getattr(pool, "call_tool", None))
            else _DirectSessionCaller(session)
        )

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
        server = self._server_name
        raw_cap = self._raw_cap
        caller = self._caller

        async def wrapper(**kwargs):
            limit_applied = self._maybe_inject_limit(tool_name, kwargs)
            if limit_applied and self._stats is not None:
                self._stats["auto_limit_hits"] += 1
            start = time.monotonic()
            success = True
            error: str | None = None
            out_bytes = 0
            try:
                result = await caller.call_tool(server, tool_name, kwargs)
                texts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        texts.append(content.text)
                    elif hasattr(content, "data"):
                        texts.append(json.dumps(content.data, ensure_ascii=False))
                combined = "\n".join(texts)
                out_bytes = len(combined.encode("utf-8"))
                # A tool reporting an error (MCP isError=True) carries the
                # failure detail in `content`. Surface it as an exception so
                # user code can try/except and the model can't mistake an error
                # for data. getattr keeps older/mock results (no isError) working.
                if getattr(result, "isError", False):
                    detail = combined.strip() or "(no error detail returned)"
                    if len(detail) > MAX_ERROR_DETAIL:
                        detail = detail[:MAX_ERROR_DETAIL] + " …[truncated]"
                    raise MCPToolError(f"{server}.{tool_name} returned an error: {detail}")
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
                    self._stats["raw_tool_bytes"] = (
                        self._stats.get("raw_tool_bytes", 0) + out_bytes
                    )
                if self._recorder is not None:
                    with contextlib.suppress(Exception):
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
        isolation: str | None = None,
        mem_limit_mb: int | None = None,
        fsize_limit_mb: int | None = None,
    ):
        self.pool = pool
        self.recorder = recorder
        self.workspace = (
            workspace if workspace is not None else WorkspaceManager(DEFAULT_WORKSPACE_ROOT)
        )
        self.skills = skills
        # "subprocess" runs each call in a fresh, rlimited child; "inprocess" is
        # the legacy same-interpreter path. Ignored (forced inprocess) where the
        # `resource` module is unavailable.
        chosen = (isolation or _ISOLATION_DEFAULT)
        self._isolation = chosen if _SUBPROCESS_AVAILABLE else "inprocess"
        self._mem_limit_mb = mem_limit_mb if mem_limit_mb is not None else _MEM_LIMIT_MB_DEFAULT
        self._fsize_limit_mb = (
            fsize_limit_mb if fsize_limit_mb is not None else _FSIZE_LIMIT_MB_DEFAULT
        )
        # Signals are process-global and can only be armed from the main
        # thread — serialize executions so two concurrent calls can't clobber
        # each other's SIGALRM state.
        self._exec_lock = asyncio.Lock()
        self._sessions: dict[str, _SessionState] = {}
        # Per-call project pool (daemon mode). Выставляется только под
        # _exec_lock — исполнение сериализовано, гонки нет.
        self._extra_pool = None

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

    def _build_proxies(
        self,
        auto_limit: int,
        stats: dict[str, int] | None,
        raw_cap: int,
    ) -> dict[str, "_ToolNamespace"]:
        """One MCP proxy object per connected server. Shared by the in-process
        namespace and the subprocess broker (which dispatches remote calls to
        these same objects)."""
        proxies: dict[str, _ToolNamespace] = {}
        for server_name, session in self.pool.sessions.items():
            py_name = server_name_to_py(server_name)
            tools = self.pool.tools.get(server_name, [])
            proxies[py_name] = _ToolNamespace(
                server_name, session, tools,
                auto_limit=auto_limit,
                stats=stats,
                recorder=self.recorder,
                raw_cap=raw_cap,
                pool=self.pool,
            )
        # Project-level servers (daemon mode): only names the global pool
        # doesn't already serve — global wins on collision.
        extra = self._extra_pool
        if extra is not None:
            for server_name, session in extra.sessions.items():
                py_name = server_name_to_py(server_name)
                if py_name in proxies:
                    continue
                proxies[py_name] = _ToolNamespace(
                    server_name, session, extra.tools.get(server_name, []),
                    auto_limit=auto_limit,
                    stats=stats,
                    recorder=self.recorder,
                    raw_cap=raw_cap,
                    pool=extra,
                )
        return proxies

    def _build_namespace(
        self,
        session_id: str | None = None,
        auto_limit: int = 0,
        stats: dict[str, int] | None = None,
        raw_cap: int = 0,
    ) -> tuple[dict[str, Any], set[str]]:
        # Server-side use of getattr/hasattr to build whitelist — NOT exposed to user sandbox
        safe_builtins = {
            name: getattr(builtins, name) for name in SAFE_BUILTINS if hasattr(builtins, name)
        }
        namespace: dict[str, Any] = {
            "__builtins__": safe_builtins,
            "asyncio": _SAFE_ASYNCIO_WRAPPED,
            "json": _JSON_WRAPPED,
            **_SAFE_MODULES_WRAPPED,
        }

        # Workspace-bound open(): when session_id is set, writes go into
        # <workspace>/<session_id>/. When unset, the call raises so user code
        # can't accidentally touch the host FS.
        if session_id is not None:
            wm = self.workspace
            sid = session_id
            def _user_open(
                path, mode="r", *,
                max_bytes=None, encoding=None, errors=None, newline=None,
            ):
                kwargs: dict[str, Any] = {
                    "encoding": encoding, "errors": errors, "newline": newline,
                }
                if max_bytes is not None:
                    kwargs["max_bytes"] = max_bytes
                return safe_open(wm, sid, path, mode, **kwargs)
            namespace["open"] = _user_open
        else:
            def _denied(*_a, **_kw):
                raise WorkspaceError("open() requires session_id")
            namespace["open"] = _denied

        namespace.update(self._build_proxies(auto_limit, stats, raw_cap))

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

    async def _run_in_subprocess(
        self,
        code: str,
        session_id: str | None,
        timeout: float,
        auto_limit: int,
        raw_cap: int,
        stats: dict[str, int],
    ) -> tuple[bool, str, str | None]:
        """Run user code in a fresh rlimited child; proxy its MCP calls back here.

        The child holds no live sessions, so each tool call it makes is sent over
        the pipe, executed against the real proxy in *this* process (so auto-LIMIT,
        raw-cap, parsing, stats and metrics all still apply), and the parsed result
        returned. On timeout the child is SIGKILLed — the containment the
        in-process path can't offer.
        """
        from . import subprocess_worker  # lazy: the worker imports from this module

        proxies = self._build_proxies(auto_limit, stats, raw_cap)
        servers = [
            {"py_name": py_name, "tools": [n.replace("-", "_") for n in proxy._tools]}
            for py_name, proxy in proxies.items()
        ]

        # Restore this session's persisted vars (an opaque pickled blob produced
        # by a prior child). {} means "no prior state yet".
        persisted: bytes | None = None
        if session_id is not None:
            state = self._get_or_create_session(session_id)
            if isinstance(state.user_vars, (bytes, bytearray)):
                persisted = bytes(state.user_vars)

        payload = {
            "code": code,
            "session_id": session_id,
            "workspace_root": str(self.workspace.root) if self.workspace is not None else None,
            # Skill callables don't pickle, so the child reloads them from the
            # same directory this namespace came from — which also means a skill
            # added by save_skill is live on the next call, without a restart.
            "skills_dir": str(self.skills.root) if getattr(self.skills, "root", None) else None,
            "servers": servers,
            "user_vars": persisted,
            "limits": {
                "mem_bytes": self._mem_limit_mb * 1024 * 1024 if self._mem_limit_mb else 0,
                "cpu_s": int(timeout) + 2,
                "fsize_bytes": self._fsize_limit_mb * 1024 * 1024 if self._fsize_limit_mb else 0,
            },
        }

        ctx = multiprocessing.get_context(_MP_START_METHOD)
        parent_conn, child_conn = ctx.Pipe()
        proc = ctx.Process(
            target=subprocess_worker.run_worker,
            args=(child_conn, payload),
            daemon=True,
        )
        proc.start()
        child_conn.close()  # only the child uses its end
        loop = asyncio.get_running_loop()

        def _recv() -> Any:
            try:
                return parent_conn.recv()
            except (EOFError, OSError):
                return _EOF

        async def _serve() -> dict[str, Any]:
            while True:
                msg = await loop.run_in_executor(None, _recv)
                if msg is _EOF:
                    return {
                        "success": False, "output": "",
                        "error": "isolation worker died before returning a result",
                        "user_vars": None,
                    }
                tag, body = msg
                if tag == "call":
                    req_id, py_server, tool_attr, kwargs = body
                    try:
                        wrapper = getattr(proxies[py_server], tool_attr)
                        value = await wrapper(**kwargs)
                        parent_conn.send(("resp", (req_id, "ok", value)))
                    except BaseException as e:
                        parent_conn.send(("resp", (req_id, "err", (type(e).__name__, str(e)))))
                elif tag == "done":
                    return body

        try:
            result = await asyncio.wait_for(_serve(), timeout=timeout)
        except TimeoutError:
            result = {
                "success": False, "output": "",
                "error": f"Execution timed out after {timeout}s (isolation subprocess killed)",
                "user_vars": None,
            }
        finally:
            if proc.is_alive():
                proc.kill()
            await loop.run_in_executor(None, proc.join)
            parent_conn.close()

        if (
            session_id is not None
            and result.get("success")
            and result.get("user_vars") is not None
        ):
            st = self._sessions.get(session_id)
            if st is not None:
                st.user_vars = result["user_vars"]
                st.last_access = time.monotonic()

        error = result.get("error")
        if error:
            error += _str_result_hint(error, stats)
        return bool(result.get("success")), result.get("output") or "", error

    async def execute(
        self,
        code: str,
        timeout: float = 60.0,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        session_id: str | None = None,
        auto_limit: int = DEFAULT_AUTO_LIMIT,
        raw_cap: int = DEFAULT_RAW_CAP,
        extra_pool=None,
    ) -> dict[str, Any]:
        # Signals are process-global, so only one execution may arm SIGALRM
        # at a time. The lock is also cheap for the common single-client
        # MCP stdio case.
        async with self._exec_lock:
            self._extra_pool = extra_pool
            try:
                return await self._execute_locked(
                    code, timeout, max_output_bytes, session_id, auto_limit, raw_cap
                )
            finally:
                self._extra_pool = None

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
        # Fingerprint the code as the caller sent it, before _transform_last_expr
        # rewrites it — identical snippets must hash identically across calls.
        code_sha = code_fingerprint(code)
        code_lines = code.count("\n") + 1
        stats: dict[str, int] = {
            "tool_calls": 0, "auto_limit_hits": 0, "str_results": 0, "raw_tool_bytes": 0,
        }

        def finalize(success: bool, output: str, error: str | None) -> dict[str, Any]:
            error = _truncate_error(error, MAX_ERROR_BYTES)
            truncated = _truncate_output(output, max_output_bytes)
            truncated = _append_cost_footer(
                truncated, stats, elapsed_ms=(time.monotonic() - start_exec) * 1000
            )
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
                        "code_sha": code_sha,
                        "code_lines": code_lines,
                        "error": error,
                        "error_class": classify_error(error),
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

        # Subprocess isolation path: run in a fresh rlimited child. finalize()
        # (truncation, error cap, cost footer, metrics) and stats — populated by
        # the broker's proxy calls — are shared with the in-process path below.
        if self._isolation == "subprocess" and _SUBPROCESS_AVAILABLE:
            try:
                success, output, error = await self._run_in_subprocess(
                    code, session_id, timeout, auto_limit, raw_cap, stats
                )
            except Exception as e:
                return finalize(
                    False, "",
                    f"isolation error: {type(e).__name__}: {e}\n{traceback.format_exc()}",
                )
            return finalize(success, output, error)

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
        except TimeoutError:
            return finalize(
                False,
                "".join(output_lines),
                f"Execution timed out after {timeout}s",
            )
        except Exception as e:
            return finalize(
                False,
                "".join(output_lines),
                _format_user_traceback(e)
                + _str_result_hint(e, stats)
                + _signature_hint(e, self.skills)
                + _name_error_hint(e, namespace)
                + _limit_error_hint(e),
            )
        finally:
            if alarm_armed:
                _disarm_sandbox_alarm()

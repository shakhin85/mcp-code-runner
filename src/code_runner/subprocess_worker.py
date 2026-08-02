"""Runs LLM-generated code in a disposable, resource-limited child process.

The in-process executor runs user code in the server's own interpreter. A single
escape — or an OOM, or a CPU loop on a non-main thread where SIGALRM never fires —
takes the whole server down, and with it every live MCP session's credentials.
This module runs that same code in a *fresh child process per call* with
``setrlimit`` caps (address space, CPU time, file size) and a scrubbed
environment, reachable only over one pipe.

MCP tool calls can't run here — the live sessions live in the parent — so each
proxy call is marshalled back over the pipe, executed by the parent against the
real session, and the parsed result returned. Everything else (pure compute,
``open()`` into the session workspace, skills) stays inside the sandbox.

A fresh process per call is deliberate: ``RLIMIT_CPU`` is cumulative over a
process's whole life, so a reused worker would eventually trip it on honest work.
The default start method is ``spawn`` — never plain ``fork`` — so the child is a
brand-new interpreter that never inherits the parent's in-memory MCP sessions or
credentials (``forkserver`` is opt-in and reintroduces COW inheritance).
"""

from __future__ import annotations

import ast
import asyncio
import builtins as _builtins
import contextlib
import json as _json
import os
import pickle
import resource
from typing import Any

from .executor import (
    _JSON_WRAPPED,
    _RESULT_SENTINEL,
    _SAFE_ASYNCIO_WRAPPED,
    _SAFE_MODULES_WRAPPED,
    SAFE_BUILTINS,
    _format_user_traceback,
)
from .workspace import WorkspaceError, WorkspaceManager, safe_open

# Env vars the child keeps; everything else (API keys, tokens, connection
# strings) is dropped before any user code runs, so an escape reaches a clean
# environment. Kept: what the interpreter and stdlib genuinely need.
_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "TZ",
    "PYTHONHASHSEED",
})


def _scrub_env() -> None:
    for key in list(os.environ):
        if key not in _ENV_ALLOWLIST:
            del os.environ[key]


def _apply_rlimits(limits: dict[str, int]) -> None:
    """Best-effort OS resource caps on this process. Each is independent."""
    def _set(res: int, soft: int, hard: int | None = None) -> None:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(res, (soft, hard if hard is not None else soft))

    mem = limits.get("mem_bytes")
    if mem:
        # RLIMIT_AS caps *virtual* address space. A runaway `[0] * 10**9`
        # (≈8 GB) is refused with MemoryError long before the host OOMs.
        _set(resource.RLIMIT_AS, mem)
    cpu = limits.get("cpu_s")
    if cpu:
        # Soft SIGXCPU then hard SIGKILL — backstop for a CPU loop if the
        # parent's wall-clock kill somehow doesn't land.
        _set(resource.RLIMIT_CPU, cpu, cpu + 1)
    fsize = limits.get("fsize_bytes")
    if fsize:
        _set(resource.RLIMIT_FSIZE, fsize)
    # No core dumps — a segfault after a deep-recursion escape shouldn't spray
    # the disk with a multi-GB core.
    _set(resource.RLIMIT_CORE, 0)


class _RemoteToolError(RuntimeError):
    """Re-raised in the child for an MCP tool error that occurred in the parent.

    The parent runs the real proxy (which raises MCPToolError, ValueError, …);
    only the type name and message cross the pipe. We surface a RuntimeError so
    ``except RuntimeError`` / ``except Exception`` in user code still catches it,
    and set ``__name__`` so ``type(e).__name__`` reads the original class.
    """


def _make_remote_exc(type_name: str, msg: str) -> BaseException:
    exc = _RemoteToolError(msg)
    # So `type(e).__name__` in user code reports the parent-side class name.
    exc.__class__ = type(type_name, (_RemoteToolError,), {})
    return exc


class _RemoteProxy:
    """Stand-in for one MCP server: each tool becomes an async method that RPCs
    the real call back to the parent over the pipe."""

    def __init__(self, py_name: str, tool_attrs: list[str], rpc) -> None:
        self._py_name = py_name
        self._rpc = rpc
        for attr in tool_attrs:
            setattr(self, attr, self._make(attr))

    def _make(self, attr: str):
        async def call(**kwargs):
            return await self._rpc(self._py_name, attr, kwargs)
        call.__name__ = attr
        return call

    def __repr__(self) -> str:
        return f"<MCP:{self._py_name} (remote)>"


class _RpcBridge:
    """Serializes proxy calls onto the single pipe.

    A lock keeps exactly one request in flight, so the blocking ``recv`` can be
    offloaded to a thread without two threads reading the pipe at once. Concurrent
    proxy calls (``asyncio.gather``) therefore serialize rather than overlap —
    acceptable for a single-client, serialized executor.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._counter = 0

    async def __call__(self, py_server: str, tool_attr: str, kwargs: dict) -> Any:
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._counter += 1
            req_id = self._counter
            self._conn.send(("call", (req_id, py_server, tool_attr, kwargs)))
            _tag, body = await loop.run_in_executor(None, self._conn.recv)
        # tag is always "resp" here; the parent never interleaves other tags
        # while a call is outstanding.
        _rid, status, payload = body
        if status == "ok":
            return payload
        raise _make_remote_exc(payload[0], payload[1])


def _build_child_namespace(payload: dict, rpc: _RpcBridge) -> tuple[dict, set[str], list[str]]:
    safe_builtins = {
        name: getattr(_builtins, name)
        for name in SAFE_BUILTINS
        if hasattr(_builtins, name)
    }
    output_lines: list[str] = []

    def captured_print(*args, sep=" ", end="\n", **_kwargs):
        output_lines.append(sep.join(str(a) for a in args) + end)

    namespace: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "asyncio": _SAFE_ASYNCIO_WRAPPED,
        "json": _JSON_WRAPPED,
        **_SAFE_MODULES_WRAPPED,
        "print": captured_print,
    }

    session_id = payload.get("session_id")
    workspace_root = payload.get("workspace_root")
    if session_id is not None and workspace_root is not None:
        wm = WorkspaceManager(workspace_root)

        def _user_open(path, mode="r", *, max_bytes=None, encoding=None, errors=None, newline=None):
            kwargs: dict[str, Any] = {"encoding": encoding, "errors": errors, "newline": newline}
            if max_bytes is not None:
                kwargs["max_bytes"] = max_bytes
            return safe_open(wm, session_id, path, mode, **kwargs)
        namespace["open"] = _user_open
    else:
        def _denied(*_a, **_kw):
            raise WorkspaceError("open() requires session_id")
        namespace["open"] = _denied

    for spec in payload.get("servers", []):
        namespace[spec["py_name"]] = _RemoteProxy(spec["py_name"], spec["tools"], rpc)

    framework_names = set(namespace.keys())

    persisted_blob = payload.get("user_vars")
    if persisted_blob:
        # pickle.loads is safe here: the blob is this session's own vars, pickled
        # by _extract_picklable_vars in a prior child and round-tripped through
        # the trusted parent — not attacker-supplied data. Even a hand-crafted
        # __reduce__ would run inside this already-sandboxed, rlimited child, so
        # it grants nothing the user's own code in the same session lacks.
        with contextlib.suppress(Exception):
            namespace.update(pickle.loads(persisted_blob))

    return namespace, framework_names, output_lines


def _extract_picklable_vars(namespace: dict, framework_names: set[str]) -> bytes | None:
    """Pickle only the user vars that survive serialization; drop the rest.

    A fresh process per call means persistence must cross a pipe, so unlike the
    in-process path this can only carry picklable values. Live proxies, modules,
    and lambdas are silently not persisted — which is also more honest than
    carrying a stale proxy across calls.
    """
    keep: dict[str, Any] = {}
    for k, v in namespace.items():
        if k in framework_names or k == "print":
            continue
        if k.startswith("__") and k.endswith("__"):
            continue
        try:
            pickle.dumps(v)
        except Exception:
            continue
        keep[k] = v
    if not keep:
        return None
    try:
        return pickle.dumps(keep)
    except Exception:
        return None


async def _run(payload: dict, rpc: _RpcBridge) -> dict:
    code = payload["code"]
    namespace, framework_names, output_lines = _build_child_namespace(payload, rpc)

    try:
        compiled = compile(code, "<user>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    except SyntaxError as e:
        return {"success": False, "output": "", "error": f"SyntaxError: {e}", "user_vars": None}

    try:
        # eval() IS the sandbox: it runs the user's already AST-validated and
        # -transformed code with restricted __builtins__ and module wrappers, in
        # this isolated, rlimited, env-scrubbed child. Same mechanism as the
        # in-process path — the isolation here is the point, not a bypass of it.
        maybe_coro = eval(compiled, namespace)
        if asyncio.iscoroutine(maybe_coro):
            await maybe_coro
        output = "".join(output_lines)
        result = namespace.pop(_RESULT_SENTINEL, None)
        if result is not None:
            if isinstance(result, (dict, list)):
                output += _json.dumps(result, ensure_ascii=False, indent=2)
            else:
                output += str(result)
        return {
            "success": True,
            "output": output,
            "error": None,
            "user_vars": _extract_picklable_vars(namespace, framework_names),
        }
    except BaseException as e:
        return {
            "success": False,
            "output": "".join(output_lines),
            "error": _format_user_traceback(e),
            "user_vars": None,
        }


def run_worker(conn, payload: dict) -> None:
    """Child-process entrypoint. Must be a top-level function so ``forkserver``/
    ``spawn`` can import and call it."""
    _scrub_env()
    _apply_rlimits(payload.get("limits", {}))
    rpc = _RpcBridge(conn)
    try:
        result = asyncio.run(_run(payload, rpc))
    except BaseException as e:
        result = {
            "success": False,
            "output": "",
            "error": f"worker crashed: {type(e).__name__}: {e}",
            "user_vars": None,
        }
    with contextlib.suppress(BaseException):
        conn.send(("done", result))
    with contextlib.suppress(BaseException):
        conn.close()

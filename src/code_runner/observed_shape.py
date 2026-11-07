"""Observed result shape of `server.tool`: keys and types of the last successful call.

Values never reach the store: only type names and dict keys, depth <= 2, <= 300 chars.
The parent process records it (subprocess isolation proxies MCP calls back to the parent),
the file survives a daemon restart. search_tools shows it for tools without outputSchema.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SHAPE_MAX = 300
_DEPTH = 2
PROSE = "str (не JSON)"
_TYPE_NAMES = {str: "str", int: "int", float: "float", bool: "bool", type(None): "None"}


def shapes_path() -> Path:
    override = os.environ.get("CODE_RUNNER_SHAPES_PATH")
    return Path(override) if override else Path.home() / ".cache" / "code-runner" / "shapes.json"


def shape_of(value: Any, depth: int = _DEPTH) -> str:
    """Compact type description; dict keys are shown, values are not."""
    if isinstance(value, dict):
        if depth == 0 or not value:
            return "dict"
        fields = ", ".join(f"{k}: {shape_of(v, depth - 1)}" for k, v in value.items())
        return "{" + fields + "}"
    if isinstance(value, list | tuple):
        if depth == 0 or not value:
            return "list"
        return f"list[{shape_of(value[0], depth)}]"
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def describe(value: Any) -> str:
    text = PROSE if isinstance(value, str) else shape_of(value)
    return text if len(text) <= SHAPE_MAX else text[: SHAPE_MAX - 3] + "..."


def _load() -> dict[str, str]:
    try:
        data = json.loads(shapes_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def lookup(server_py: str, tool: str) -> str | None:
    return _load().get(f"{server_py}.{tool}")


def record(server_py: str, tool: str, value: Any) -> None:
    """Store the shape of a successful result; a failed write never breaks the call."""
    key, shape = f"{server_py}.{tool}", describe(value)
    with contextlib.suppress(OSError):
        shapes = _load()
        if shapes.get(key) == shape:
            return
        shapes[key] = shape
        path = shapes_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".shapes-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(shapes, f, ensure_ascii=False)
        os.replace(tmp, path)

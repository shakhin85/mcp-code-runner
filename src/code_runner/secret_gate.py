"""SHA-503: refuse a side-effecting call whose string args carry a secret.

The rules are not ours: the canonical redactor lives in the harness
(`~/.claude/tools/secret-redact/redact.py`, also used by skills.forgetful_create).
Loading it keeps one rule set; a copy here would drift. Refusing rather than
rewriting keeps the call's data exactly as the caller wrote it — a silently
edited Linear comment or memory is worse than an explicit error.
"""

from __future__ import annotations

import functools
import importlib.util
import os
from pathlib import Path
from typing import Any

REDACT_PATH_ENV = "CODE_RUNNER_REDACT_PATH"
DEFAULT_REDACT_PATH = "~/.claude/tools/secret-redact/redact.py"


@functools.cache
def _scrub_obj():
    path = Path(os.path.expanduser(os.environ.get(REDACT_PATH_ENV, DEFAULT_REDACT_PATH)))
    spec = importlib.util.spec_from_file_location("_code_runner_redact", path)
    if spec is None or spec.loader is None or not path.is_file():
        raise RuntimeError(
            f"secret-redact не загружается: {path} (задай {REDACT_PATH_ENV}); "
            "side-effect вызовы без проверки на секреты не отправляются"
        )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.scrub_obj


def check(server: str, tool: str, kwargs: dict[str, Any]) -> None:
    """Raise PermissionError naming the matched rules; values are not echoed."""
    _clean, hits = _scrub_obj()(kwargs)
    if hits:
        raise PermissionError(
            f"{server}.{tool}: в аргументах секрет ({', '.join(hits)}) — "
            "убери его из аргументов и повтори вызов"
        )

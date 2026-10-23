"""`restore()` — the sandbox side of reversible output truncation (SHA-482).

A truncated `execute_code` result names its block id; the full output sits in
the session workspace as `out_<id>.txt`. `restore(id, ...)` returns a slice or
a grep of it, so the next block takes the part it needs without re-running the
downstream calls.

Built on top of the sandbox `open()`, so it inherits the workspace confinement
and the "session_id required" refusal on both isolation paths.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


def saved_output_name(block: int) -> str:
    return f"out_{block}.txt"


def make_restore(user_open: Callable[..., Any]) -> Callable[..., str]:
    def restore(
        block: int,
        start: int = 0,
        end: int | None = None,
        *,
        grep: str | None = None,
        context: int = 0,
    ) -> str:
        """Lines `start:end` of block `block`'s full output, or the lines matching
        regex `grep` (with `context` lines around each, numbered from 1)."""
        try:
            with user_open(saved_output_name(block)) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            raise FileNotFoundError(
                f"no saved output for block {block} in this session_id — only "
                f"truncated outputs are saved"
            ) from None
        if grep is None:
            return "\n".join(lines[start:end])
        rx = re.compile(grep)
        keep: set[int] = set()
        for i, line in enumerate(lines):
            if rx.search(line):
                keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
        return "\n".join(f"{i + 1}: {lines[i]}" for i in sorted(keep))

    return restore

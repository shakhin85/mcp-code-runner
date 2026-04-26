"""
Per-session workspace under ~/.cache/code-runner/workspace/<session_id>/.

Lazy-created; cleaned up when the session is evicted. Path resolution
rejects absolute paths, parent traversal, and symlink escapes so user
code cannot reach outside its own session dir.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path


class WorkspaceError(ValueError):
    """Raised on unsafe paths, bad session ids, or write-cap violations."""


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _session_dir(self, session_id: str) -> Path:
        if not _SESSION_ID_RE.match(session_id or ""):
            raise WorkspaceError(
                f"invalid session_id: must match {_SESSION_ID_RE.pattern}"
            )
        return self.root / session_id

    def resolve_path(self, session_id: str, rel_path: str) -> Path:
        if not rel_path:
            raise WorkspaceError("empty path")
        rel = Path(rel_path)
        if rel.is_absolute():
            raise WorkspaceError(f"absolute paths not allowed: {rel_path}")
        if any(part == ".." for part in rel.parts):
            raise WorkspaceError(f"path traversal not allowed: {rel_path}")

        sess_dir = self._session_dir(session_id)
        sess_dir.mkdir(parents=True, exist_ok=True)

        target = (sess_dir / rel).resolve()
        sess_resolved = sess_dir.resolve()
        try:
            target.relative_to(sess_resolved)
        except ValueError as e:
            raise WorkspaceError(
                f"path resolves outside session (symlink?): {rel_path}"
            ) from e
        return target

    def cleanup_session(self, session_id: str) -> None:
        try:
            sess_dir = self._session_dir(session_id)
        except WorkspaceError:
            return
        if sess_dir.exists():
            shutil.rmtree(sess_dir, ignore_errors=True)


DEFAULT_WRITE_CAP = 50 * 1024 * 1024  # 50 MB per file
_ALLOWED_MODES = frozenset({"r", "rb", "w", "wb", "a", "ab"})
_DENIED_FILE_ATTRS = frozenset({
    "fileno", "detach", "buffer", "raw",
})


class _CappedFile:
    """File proxy that enforces a cumulative bytes-written cap.

    The cap counts bytes passed to write/writelines, not the resulting
    file size — a seek+overwrite pattern still consumes the cap. Methods
    that would expose the underlying fd or buffered stream (fileno,
    detach, buffer, raw) are blocked so user code cannot escape via
    os.write or stream-stealing.
    """

    def __init__(self, fp, max_bytes: int, start_bytes: int = 0) -> None:
        self._fp = fp
        self._max = max_bytes
        self._written = start_bytes

    def write(self, data):
        size = len(data)
        if self._written + size > self._max:
            self._fp.close()
            raise WorkspaceError(
                f"write cap exceeded: {self._written + size} > {self._max} bytes"
            )
        self._written += size
        return self._fp.write(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        if name in _DENIED_FILE_ATTRS:
            raise WorkspaceError(
                f"{name!r} is not exposed on capped files (would bypass write cap)"
            )
        return getattr(self._fp, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._fp.__exit__(*exc)


def safe_open(
    wm: "WorkspaceManager",
    session_id: str,
    path: str,
    mode: str = "r",
    *,
    max_bytes: int = DEFAULT_WRITE_CAP,
):
    """Open a file under <wm.root>/<session_id>/, with mode whitelist + write cap.

    Modes 'w', 'wb', 'a', 'ab' return a _CappedFile that enforces max_bytes
    across the lifetime of the handle. Read modes return a plain file object.
    Parent directories are created on demand for write/append modes.
    """
    if mode not in _ALLOWED_MODES:
        raise WorkspaceError(
            f"mode {mode!r} not allowed; use one of {sorted(_ALLOWED_MODES)}"
        )
    target = wm.resolve_path(session_id, path)

    is_write = any(c in mode for c in "wa")
    if is_write:
        target.parent.mkdir(parents=True, exist_ok=True)
        start = target.stat().st_size if (target.exists() and "a" in mode) else 0
        fp = open(target, mode)
        return _CappedFile(fp, max_bytes=max_bytes, start_bytes=start)

    return open(target, mode)

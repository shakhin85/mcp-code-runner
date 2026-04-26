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

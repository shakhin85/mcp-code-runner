"""
Per-session workspace under ~/.cache/code-runner/workspace/<session_id>/.

Writes are confined to the session dir: relative paths only, no parent
traversal, no symlink escapes. Reads may additionally reach absolute
paths under the configured read-only roots (CODE_RUNNER_READ_ROOTS,
default ~/projects), minus a secret deny-list — so code can open a
project file directly instead of copying it into the workspace first.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
from pathlib import Path


class WorkspaceError(ValueError):
    """Raised on unsafe paths, bad session ids, or write-cap violations."""


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

DEFAULT_READ_ROOTS = (Path.home() / "projects",)

# Files that must stay unreadable even inside an allowed read root: an
# allowlisted directory tree is not a licence to exfiltrate credentials.
SECRET_PATTERNS = (
    ".env", ".env.*", "*.env",
    ".mcp.json", ".claude.json", ".netrc", ".pgpass", ".htpasswd",
    "id_rsa*", "id_ed25519*", "*.pem", "*.key", "*.p12", "*.pfx",
    "credentials", "credentials.*", "*secret*", "*token*",
)


def read_roots_from_env() -> tuple[Path, ...]:
    """Read-only roots from CODE_RUNNER_READ_ROOTS (colon-separated), or default."""
    raw = os.environ.get("CODE_RUNNER_READ_ROOTS", "").strip()
    if not raw:
        return DEFAULT_READ_ROOTS
    roots = [Path(p).expanduser() for p in raw.split(":") if p.strip()]
    return tuple(roots) or DEFAULT_READ_ROOTS


class WorkspaceManager:
    def __init__(self, root: Path, read_roots: tuple[Path, ...] | None = None) -> None:
        self.root = Path(root)
        self.read_roots = tuple(
            read_roots if read_roots is not None else read_roots_from_env()
        )

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

    def resolve_read_path(self, abs_path: str) -> Path:
        """Resolve an absolute path for READING under the allowed read roots.

        Writes never come here — they stay confined to the session dir. A path
        is accepted only if it resolves (symlinks followed) inside one of
        ``self.read_roots`` and its filename is not on the secret deny-list.
        """
        target = Path(abs_path).expanduser().resolve()

        name = target.name
        if any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS):
            raise WorkspaceError(
                f"refusing to read a secret-looking file: {name}"
            )

        for root in self.read_roots:
            try:
                target.relative_to(root.expanduser().resolve())
            except ValueError:
                continue
            if not target.exists():
                raise WorkspaceError(f"file not found: {target}")
            return target

        allowed = ", ".join(str(r) for r in self.read_roots) or "(none)"
        raise WorkspaceError(
            f"absolute path outside the read-only roots: {abs_path}\n"
            f"readable roots: {allowed} (set CODE_RUNNER_READ_ROOTS to change).\n"
            f"Writes are always relative to the session workspace — use open('out.csv', 'w')."
        )

    def cleanup_session(self, session_id: str) -> None:
        try:
            sess_dir = self._session_dir(session_id)
        except WorkspaceError:
            return
        if sess_dir.exists():
            shutil.rmtree(sess_dir, ignore_errors=True)


DEFAULT_WRITE_CAP = 50 * 1024 * 1024  # 50 MB per file handle (resets on reopen)
_ALLOWED_MODES = frozenset({"r", "rb", "w", "wb", "a", "ab"})
_DENIED_FILE_ATTRS = frozenset({
    "fileno", "detach", "buffer", "raw",
})


class _CappedFile:
    """File proxy that enforces a per-handle bytes-written cap.

    The cap counts bytes passed to write/writelines on this handle, not
    the resulting file size — a seek+overwrite still consumes the cap.
    The cap is per-open: reopening the same file resets the counter, so
    this is a runaway-write guard, not a session-wide disk quota. Methods
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
    wm: WorkspaceManager,
    session_id: str,
    path: str,
    mode: str = "r",
    *,
    max_bytes: int = DEFAULT_WRITE_CAP,
    encoding: str | None = None,
    errors: str | None = None,
    newline: str | None = None,
):
    """Open a file: writes land in the session workspace, reads may also come
    from the read-only roots.

    Relative path → resolved under <wm.root>/<session_id>/ (any allowed mode).
    Absolute path → READ modes only, resolved under ``wm.read_roots``.

    Text modes default to UTF-8 regardless of the host locale: the data here is
    routinely Russian, and locale-dependent decoding turns that into mojibake or
    a UnicodeDecodeError.

    Modes 'w', 'wb', 'a', 'ab' return a _CappedFile enforcing `max_bytes` for the
    lifetime of THIS handle. Reopening resets the counter — a runaway-write guard,
    not a session-wide quota.
    """
    if mode not in _ALLOWED_MODES:
        raise WorkspaceError(
            f"mode {mode!r} not allowed; use one of {sorted(_ALLOWED_MODES)}"
        )

    is_write = any(c in mode for c in "wa")
    is_binary = "b" in mode

    if is_binary and (encoding is not None or errors is not None or newline is not None):
        raise WorkspaceError(
            f"encoding/errors/newline are text-mode options; mode {mode!r} is binary"
        )

    def _open(target: Path):
        if is_binary:
            return open(target, mode)
        return open(
            target, mode,
            encoding=encoding or "utf-8",
            errors=errors or "strict",
            newline=newline,
        )

    if Path(path).is_absolute():
        if is_write:
            raise WorkspaceError(
                f"writing to an absolute path is not allowed: {path}\n"
                "Absolute paths are readable (under the read-only roots) but never writable — "
                "write relative, e.g. open('out.csv', 'w'), then copy it out with Bash."
            )
        return _open(wm.resolve_read_path(path))

    target = wm.resolve_path(session_id, path)

    if is_write:
        target.parent.mkdir(parents=True, exist_ok=True)
        start = target.stat().st_size if (target.exists() and "a" in mode) else 0
        return _CappedFile(_open(target), max_bytes=max_bytes, start_bytes=start)

    return _open(target)

# Workspace + Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-session filesystem workspace + reusable skills library to code-runner, closing the two gaps vs Anthropic's "Code Execution with MCP" article.

**Architecture:**
- **Workspace:** lazy `~/.cache/code-runner/workspace/<session_id>/` directory. A whitelisted `open()` is injected into the sandbox namespace, confined to the session's workspace, with per-write byte cap. Cleanup on session eviction.
- **Skills:** Python files under `~/.claude/code-runner-skills/<name>/{script.py, SKILL.md}` discovered at startup. Each is `exec`'d with full builtins (trusted user code) into an isolated module-like dict; public callables exposed as `skills.<name>.<fn>` in the sandbox. New `save_skill` MCP tool writes a skill from inside `execute_code` and triggers a hot reload.
- **Opt-in, default-safe:** workspace activates only when `session_id` is set; skills load only if the directory exists; no behavior change for existing callers.

**Tech Stack:** Python 3.11+, MCP (FastMCP), pytest, stdlib-only (no new third-party deps).

---

## File Structure

**Create:**
- `src/code_runner/workspace.py` — `WorkspaceManager`, `safe_open`, write-capped file wrapper
- `src/code_runner/skills.py` — `SkillLoader`, `SkillsNamespace`, `SkillProxy`
- `tests/test_workspace.py`
- `tests/test_skills.py`
- `tests/skills_fixtures/` — sample skills used by tests
- `CLAUDE.md` — project doc (currently absent)
- `skills_templates/csv_export/{script.py,SKILL.md}` — starter skill
- `skills_templates/snapshot_diff/{script.py,SKILL.md}` — starter skill
- `skills_templates/schema_dump/{script.py,SKILL.md}` — starter skill

**Modify:**
- `src/code_runner/executor.py` — wire `open` and `skills` into namespace, cleanup workspace on eviction
- `src/code_runner/server.py` — register `save_skill` MCP tool, add skills section to `list_available_tools`, instantiate `SkillLoader` in lifespan
- `tests/test_executor.py` — additions for workspace + skills integration

---

### Task 0: Create project CLAUDE.md

**Goal:** Document architecture and dev workflow so future sessions don't re-discover everything.

**Files:**
- Create: `CLAUDE.md`

**Acceptance Criteria:**
- [ ] File exists at repo root
- [ ] Lists module responsibilities (executor, server, client_pool, schema_gen, sql_limit, metrics, config_reader)
- [ ] States dev commands (`uv run pytest`, `uv run code-runner`)
- [ ] Notes the two opt-in features being added (workspace, skills)
- [ ] Notes `.mcp.json` is gitignored due to embedded API keys

**Verify:** `test -f CLAUDE.md && grep -q workspace CLAUDE.md && grep -q skills CLAUDE.md`

**Steps:**

- [ ] **Step 1: Write CLAUDE.md**

```markdown
# code-runner

MCP server that exposes a single Python `execute_code` tool. User code runs at module scope in a restricted namespace where every other connected MCP server is available as a Python proxy object (e.g. `await postgres_lime.execute_sql(sql=...)`). Server-side: AST validation, SAFE_BUILTINS, SIGALRM timeout, output truncation, auto-LIMIT for bare SELECTs, JSONL metrics.

## Modules

- `server.py` — FastMCP entry point. Tools: `list_available_tools`, `search_tools`, `execute_code`, `get_metrics`, `save_skill` (planned).
- `executor.py` — `CodeExecutor`: validates code, builds namespace, runs with top-level-await, persists user vars per `session_id`.
- `client_pool.py` — `MCPClientPool`: long-lived stdio/HTTP MCP client connections.
- `config_reader.py` — reads `~/.claude.json`, project `.claude/settings.json`, project `.mcp.json`. Detects project dir via `CLAUDE_PROJECT_DIR` or `/proc` walk.
- `schema_gen.py` — JSON-schema → Python stub strings for `search_tools`.
- `sql_limit.py` — sqlglot-based auto-LIMIT/TOP injection for bare SELECTs.
- `metrics.py` — JSONL recorder for `tool_call` and `execute_code` events.
- `workspace.py` (planned) — per-session filesystem at `~/.cache/code-runner/workspace/<session_id>/`, exposed via injected `open()`.
- `skills.py` (planned) — discovers `~/.claude/code-runner-skills/<name>/`, exposes `skills.<name>.<fn>` proxy.

## Dev

- `uv run pytest` — full test suite (135+ tests baseline)
- `uv run code-runner` — start server (stdio transport)
- `CODE_RUNNER_METRICS=0` disables the JSONL metrics recorder (default: enabled, writes to `~/.cache/code-runner/metrics.jsonl`)

## Security

- `.mcp.json` is gitignored — local file holds project-only MCP server config including API keys
- Sandbox forbids `import`, dunder access, file I/O outside workspace, subprocess
- Skills run with full builtins (trusted local code), user code in `execute_code` does not

## Opt-in features

- **Workspace** — set `session_id` to enable `open()` inside sandbox; files live in `~/.cache/code-runner/workspace/<session_id>/`. Cleared on session eviction (TTL or LRU).
- **Skills** — drop a directory into `~/.claude/code-runner-skills/<name>/` containing `script.py` + `SKILL.md`; functions become `skills.<name>.<fn>` in the sandbox. Use `save_skill` MCP tool to create one from inside `execute_code`.
```

- [ ] **Step 2: Verify**

Run: `test -f CLAUDE.md && grep -E '(workspace|skills)' CLAUDE.md | wc -l`
Expected: ≥ 4 matches.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md .gitignore
git commit -m "docs(claude): add CLAUDE.md with module map and dev workflow

- describe each module's responsibility
- document opt-in workspace and skills features (planned)
- ignore .mcp.json due to embedded firecrawl API key"
```

---

### Task 1: WorkspaceManager — lazy session dirs + path safety

**Goal:** Create the foundation that owns per-session directories and rejects path traversal.

**Files:**
- Create: `src/code_runner/workspace.py`
- Create: `tests/test_workspace.py`

**Acceptance Criteria:**
- [ ] `WorkspaceManager(root: Path)` lazily creates `<root>/<session_id>/` on first request
- [ ] `resolve_path(session_id, "out.csv")` returns absolute path inside session dir
- [ ] Rejects: absolute paths, `..` segments, symlinks pointing outside, empty strings
- [ ] `cleanup_session(session_id)` deletes the dir (idempotent if missing)
- [ ] All tests pass

**Verify:** `uv run pytest tests/test_workspace.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workspace.py
import os
from pathlib import Path

import pytest

from code_runner.workspace import WorkspaceManager, WorkspaceError


@pytest.fixture
def wm(tmp_path):
    return WorkspaceManager(root=tmp_path)


def test_session_dir_created_lazily(wm, tmp_path):
    p = wm.resolve_path("sess1", "out.csv")
    assert p.parent == tmp_path / "sess1"
    assert (tmp_path / "sess1").is_dir()


def test_session_dir_not_created_until_used(wm, tmp_path):
    assert not (tmp_path / "sess1").exists()


def test_rejects_absolute_path(wm):
    with pytest.raises(WorkspaceError, match="absolute"):
        wm.resolve_path("sess1", "/etc/passwd")


def test_rejects_parent_traversal(wm):
    with pytest.raises(WorkspaceError, match="traversal"):
        wm.resolve_path("sess1", "../../etc/passwd")


def test_rejects_empty_path(wm):
    with pytest.raises(WorkspaceError):
        wm.resolve_path("sess1", "")


def test_rejects_symlink_escape(wm, tmp_path):
    wm.resolve_path("sess1", "ok.txt")  # create dir
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = tmp_path / "sess1" / "evil"
    os.symlink(outside, link)
    with pytest.raises(WorkspaceError, match="symlink|outside"):
        wm.resolve_path("sess1", "evil")


def test_nested_subdir_allowed(wm, tmp_path):
    p = wm.resolve_path("sess1", "sub/inner/out.csv")
    assert p == tmp_path / "sess1" / "sub" / "inner" / "out.csv"
    # parents are created on demand by safe_open, not resolve_path
    # resolve_path only validates and returns the path


def test_cleanup_session_removes_dir(wm, tmp_path):
    wm.resolve_path("sess1", "f.txt")
    assert (tmp_path / "sess1").is_dir()
    wm.cleanup_session("sess1")
    assert not (tmp_path / "sess1").exists()


def test_cleanup_session_idempotent(wm):
    wm.cleanup_session("never-existed")  # no error


def test_session_id_must_be_safe(wm):
    with pytest.raises(WorkspaceError, match="session_id"):
        wm.resolve_path("../escape", "f.txt")
    with pytest.raises(WorkspaceError, match="session_id"):
        wm.resolve_path("a/b", "f.txt")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_workspace.py -v`
Expected: All fail with `ModuleNotFoundError: code_runner.workspace`

- [ ] **Step 3: Implement `workspace.py`**

```python
# src/code_runner/workspace.py
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


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_workspace.py -v`
Expected: All 9 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/code_runner/workspace.py tests/test_workspace.py
git commit -m "feat(workspace): WorkspaceManager with lazy session dirs and path safety

resolve_path rejects absolute paths, parent traversal, and symlink
escapes. cleanup_session removes the dir on eviction. Foundation for
the safe_open builtin coming next."
```

---

### Task 2: safe_open + per-write byte cap

**Goal:** A whitelisted `open` that user code can call inside the sandbox, capped to prevent runaway writes.

**Files:**
- Modify: `src/code_runner/workspace.py`
- Modify: `tests/test_workspace.py`

**Acceptance Criteria:**
- [ ] `safe_open(wm, session_id, path, mode)` supports modes: `r`, `rb`, `w`, `wb`, `a`, `ab`
- [ ] All other modes raise `WorkspaceError`
- [ ] Write modes wrap the file object with a cap (default 50MB); writes that would exceed the cap raise `WorkspaceError`
- [ ] Read modes return a normal file object
- [ ] `safe_open` creates parent subdirectories on demand for write modes
- [ ] All tests pass

**Verify:** `uv run pytest tests/test_workspace.py -v` → all pass

**Steps:**

- [ ] **Step 1: Add the failing tests**

```python
# append to tests/test_workspace.py
from code_runner.workspace import safe_open, DEFAULT_WRITE_CAP


def test_safe_open_write_text(wm):
    with safe_open(wm, "sess1", "out.txt", "w") as f:
        f.write("hello")
    p = wm.resolve_path("sess1", "out.txt")
    assert p.read_text() == "hello"


def test_safe_open_read_after_write(wm):
    with safe_open(wm, "sess1", "out.txt", "w") as f:
        f.write("hi")
    with safe_open(wm, "sess1", "out.txt", "r") as f:
        assert f.read() == "hi"


def test_safe_open_binary(wm):
    with safe_open(wm, "sess1", "blob.bin", "wb") as f:
        f.write(b"\x00\x01\x02")
    with safe_open(wm, "sess1", "blob.bin", "rb") as f:
        assert f.read() == b"\x00\x01\x02"


def test_safe_open_append(wm):
    with safe_open(wm, "sess1", "log.txt", "w") as f:
        f.write("a")
    with safe_open(wm, "sess1", "log.txt", "a") as f:
        f.write("b")
    p = wm.resolve_path("sess1", "log.txt")
    assert p.read_text() == "ab"


def test_safe_open_rejects_other_modes(wm):
    for mode in ("x", "r+", "w+", "rt+"):
        with pytest.raises(WorkspaceError, match="mode"):
            safe_open(wm, "sess1", "f.txt", mode)


def test_safe_open_creates_parent_dirs(wm):
    with safe_open(wm, "sess1", "deep/nested/f.txt", "w") as f:
        f.write("x")
    assert wm.resolve_path("sess1", "deep/nested/f.txt").read_text() == "x"


def test_safe_open_write_cap_enforced(wm):
    with pytest.raises(WorkspaceError, match="cap"):
        with safe_open(wm, "sess1", "big.bin", "wb", max_bytes=10) as f:
            f.write(b"x" * 11)


def test_safe_open_write_cap_across_multiple_writes(wm):
    with safe_open(wm, "sess1", "big.bin", "wb", max_bytes=10) as f:
        f.write(b"x" * 5)
        f.write(b"y" * 5)  # exactly at cap, fine
        with pytest.raises(WorkspaceError, match="cap"):
            f.write(b"z")


def test_safe_open_default_cap_is_50mb(wm):
    assert DEFAULT_WRITE_CAP == 50 * 1024 * 1024


def test_safe_open_traversal_rejected(wm):
    with pytest.raises(WorkspaceError, match="traversal"):
        safe_open(wm, "sess1", "../escape.txt", "w")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_workspace.py -v -k "safe_open"`
Expected: All fail with `ImportError: cannot import name 'safe_open'`.

- [ ] **Step 3: Extend `workspace.py`**

Add to the file:

```python
DEFAULT_WRITE_CAP = 50 * 1024 * 1024  # 50 MB per file
_ALLOWED_MODES = frozenset({"r", "rb", "w", "wb", "a", "ab"})


class _CappedFile:
    """File proxy that raises WorkspaceError if cumulative writes exceed max_bytes."""

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

    def __getattr__(self, name):
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
    if mode not in _ALLOWED_MODES:
        raise WorkspaceError(
            f"mode {mode!r} not allowed; use one of {sorted(_ALLOWED_MODES)}"
        )
    target = wm.resolve_path(session_id, path)

    if any(c in mode for c in "wa"):
        target.parent.mkdir(parents=True, exist_ok=True)
        # For 'a' mode, count existing bytes against the cap.
        start = target.stat().st_size if (target.exists() and "a" in mode) else 0
        fp = open(target, mode)
        return _CappedFile(fp, max_bytes=max_bytes, start_bytes=start)

    return open(target, mode)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_workspace.py -v`
Expected: All workspace tests pass (~19 tests total).

- [ ] **Step 5: Commit**

```bash
git add src/code_runner/workspace.py tests/test_workspace.py
git commit -m "feat(workspace): safe_open with mode whitelist and write cap

Modes restricted to r/rb/w/wb/a/ab. Writes wrapped with a 50MB cap
that raises WorkspaceError on overflow; append mode counts existing
bytes. Parent directories are created on demand."
```

---

### Task 3: Wire workspace into executor

**Goal:** When `session_id` is set, `open()` is available inside the sandbox bound to that session; cleanup runs on eviction.

**Files:**
- Modify: `src/code_runner/executor.py`
- Modify: `tests/test_executor.py`

**Acceptance Criteria:**
- [ ] `CodeExecutor` accepts an optional `WorkspaceManager` (default constructed pointing at `~/.cache/code-runner/workspace/`)
- [ ] When `session_id` is set, sandbox namespace contains `open` bound to that session
- [ ] When `session_id` is `None`, calling `open(...)` raises a clear error (`open` not in namespace, or stub that explains)
- [ ] Files written in one call are readable from another call sharing `session_id`
- [ ] Eviction (TTL or LRU) calls `wm.cleanup_session(sid)`
- [ ] Existing executor tests still pass

**Verify:** `uv run pytest tests/test_executor.py tests/test_workspace.py -v`

**Steps:**

- [ ] **Step 1: Add failing tests**

```python
# append to tests/test_executor.py — find existing imports / fixtures and reuse
import asyncio
import pytest

from code_runner.executor import CodeExecutor
from code_runner.workspace import WorkspaceManager


# Reuse whatever fake-pool fixture the file already has.
# Below assumes a `make_executor(pool)` helper or similar exists; if not,
# see how other tests construct CodeExecutor and follow the same pattern.

@pytest.mark.anyio
async def test_open_writes_into_session_workspace(tmp_path, fake_pool):
    wm = WorkspaceManager(root=tmp_path)
    ex = CodeExecutor(fake_pool, workspace=wm)

    code = """
with open("greeting.txt", "w") as f:
    f.write("hi")
print("done")
"""
    result = await ex.execute(code, session_id="s1")
    assert result["success"], result["error"]
    assert (tmp_path / "s1" / "greeting.txt").read_text() == "hi"


@pytest.mark.anyio
async def test_open_persists_across_calls_in_same_session(tmp_path, fake_pool):
    wm = WorkspaceManager(root=tmp_path)
    ex = CodeExecutor(fake_pool, workspace=wm)
    await ex.execute('open("x.txt","w").write("abc")', session_id="s2")
    result = await ex.execute(
        'print(open("x.txt","r").read())', session_id="s2"
    )
    assert "abc" in result["output"]


@pytest.mark.anyio
async def test_open_unavailable_without_session(tmp_path, fake_pool):
    wm = WorkspaceManager(root=tmp_path)
    ex = CodeExecutor(fake_pool, workspace=wm)
    result = await ex.execute('open("x.txt","w")', session_id=None)
    assert not result["success"]
    assert "session_id" in (result["error"] or "").lower() or "open" in (result["error"] or "").lower()


@pytest.mark.anyio
async def test_eviction_cleans_workspace(tmp_path, fake_pool):
    wm = WorkspaceManager(root=tmp_path)
    ex = CodeExecutor(fake_pool, workspace=wm)
    await ex.execute('open("a.txt","w").write("1")', session_id="evictme")
    sess_dir = tmp_path / "evictme"
    assert sess_dir.exists()
    # Force eviction: directly call internal helper after expiring TTL
    ex._sessions["evictme"].last_access = 0.0
    ex._evict_expired_sessions()
    assert not sess_dir.exists()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_executor.py -v -k "workspace or open or evict"`
Expected: Fail because `CodeExecutor.__init__` does not accept `workspace`.

- [ ] **Step 3: Modify `executor.py`**

Changes:

1. Import `WorkspaceManager` and `safe_open`:

```python
from .workspace import WorkspaceManager, safe_open, WorkspaceError
```

2. Default workspace root constant:

```python
DEFAULT_WORKSPACE_ROOT = Path.home() / ".cache" / "code-runner" / "workspace"
```

(Add `from pathlib import Path` if missing.)

3. Update `CodeExecutor.__init__`:

```python
def __init__(
    self,
    pool,
    recorder: "MetricsRecorder | None" = None,
    workspace: "WorkspaceManager | None" = None,
):
    self.pool = pool
    self.recorder = recorder
    self.workspace = workspace or WorkspaceManager(DEFAULT_WORKSPACE_ROOT)
    self._exec_lock = asyncio.Lock()
    self._sessions: dict[str, _SessionState] = {}
```

4. Update `_build_namespace` signature and body to inject `open`:

```python
def _build_namespace(
    self,
    session_id: str | None = None,
    auto_limit: int = 0,
    stats: dict[str, int] | None = None,
) -> tuple[dict[str, Any], set[str]]:
    safe_builtins = {name: getattr(builtins, name) for name in SAFE_BUILTINS if hasattr(builtins, name)}
    namespace: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "asyncio": _SAFE_ASYNCIO,
        "json": json,
        **SAFE_MODULES,
    }

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
            auto_limit=auto_limit, stats=stats, recorder=self.recorder,
        )

    framework_names = set(namespace.keys())

    if session_id is not None:
        state = self._get_or_create_session(session_id)
        namespace.update(state.user_vars)

    return namespace, framework_names
```

5. Update both eviction methods to clean up workspace:

```python
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
```

6. Add `open` to the SAFE_BUILTINS list? **No** — leave SAFE_BUILTINS alone; we inject our `open` at the namespace top level so it shadows any builtin reference. Confirm by reading the namespace flow.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ -v`
Expected: All 135 baseline + 4 new workspace integration tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/code_runner/executor.py tests/test_executor.py
git commit -m "feat(executor): inject safe_open as workspace-bound open()

When session_id is set, open() inside the sandbox writes to
~/.cache/code-runner/workspace/<session_id>/. Without session_id,
open() raises WorkspaceError. Workspace is wiped on TTL/LRU
eviction so per-session state stays isolated."
```

---

### Task 4: SkillLoader — discover skills on disk

**Goal:** Read `~/.claude/code-runner-skills/<name>/{script.py, SKILL.md}` into an in-memory catalog.

**Files:**
- Create: `src/code_runner/skills.py`
- Create: `tests/test_skills.py`
- Create: `tests/skills_fixtures/sample_csv/script.py`
- Create: `tests/skills_fixtures/sample_csv/SKILL.md`
- Create: `tests/skills_fixtures/no_md/script.py` (skill missing SKILL.md, should be skipped)

**Acceptance Criteria:**
- [ ] `SkillLoader(skills_dir).discover()` returns `dict[str, SkillSpec]`
- [ ] `SkillSpec(name, source, description, path)` populated from filesystem
- [ ] Skipped: dirs without `script.py`, dirs without `SKILL.md`, hidden dirs starting with `.`
- [ ] `SKILL.md` description parsed from frontmatter `description:` line OR first non-empty line if no frontmatter
- [ ] Returns empty dict if `skills_dir` does not exist
- [ ] Tests pass

**Verify:** `uv run pytest tests/test_skills.py -v`

**Steps:**

- [ ] **Step 1: Create fixture skills**

`tests/skills_fixtures/sample_csv/script.py`:

```python
import csv

def write_csv(rows, path):
    """Write a list of dicts to a CSV file in the workspace."""
    if not rows:
        return 0
    keys = list(rows[0].keys())
    with open(path, "w") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
```

`tests/skills_fixtures/sample_csv/SKILL.md`:

```markdown
---
name: sample_csv
description: Write list-of-dicts to CSV in the session workspace.
---

# sample_csv

Use `await skills.sample_csv.write_csv(rows, "out.csv")` to dump rows.
```

`tests/skills_fixtures/no_md/script.py`:

```python
def f(): return 1
```

(Note: `no_md` dir intentionally has no SKILL.md — used to verify skipping.)

- [ ] **Step 2: Write failing tests**

```python
# tests/test_skills.py
from pathlib import Path

import pytest

from code_runner.skills import SkillLoader, SkillSpec


FIXTURE_DIR = Path(__file__).parent / "skills_fixtures"


def test_discover_finds_sample_csv():
    loader = SkillLoader(FIXTURE_DIR)
    skills = loader.discover()
    assert "sample_csv" in skills
    spec = skills["sample_csv"]
    assert isinstance(spec, SkillSpec)
    assert spec.name == "sample_csv"
    assert "list-of-dicts" in spec.description.lower()
    assert "def write_csv" in spec.source


def test_discover_skips_skill_without_md():
    loader = SkillLoader(FIXTURE_DIR)
    skills = loader.discover()
    assert "no_md" not in skills


def test_discover_returns_empty_when_dir_missing(tmp_path):
    loader = SkillLoader(tmp_path / "does-not-exist")
    assert loader.discover() == {}


def test_discover_ignores_hidden_dirs(tmp_path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "script.py").write_text("x = 1")
    (tmp_path / ".hidden" / "SKILL.md").write_text("---\ndescription: x\n---")
    loader = SkillLoader(tmp_path)
    assert loader.discover() == {}


def test_description_falls_back_to_first_line_when_no_frontmatter(tmp_path):
    skill_dir = tmp_path / "plain"
    skill_dir.mkdir()
    (skill_dir / "script.py").write_text("def f(): pass")
    (skill_dir / "SKILL.md").write_text("Just a paragraph describing it.\n\nMore details.")
    loader = SkillLoader(tmp_path)
    spec = loader.discover()["plain"]
    assert spec.description == "Just a paragraph describing it."
```

- [ ] **Step 3: Run tests to verify failure**

Run: `uv run pytest tests/test_skills.py -v`
Expected: ImportError on `code_runner.skills`.

- [ ] **Step 4: Implement `skills.py`**

```python
# src/code_runner/skills.py
"""
Discover and expose skills from ~/.claude/code-runner-skills/<name>/.

A skill is a directory with two files:
  - script.py: Python source (trusted local code, full builtins)
  - SKILL.md: human description; optional YAML-ish frontmatter

The loader returns a catalog of SkillSpec objects. Wiring into the
executor and the namespace proxy is handled in skills_namespace later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillSpec:
    name: str
    source: str
    description: str
    path: Path


_FRONTMATTER_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


def _parse_description(md_text: str) -> str:
    text = md_text.strip()
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            front = text[3:end]
            m = _FRONTMATTER_DESC_RE.search(front)
            if m:
                return m.group(1)
            text = text[end + 3:].strip()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


class SkillLoader:
    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)

    def discover(self) -> dict[str, SkillSpec]:
        if not self.skills_dir.is_dir():
            return {}
        out: dict[str, SkillSpec] = {}
        for child in sorted(self.skills_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            script = child / "script.py"
            skill_md = child / "SKILL.md"
            if not script.is_file() or not skill_md.is_file():
                continue
            source = script.read_text(encoding="utf-8")
            description = _parse_description(
                skill_md.read_text(encoding="utf-8")
            )
            out[child.name] = SkillSpec(
                name=child.name,
                source=source,
                description=description,
                path=child,
            )
        return out
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_skills.py -v`
Expected: 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/code_runner/skills.py tests/test_skills.py tests/skills_fixtures/
git commit -m "feat(skills): SkillLoader discovers ~/.claude/code-runner-skills

Each skill is a directory with script.py + SKILL.md. The loader
returns a catalog of SkillSpec(name, source, description, path).
Hidden dirs and incomplete skills are skipped. Description is read
from frontmatter or the first non-empty line of SKILL.md."
```

---

### Task 5: SkillsNamespace — exec skill source and proxy callables

**Goal:** Turn the catalog into a `skills.<name>.<fn>` proxy object suitable for injection into the sandbox namespace.

**Files:**
- Modify: `src/code_runner/skills.py`
- Modify: `tests/test_skills.py`

**Acceptance Criteria:**
- [ ] `SkillsNamespace(specs: dict[str, SkillSpec])` is attribute-accessible: `ns.sample_csv` returns a SkillProxy
- [ ] `SkillProxy.write_csv(rows, path)` invokes the function defined in script.py
- [ ] Source `exec`'d once per `SkillsNamespace` instance with full builtins (skills are trusted)
- [ ] Skill source can `import` (no AST validation)
- [ ] Names starting with `_` are not exposed
- [ ] Missing skill: `ns.unknown` raises `AttributeError`
- [ ] Missing function: `ns.sample_csv.unknown_fn` raises `AttributeError`
- [ ] `__repr__` lists available skills

**Verify:** `uv run pytest tests/test_skills.py -v`

**Steps:**

- [ ] **Step 1: Add failing tests**

```python
# append to tests/test_skills.py
from code_runner.skills import SkillsNamespace


def test_namespace_calls_skill_function(tmp_path):
    loader = SkillLoader(FIXTURE_DIR)
    ns = SkillsNamespace(loader.discover())
    out_path = tmp_path / "out.csv"
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    written = ns.sample_csv.write_csv(rows, str(out_path))
    assert written == 2
    text = out_path.read_text()
    assert "a,b" in text and "1,x" in text


def test_namespace_unknown_skill_raises():
    ns = SkillsNamespace({})
    with pytest.raises(AttributeError, match="unknown"):
        ns.unknown


def test_namespace_unknown_function_raises():
    loader = SkillLoader(FIXTURE_DIR)
    ns = SkillsNamespace(loader.discover())
    with pytest.raises(AttributeError):
        ns.sample_csv.does_not_exist


def test_namespace_hides_private_names():
    loader = SkillLoader(FIXTURE_DIR)
    ns = SkillsNamespace(loader.discover())
    # csv module is imported inside script.py; it must not leak as ns.sample_csv.csv
    with pytest.raises(AttributeError):
        ns.sample_csv._private  # nor any underscore-prefixed name


def test_namespace_repr_lists_skills():
    loader = SkillLoader(FIXTURE_DIR)
    ns = SkillsNamespace(loader.discover())
    r = repr(ns)
    assert "sample_csv" in r
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_skills.py -v -k "namespace"`
Expected: Fail on `ImportError: cannot import name 'SkillsNamespace'`.

- [ ] **Step 3: Extend `skills.py`**

```python
# append to src/code_runner/skills.py
import builtins as _builtins
from typing import Any


class SkillProxy:
    """Thin wrapper around a single skill's executed namespace."""

    __slots__ = ("_name", "_callables")

    def __init__(self, name: str, callables: dict[str, Any]) -> None:
        self._name = name
        self._callables = callables

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._callables:
            raise AttributeError(
                f"skill {self._name!r} has no callable {attr!r}"
            )
        return self._callables[attr]

    def __dir__(self):
        return list(self._callables)

    def __repr__(self):
        fns = ", ".join(sorted(self._callables))
        return f"<SkillProxy {self._name} fns=[{fns}]>"


class SkillsNamespace:
    """Lazily-built attribute namespace exposing skills.<name> as SkillProxy."""

    __slots__ = ("_proxies",)

    def __init__(self, specs: dict[str, "SkillSpec"]) -> None:
        self._proxies: dict[str, SkillProxy] = {}
        for name, spec in specs.items():
            module_ns: dict[str, Any] = {"__builtins__": _builtins.__dict__}
            try:
                exec(compile(spec.source, str(spec.path / "script.py"), "exec"), module_ns)
            except Exception as e:
                # Don't crash the executor on a broken skill — surface it via
                # an attribute that raises when called.
                self._proxies[name] = _BrokenSkill(name, e)
                continue
            callables = {
                k: v for k, v in module_ns.items()
                if callable(v) and not k.startswith("_")
            }
            self._proxies[name] = SkillProxy(name, callables)

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._proxies:
            raise AttributeError(f"unknown skill: {attr}")
        return self._proxies[attr]

    def __dir__(self):
        return list(self._proxies)

    def __repr__(self):
        names = ", ".join(sorted(self._proxies))
        return f"<Skills [{names}]>"


class _BrokenSkill:
    """Surfaces a skill load error only when accessed, not at server startup."""

    def __init__(self, name: str, error: Exception) -> None:
        self._name = name
        self._error = error

    def __getattr__(self, attr: str):
        raise RuntimeError(
            f"skill {self._name!r} failed to load: {self._error}"
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_skills.py -v`
Expected: All ~10 skills tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/code_runner/skills.py tests/test_skills.py
git commit -m "feat(skills): SkillsNamespace and SkillProxy for sandbox injection

Skill source is exec'd once with full builtins (trusted local code).
Public callables are exposed via skills.<name>.<fn>; underscore-prefixed
names are hidden. A skill that fails to load becomes a _BrokenSkill that
errors only when actually accessed."
```

---

### Task 6: Wire skills into executor

**Goal:** `skills` is available inside `execute_code`'s sandbox.

**Files:**
- Modify: `src/code_runner/executor.py`
- Modify: `tests/test_executor.py`

**Acceptance Criteria:**
- [ ] `CodeExecutor` accepts an optional `SkillsNamespace`
- [ ] When provided, `skills` is in the namespace; absent otherwise
- [ ] User code can call `skills.sample_csv.write_csv(...)`
- [ ] Skill calls combine with workspace: write CSV to workspace, then read it back
- [ ] Tests pass

**Verify:** `uv run pytest tests/test_executor.py -v`

**Steps:**

- [ ] **Step 1: Add failing tests**

```python
# append to tests/test_executor.py
from code_runner.skills import SkillLoader, SkillsNamespace
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "skills_fixtures"


@pytest.mark.anyio
async def test_skill_callable_from_sandbox(tmp_path, fake_pool):
    wm = WorkspaceManager(root=tmp_path)
    ns = SkillsNamespace(SkillLoader(FIXTURE_DIR).discover())
    ex = CodeExecutor(fake_pool, workspace=wm, skills=ns)

    code = """
rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
written = skills.sample_csv.write_csv(rows, "out.csv")
print(written)
print(open("out.csv","r").read())
"""
    result = await ex.execute(code, session_id="s1")
    assert result["success"], result["error"]
    assert "2" in result["output"]
    assert "a,b" in result["output"]


@pytest.mark.anyio
async def test_skills_absent_when_not_provided(fake_pool, tmp_path):
    ex = CodeExecutor(fake_pool, workspace=WorkspaceManager(tmp_path))
    result = await ex.execute("print(skills)", session_id="s1")
    assert not result["success"]
    assert "skills" in (result["error"] or "")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_executor.py -v -k "skill"`
Expected: Fail on missing `skills` kwarg.

- [ ] **Step 3: Modify `executor.py`**

1. Import:

```python
from .skills import SkillsNamespace
```

2. Update `__init__`:

```python
def __init__(
    self,
    pool,
    recorder: "MetricsRecorder | None" = None,
    workspace: "WorkspaceManager | None" = None,
    skills: "SkillsNamespace | None" = None,
):
    ...
    self.skills = skills
```

3. Update `_build_namespace` to inject `skills` when present:

```python
if self.skills is not None:
    namespace["skills"] = self.skills
```

(Place this alongside the existing namespace dict construction.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_executor.py -v`
Expected: All pass including 2 new skill tests.

- [ ] **Step 5: Commit**

```bash
git add src/code_runner/executor.py tests/test_executor.py
git commit -m "feat(executor): inject skills namespace into sandbox

When a SkillsNamespace is wired into CodeExecutor, user code can
call skills.<name>.<fn>(...) directly. Skills compose with the
workspace: write outputs from a skill via the injected open()."
```

---

### Task 7: save_skill MCP tool with hot reload

**Goal:** A new tool that lets `execute_code` author skills from inside the sandbox; reload happens before next call sees them.

**Files:**
- Modify: `src/code_runner/server.py`
- Modify: `src/code_runner/skills.py` — add `save_skill_files` helper, `SkillsNamespace.refresh()` style API
- Create: `tests/test_save_skill.py`

**Acceptance Criteria:**
- [ ] New MCP tool `save_skill(name, code, description)` writes `~/.claude/code-runner-skills/<name>/{script.py,SKILL.md}`
- [ ] Validates `name` matches `^[a-z][a-z0-9_]{0,39}$`
- [ ] After save, `lifespan_context["skills"]` reflects new skill on the next `execute_code`
- [ ] Returns a confirmation string with absolute path
- [ ] Refuses to overwrite without `overwrite=True`? **No, allow overwrite** — author iteration is the normal path; user can git-track if they want
- [ ] Tests pass

**Verify:** `uv run pytest tests/test_save_skill.py -v`

**Steps:**

- [ ] **Step 1: Write helper + reload API**

In `skills.py`, add:

```python
import re as _re
_SKILL_NAME_RE = _re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def validate_skill_name(name: str) -> None:
    if not _SKILL_NAME_RE.match(name or ""):
        raise ValueError(
            f"invalid skill name {name!r}: must match {_SKILL_NAME_RE.pattern}"
        )


def write_skill_files(skills_dir: Path, name: str, code: str, description: str) -> Path:
    validate_skill_name(name)
    target = Path(skills_dir) / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "script.py").write_text(code, encoding="utf-8")
    md = f"---\nname: {name}\ndescription: {description.strip()}\n---\n"
    (target / "SKILL.md").write_text(md, encoding="utf-8")
    return target
```

- [ ] **Step 2: Wire in server.py**

1. Imports:

```python
from pathlib import Path
from .skills import SkillLoader, SkillsNamespace, write_skill_files
```

2. Constants:

```python
SKILLS_DIR = Path.home() / ".claude" / "code-runner-skills"
```

3. Update `lifespan` to load skills + store loader:

```python
loader = SkillLoader(SKILLS_DIR)
skills_ns = SkillsNamespace(loader.discover())
executor = CodeExecutor(pool, recorder=recorder, skills=skills_ns)
try:
    yield {
        "pool": pool,
        "executor": executor,
        "skills_loader": loader,
    }
finally:
    await pool.shutdown()
```

4. Add the tool:

```python
@mcp.tool()
async def save_skill(name: str, code: str, description: str, ctx: Context) -> str:
    """
    Save a skill to ~/.claude/code-runner-skills/<name>/.

    A skill is a Python file plus a description. Once saved, its public
    functions are immediately available inside execute_code as
    skills.<name>.<function_name>(...). Skills are local and trusted —
    they run with full Python builtins, can import packages from this
    server's venv, and are persistent across restarts.

    Args:
        name: lowercase alphanumeric + underscore, max 40 chars
        code: full Python source for script.py
        description: one-sentence summary used in list_available_tools
    """
    target = write_skill_files(SKILLS_DIR, name, code, description)

    loader: SkillLoader = ctx.request_context.lifespan_context["skills_loader"]
    new_ns = SkillsNamespace(loader.discover())
    ctx.request_context.lifespan_context["executor"].skills = new_ns

    return f"Saved skill {name!r} to {target}"
```

- [ ] **Step 3: Tests**

```python
# tests/test_save_skill.py
import pytest
from pathlib import Path

from code_runner.skills import (
    SkillLoader, SkillsNamespace, write_skill_files, validate_skill_name
)


def test_validate_skill_name_accepts_good():
    validate_skill_name("foo")
    validate_skill_name("foo_bar_2")


def test_validate_skill_name_rejects_bad():
    for bad in ["", "Foo", "1foo", "foo-bar", "foo bar", "x" * 41, "foo.bar"]:
        with pytest.raises(ValueError):
            validate_skill_name(bad)


def test_write_skill_files_creates_both(tmp_path):
    target = write_skill_files(tmp_path, "demo", "def f(): return 1", "demo skill")
    assert (target / "script.py").read_text() == "def f(): return 1"
    md = (target / "SKILL.md").read_text()
    assert "description: demo skill" in md


def test_write_then_discover_then_call(tmp_path):
    write_skill_files(tmp_path, "demo", "def f(): return 42", "answer")
    specs = SkillLoader(tmp_path).discover()
    ns = SkillsNamespace(specs)
    assert ns.demo.f() == 42
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/code_runner/server.py src/code_runner/skills.py tests/test_save_skill.py
git commit -m "feat(skills): save_skill MCP tool with in-process hot reload

LLM can write skills from inside execute_code via save_skill(name,
code, description). Files land in ~/.claude/code-runner-skills/<name>/.
The executor's SkillsNamespace is rebuilt so subsequent execute_code
calls in the same process see the new skill without a restart."
```

---

### Task 8: list_available_tools — surface skills

**Goal:** Skills appear in the overview so the LLM can find them via the same discovery path it already uses for MCP tools.

**Files:**
- Modify: `src/code_runner/server.py`
- Modify: `tests/test_search_tools.py` (or new test file)

**Acceptance Criteria:**
- [ ] `list_available_tools` output includes a `# === Skills ===` section when skills are loaded
- [ ] Each line: `# - skills.<name>: <description>`
- [ ] Section is omitted when there are no skills
- [ ] Existing tests still pass

**Verify:** `uv run pytest tests/ -v`

**Steps:**

- [ ] **Step 1: Add a failing test**

```python
# tests/test_skills_overview.py
from code_runner.skills import SkillSpec, SkillsNamespace
from code_runner.server import _format_skills_section


def test_skills_section_lists_each_skill():
    specs = {
        "csv_export": SkillSpec("csv_export", "", "Write rows to CSV.", path=None),
        "snapshot_diff": SkillSpec("snapshot_diff", "", "Diff two row lists.", path=None),
    }
    out = _format_skills_section(specs)
    assert "skills.csv_export" in out
    assert "Write rows to CSV." in out
    assert "skills.snapshot_diff" in out


def test_skills_section_empty_when_no_skills():
    assert _format_skills_section({}) == ""
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_skills_overview.py -v`
Expected: ImportError on `_format_skills_section`.

- [ ] **Step 3: Implement helper + integrate**

In `server.py`:

```python
def _format_skills_section(specs: dict) -> str:
    if not specs:
        return ""
    lines = ["", "# === Skills ===", "# (call as skills.<name>.<fn>(...))"]
    for name in sorted(specs):
        desc = (specs[name].description or "").strip()
        line = f"# - skills.{name}"
        if desc:
            line += f": {desc}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
async def list_available_tools(ctx: Context) -> str:
    """..."""  # keep existing docstring
    pool: MCPClientPool = ctx.request_context.lifespan_context["pool"]
    py_name_map = pool.py_name_map()
    tools_by_server = pool.get_all_tools()

    if not py_name_map:
        return "No MCP servers connected."

    overview = _overview_logic(tools_by_server, py_name_map)

    if pool.failed:
        lines = ["", "# === Failed to connect ==="]
        for name, err in pool.failed.items():
            lines.append(f"# {name}: {err}")
        overview += "\n".join(lines)

    loader = ctx.request_context.lifespan_context.get("skills_loader")
    if loader is not None:
        overview += _format_skills_section(loader.discover())

    return overview
```

Note: re-discovering on each call keeps things fresh after `save_skill`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/code_runner/server.py tests/test_skills_overview.py
git commit -m "feat(server): list skills in list_available_tools overview

Adds a '=== Skills ===' section listing skills.<name>: description.
Section is omitted when no skills are loaded. The loader rediscovers
on each call so save_skill is reflected immediately."
```

---

### Task 9: Bundled skill templates

**Goal:** Ship 3 reference skills under `skills_templates/` in the repo. Document how to install them.

**Files:**
- Create: `skills_templates/csv_export/script.py`
- Create: `skills_templates/csv_export/SKILL.md`
- Create: `skills_templates/snapshot_diff/script.py`
- Create: `skills_templates/snapshot_diff/SKILL.md`
- Create: `skills_templates/schema_dump/script.py`
- Create: `skills_templates/schema_dump/SKILL.md`
- Create: `tests/test_bundled_skills.py`
- Modify: `CLAUDE.md` — install-templates section

**Acceptance Criteria:**
- [ ] All three templates load without error
- [ ] Each defines at least one callable + has a non-empty description
- [ ] Test runs the templates against sample inputs and checks expected outputs
- [ ] CLAUDE.md documents `cp -r skills_templates/* ~/.claude/code-runner-skills/`

**Verify:** `uv run pytest tests/test_bundled_skills.py -v`

**Steps:**

- [ ] **Step 1: csv_export**

`skills_templates/csv_export/script.py`:

```python
import csv

def write_rows(rows, path):
    """Write a list of dicts to a CSV inside the workspace.

    Returns the number of rows written. Empty input creates a 0-byte file.
    """
    if not rows:
        with open(path, "w") as f:
            pass
        return 0
    keys = list(rows[0].keys())
    with open(path, "w") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
```

`skills_templates/csv_export/SKILL.md`:

```markdown
---
name: csv_export
description: Write list-of-dicts rows to CSV inside the session workspace.
---

# csv_export

```python
rows = await postgres_x.execute_sql(sql="SELECT id, total FROM orders LIMIT 100")
n = skills.csv_export.write_rows(rows, "orders.csv")
```
```

- [ ] **Step 2: snapshot_diff**

`skills_templates/snapshot_diff/script.py`:

```python
def diff(before, after, key):
    """Return {added, removed, changed} for two lists of dicts keyed by `key`.

    `changed` lists keys whose row changed; `added`/`removed` list keys
    appearing only in one snapshot.
    """
    by_b = {r[key]: r for r in before}
    by_a = {r[key]: r for r in after}
    bk, ak = set(by_b), set(by_a)
    return {
        "added": sorted(ak - bk),
        "removed": sorted(bk - ak),
        "changed": sorted(k for k in bk & ak if by_b[k] != by_a[k]),
    }
```

`skills_templates/snapshot_diff/SKILL.md`:

```markdown
---
name: snapshot_diff
description: Diff two row-lists by key; return added/removed/changed.
---

# snapshot_diff

Use before and after a fix to prove what actually changed.

```python
diff = skills.snapshot_diff.diff(rows_before, rows_after, key="id")
print(diff)
```
```

- [ ] **Step 3: schema_dump**

`skills_templates/schema_dump/script.py`:

```python
def render_columns(columns):
    """Pretty-print a column list-of-dicts as a fixed-width table.

    Each column dict needs at least 'name' and 'type'. Other fields
    (nullable, default, ...) are rendered if present.
    """
    if not columns:
        return "(no columns)"
    keys = list(columns[0].keys())
    widths = {k: max(len(str(k)), max(len(str(c.get(k, ""))) for c in columns)) for k in keys}
    header = "  ".join(str(k).ljust(widths[k]) for k in keys)
    sep = "  ".join("-" * widths[k] for k in keys)
    body = "\n".join(
        "  ".join(str(c.get(k, "")).ljust(widths[k]) for k in keys)
        for c in columns
    )
    return f"{header}\n{sep}\n{body}"
```

`skills_templates/schema_dump/SKILL.md`:

```markdown
---
name: schema_dump
description: Render column metadata (name/type/nullable/...) as a fixed-width table.
---

# schema_dump

Useful when exploring a new database via information_schema.

```python
cols = await postgres_x.execute_sql(sql="""
    SELECT column_name AS name, data_type AS type, is_nullable AS nullable
    FROM information_schema.columns
    WHERE table_name = 'orders'
""")
print(skills.schema_dump.render_columns(cols))
```
```

- [ ] **Step 4: Tests**

```python
# tests/test_bundled_skills.py
from pathlib import Path

import pytest

from code_runner.skills import SkillLoader, SkillsNamespace


TEMPLATES = Path(__file__).parent.parent / "skills_templates"


@pytest.fixture(scope="module")
def ns():
    return SkillsNamespace(SkillLoader(TEMPLATES).discover())


def test_csv_export_writes_file(ns, tmp_path):
    out = tmp_path / "x.csv"
    n = ns.csv_export.write_rows(
        [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], str(out)
    )
    assert n == 2
    text = out.read_text()
    assert "a,b" in text and "1,x" in text


def test_snapshot_diff_detects_changes(ns):
    before = [{"id": 1, "v": 10}, {"id": 2, "v": 20}]
    after = [{"id": 1, "v": 11}, {"id": 3, "v": 30}]
    d = ns.snapshot_diff.diff(before, after, key="id")
    assert d == {"added": [3], "removed": [2], "changed": [1]}


def test_schema_dump_renders_table(ns):
    cols = [
        {"name": "id", "type": "int", "nullable": "no"},
        {"name": "amount", "type": "decimal", "nullable": "yes"},
    ]
    out = ns.schema_dump.render_columns(cols)
    assert "name" in out and "type" in out
    assert "id" in out and "decimal" in out
```

- [ ] **Step 5: Document install in CLAUDE.md**

Add to `CLAUDE.md`:

```markdown
## Installing bundled skills

```bash
mkdir -p ~/.claude/code-runner-skills
cp -r skills_templates/* ~/.claude/code-runner-skills/
```

After install, restart code-runner so SkillLoader picks them up. Calling `save_skill` from inside `execute_code` reloads in-process without restart.
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/ -v`
Expected: all pass (including 3 new bundled-skill tests).

- [ ] **Step 7: Commit**

```bash
git add skills_templates/ tests/test_bundled_skills.py CLAUDE.md
git commit -m "feat(skills): bundle csv_export, snapshot_diff, schema_dump templates

skills_templates/ ships three starter skills under the new templates
layout. Tests load and exercise each. CLAUDE.md documents how to
install them into ~/.claude/code-runner-skills/."
```

---

## Self-Review Checklist

- ✅ Spec coverage: workspace (Tasks 1-3), skills loader/namespace/wiring (4-6), save_skill (7), discoverability (8), bundled templates (9), CLAUDE.md (0+9). PII-tokenization, FS-discovery, process isolation explicitly out of scope per memory #254.
- ✅ No placeholders: every step has runnable code or exact commands.
- ✅ Type consistency: `WorkspaceManager`, `WorkspaceError`, `SkillSpec`, `SkillsNamespace`, `SkillProxy`, `SkillLoader`, `safe_open`, `write_skill_files`, `validate_skill_name`, `_format_skills_section`, `DEFAULT_WRITE_CAP`, `DEFAULT_WORKSPACE_ROOT`, `SKILLS_DIR` consistent across tasks.
- ✅ TDD discipline: every implementation task starts with a failing test step.
- ✅ Frequent commits: one per task.

"""
Discover and expose skills from ~/.claude/code-runner-skills/<name>/.

A skill is a directory with two files:
  - script.py: Python source (trusted local code, full builtins)
  - SKILL.md: human description; optional YAML-ish frontmatter

The loader returns a catalog of SkillSpec objects. Wiring into the
executor and the namespace proxy is handled in Task 5.
"""

from __future__ import annotations

import builtins as _builtins
import contextvars
import functools
import inspect
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_runner.skill_status import record_outcome


@dataclass(frozen=True)
class SkillSpec:
    name: str
    source: str
    description: str
    path: Path
    replaces: tuple[str, ...] = ()


_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def validate_skill_name(name: str) -> None:
    if not _SKILL_NAME_RE.match(name or ""):
        raise ValueError(
            f"invalid skill name {name!r}: must match {_SKILL_NAME_RE.pattern}"
        )


def write_skill_files(
    skills_dir: Path, name: str, code: str, description: str
) -> Path:
    """Write a skill into <skills_dir>/<name>/. Returns the dir path.

    Compiles `code` first so we don't persist a broken skill — the
    caller (save_skill MCP tool) gets the SyntaxError back and can
    surface it to the LLM author. The compile uses the eventual on-disk
    filename so traceback line numbers are useful when debugging.
    """
    validate_skill_name(name)
    target = Path(skills_dir) / name
    script_path = target / "script.py"
    try:
        compile(code, str(script_path), "exec")
    except SyntaxError as e:
        raise ValueError(f"skill {name!r} has invalid Python syntax: {e}") from e

    target.mkdir(parents=True, exist_ok=True)
    script_path.write_text(code, encoding="utf-8")
    md = f"---\nname: {name}\ndescription: {description.strip()}\n---\n"
    (target / "SKILL.md").write_text(md, encoding="utf-8")
    return target


_FRONTMATTER_DESC_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)


_FRONTMATTER_REPLACES_RE = re.compile(r"^replaces:\s*\[(.*?)\]\s*$", re.MULTILINE)


def _parse_replaces(md_text: str) -> tuple[str, ...]:
    """`replaces: [server.tool, ...]` from frontmatter: raw tools this skill supersedes."""
    text = md_text.strip()
    if not text.startswith("---"):
        return ()
    end = text.find("---", 3)
    if end == -1:
        return ()
    m = _FRONTMATTER_REPLACES_RE.search(text[3:end])
    if not m:
        return ()
    return tuple(t for t in (x.strip().strip("'\"") for x in m.group(1).split(",")) if t)


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
            md_text = skill_md.read_text(encoding="utf-8")
            description = _parse_description(md_text)
            out[child.name] = SkillSpec(
                name=child.name,
                source=source,
                description=description,
                path=child,
                replaces=_parse_replaces(md_text),
            )
        return out


class SkillProxy:
    """Thin wrapper exposing a single skill's public callables.

    Underscore-prefixed names (including imported modules used inside the
    skill) are hidden so they don't pollute `dir(skills.foo)` or shadow
    the framework namespace.
    """

    __slots__ = ("_name", "_callables", "_calls")

    def __init__(
        self,
        name: str,
        callables: dict[str, Any],
        skill_dir: Path | None = None,
        call_log: list[dict[str, Any]] | None = None,
    ) -> None:
        self._name = name
        # Raw functions: find_callables reads their real signatures.
        self._callables = callables
        # What user code gets: wrapped once, so `skills.x.f is skills.x.f`.
        self._calls = (
            {
                k: _with_outcome(v, skill_dir, name, k, call_log)
                for k, v in callables.items()
            }
            if skill_dir is not None else callables
        )

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._calls:
            raise AttributeError(
                f"skill {self._name!r} has no callable {attr!r}"
            )
        return self._calls[attr]

    def __dir__(self):
        return list(self._callables)

    def __repr__(self):
        fns = ", ".join(sorted(self._callables))
        return f"<SkillProxy {self._name} fns=[{fns}]>"


# Name of the skill whose callable is running now. The side-effect gate reads
# it so a refused call from inside skill X doesn't advise "use skills.X" (SHA-465).
active_skill: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_skill", default=None
)

# Tool calls made by the running skill call (SHA-523): a skill that raised
# before its first one failed on its input, and its raw tools would not help.
_tool_calls: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "_tool_calls", default=None
)


def note_tool_call() -> None:
    """Count one proxied tool call against the running skill call, if any."""
    counter = _tool_calls.get()
    if counter is not None:
        counter[0] += 1


# Builtin name under which skills see the skill-side MCP proxies (SHA-502).
# ~/.claude/lib/skill_ns.py resolves servers from it, never from user frames.
SKILL_SERVERS = "_cr_servers"


def _with_outcome(
    fn: Any,
    skill_dir: Path,
    name: str,
    fn_name: str,
    call_log: list[dict[str, Any]] | None = None,
) -> Any:
    """Wrap a skill callable so each call's outcome lands in status.json.

    The same outcome is appended to `call_log` (SHA-484): status.json keeps
    lifetime counters per skill, the log feeds per-function `skill_call`
    metrics events, from which the trim report reads usage over a window.

    Success = no exception (async: after the await). Exceptions from the skill
    and from recording both propagate. An async `fn` is called eagerly and only
    the await is deferred: bad arguments must raise TypeError at the call site,
    as the bare function does, so the executor's signature HINT still sees it.
    """
    is_async = inspect.iscoroutinefunction(fn)

    def outcome(
        ok: bool, start: float, calls: int, exc: BaseException | None = None
    ) -> None:
        # A nested skill's calls are the outer skill's calls too.
        outer = _tool_calls.get()
        if outer is not None:
            outer[0] += calls
        if call_log is not None:
            event: dict[str, Any] = {
                "skill": name, "fn": fn_name, "success": ok,
                "duration_ms": round((time.monotonic() - start) * 1000, 2),
                "tool_calls": calls,
            }
            if exc is not None:
                # Class name only: the message may hold a secret or untrusted text
                # and the repo has no redaction helper to scrub it.
                event["error"] = type(exc).__name__
                # The exception carries its own events: the catch site marks them
                # fatal only if this very object ended the block (SHA-523).
                exc.__dict__.setdefault(_EVENTS_ATTR, []).append(event)
            call_log.append(event)
        record_outcome(skill_dir, ok=ok)

    async def settle(coro: Any, start: float, counter: list[int]) -> Any:
        token = active_skill.set(name)
        calls_token = _tool_calls.set(counter)
        try:
            result = await coro
        except Exception as e:
            _tool_calls.reset(calls_token)
            outcome(False, start, counter[0], e)
            raise
        finally:
            active_skill.reset(token)
        _tool_calls.reset(calls_token)
        outcome(True, start, counter[0])
        return result

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        counter = [0]
        token = active_skill.set(name)
        calls_token = _tool_calls.set(counter)
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            _tool_calls.reset(calls_token)
            outcome(False, start, counter[0], e)
            raise
        finally:
            active_skill.reset(token)
        _tool_calls.reset(calls_token)
        if is_async:
            return settle(result, start, counter)
        outcome(True, start, counter[0])
        return result

    return wrapper


_EVENTS_ATTR = "_cr_skill_events"


def mark_fatal(exc: BaseException) -> None:
    """Flag the skill calls behind the exception that ended the block (SHA-523).

    Covers `exc` and its explicit `raise X from e` chain (`__cause__`; the
    implicit `__context__` is not followed). A skill error the user code caught
    and replaced with an unchained exception — even of the same class — stays
    unflagged.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        for event in getattr(exc, _EVENTS_ATTR, ()):
            event["fatal"] = True
        exc = exc.__cause__


class _BrokenSkill:
    """Surfaces a skill load error only when accessed, not at startup.

    A single broken skill must not crash the executor — it just becomes
    unreachable. Accessing any attribute raises with the original error.
    """

    __slots__ = ("_name", "_error")

    def __init__(self, name: str, error: Exception) -> None:
        self._name = name
        self._error = error

    def __getattr__(self, attr: str):
        raise RuntimeError(
            f"skill {self._name!r} failed to load: {self._error}"
        )


class SkillsNamespace:
    """Attribute-accessible bag of skills for sandbox injection.

    Each spec's source is exec'd ONCE at construction time in an isolated
    module dict with full builtins (skills are trusted local code; we
    don't run AST validation on them). Public callables become attributes
    on the per-skill SkillProxy.

    Trust envelope: the skills directory is user-owned. The save_skill
    MCP tool (Task 7) lets the LLM author code into that directory, so
    review save_skill carefully before widening any of the loader, exec,
    or builtins surface here.

    Builtin binding: a single shared __builtins__ dict (a copy of the
    real builtins) is given to every skill. Callers can override entries
    with `bind(name, value)` — used by the executor to point `open` at
    the per-session workspace-bound safe_open before each user run.
    """

    __slots__ = (
        "_proxies", "_shared_builtins", "root", "replacements", "fallbacks", "_call_log",
    )

    def __init__(self, specs: dict[str, SkillSpec]) -> None:
        self._proxies: dict[str, Any] = {}
        # "server.tool" -> "skills.<name>.<fn>": what the side-effect gate
        # recommends instead of a raw tool a skill declared via `replaces:`.
        self.replacements: dict[str, str] = {}
        # skill name -> the raw "server.tool"s from its `replaces:` (SHA-523): the
        # fallback a failed skill call names, so the caller isn't left at a dead end.
        self.fallbacks: dict[str, tuple[str, ...]] = {}
        # One entry per skill-callable call since the last drain_calls().
        self._call_log: list[dict[str, Any]] = []
        # Where these skills were loaded from. Skill callables can't be pickled
        # across to an isolation child, so the child reloads from this path —
        # carrying it on the namespace means a caller can't wire up skills and
        # silently lose them under subprocess isolation.
        self.root: Path | None = next(
            (spec.path.parent for spec in specs.values()), None
        )
        # Shared so a single bind() reaches every skill at lookup time.
        self._shared_builtins: dict[str, Any] = dict(_builtins.__dict__)
        for name, spec in specs.items():
            module_ns: dict[str, Any] = {"__builtins__": self._shared_builtins}
            try:
                code = compile(
                    spec.source, str(spec.path / "script.py"), "exec"
                )
                exec(code, module_ns)
            except Exception as e:
                self._proxies[name] = _BrokenSkill(name, e)
                continue
            callables = {
                k: v for k, v in module_ns.items()
                if callable(v) and not k.startswith("_")
            }
            self._proxies[name] = SkillProxy(
                name, callables, spec.path, self._call_log
            )
            # `server.tool:fn` names the replacing function; bare `server.tool`
            # points at the skill as a whole rather than guessing a function.
            tools = []
            for raw in spec.replaces:
                tool, _, fn = raw.partition(":")
                target = f"skills.{name}.{fn}" if fn else f"skills.{name}"
                self.replacements.setdefault(tool, target)
                tools.append(tool)
            if tools:
                self.fallbacks[name] = tuple(tools)

    def find_callables(self, fn_name: str) -> list[tuple[str, Any]]:
        """All loaded skill callables named `fn_name`, as (skill_name, fn).

        A TypeError from CPython names only the function ("query() got an
        unexpected keyword argument 'limit'"), not the skill it lives in.
        This resolves that name back to the actual object(s) so the executor
        can show the real signature instead of leaving the caller to guess.
        """
        out: list[tuple[str, Any]] = []
        for skill_name, proxy in sorted(self._proxies.items()):
            if not isinstance(proxy, SkillProxy):
                continue
            fn = proxy._callables.get(fn_name)
            if fn is not None:
                out.append((skill_name, fn))
        return out

    def catalog(self) -> dict[str, list[str]]:
        """Skill name → its public functions; a skill that failed to load has none."""
        return {
            name: sorted(proxy._callables) if isinstance(proxy, SkillProxy) else []
            for name, proxy in sorted(self._proxies.items())
        }

    def drain_calls(self) -> list[dict[str, Any]]:
        """Return and clear the calls logged since the previous drain.

        The executor drains once per run (runs are serialized), so the list
        holds exactly that run's skill calls.
        """
        calls = list(self._call_log)
        self._call_log.clear()
        return calls

    def bind(self, name: str, value: Any) -> None:
        """Override a builtin seen by all loaded skills.

        Called by the executor before each run to point `open` at the
        sandbox-bound safe_open for the current session. Idempotent.

        Trust note: skill code can also write into this dict at runtime
        (e.g. `__builtins__["open"] = real_open`). The skills directory
        is treated as trusted — bugs there can confuse later runs in the
        same process. save_skill is the only path through which untrusted
        (LLM-authored) code reaches this dict; review it carefully.
        """
        self._shared_builtins[name] = value

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

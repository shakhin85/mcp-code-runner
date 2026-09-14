"""Per-call outcome counting and demotion for code-runner skills (SHA-149).

A skill directory may carry `status.json`:

  {"status": "TRIAL"|"VERIFIED", "uses": N, "successes": N}

Every call through `SkillProxy` records its outcome here: `uses` grows by one,
`successes` too when the call did not raise. A VERIFIED skill whose success
rate drops below SUCCESS_RATE_THRESHOLD after MIN_USES_FOR_DEMOTION calls goes
back to TRIAL. Promotion TRIAL -> VERIFIED is golden-test driven and lives in
`~/.claude/tools/skill-verify.py`, not here.

A skill without `status.json` is not enrolled and is not counted.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SUCCESS_RATE_THRESHOLD = 0.8
MIN_USES_FOR_DEMOTION = 5

STATUS_TRIAL = "TRIAL"
STATUS_VERIFIED = "VERIFIED"
STATUS_FILE = "status.json"


def demote(status: dict[str, Any]) -> dict[str, Any]:
    """Pure: VERIFIED -> TRIAL if uses >= MIN_USES_FOR_DEMOTION and rate < threshold."""
    uses, successes = status.get("uses", 0), status.get("successes", 0)
    if (status.get("status") == STATUS_VERIFIED and uses >= MIN_USES_FOR_DEMOTION
            and (successes / uses) < SUCCESS_RATE_THRESHOLD):
        return {**status, "status": STATUS_TRIAL}
    return dict(status)


def record_outcome(skill_dir: Path, ok: bool) -> None:
    """Count one call in `skill_dir/status.json` and apply demote(), atomically.

    Concurrent calls (several isolation children, the in-process path) are
    serialized by flock on a sibling lock file; the new JSON lands via
    tmp-file + os.replace, so a reader never sees a half-written status.
    """
    path = skill_dir / STATUS_FILE
    if not path.exists():
        return
    with open(skill_dir / f"{STATUS_FILE}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = json.loads(path.read_text(encoding="utf-8"))
        status["uses"] = status.get("uses", 0) + 1
        status["successes"] = status.get("successes", 0) + int(ok)
        _write_atomic(path, demote(status))


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise

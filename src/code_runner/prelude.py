"""Auto-prelude: short aliases for the hottest code-runner skill functions.

Injected into every execute_code namespace so users don't need to remember
the full `skills.<name>.<function>` path or the exact positional ordering.

Each alias is a direct reference to the underlying skill function — calling
`fg_query(...)` is identical to `skills.forgetful_compact.query(...)`.

If a skill is missing (not installed on this machine), its aliases are
silently skipped — the namespace stays consistent across installations.

To add a new alias: append a row to ALIASES below. Format:
    (alias_name, skill_name, function_name)
"""

from __future__ import annotations

from typing import Any

# (alias, skill, function) — keep alphabetical within each section
ALIASES: list[tuple[str, str, str]] = [
    # Forgetful memory shortcuts
    ("fg_by_id",   "forgetful_get",     "by_id"),
    ("fg_by_ids",  "forgetful_get",     "by_ids"),
    ("fg_create",  "forgetful_create",  "memory"),
    ("fg_projects","forgetful_create",  "list_projects"),
    ("fg_query",   "forgetful_compact", "query"),

    # Data utils
    ("csv_write",  "csv_export",        "write_rows"),
    ("md_table",   "md_table",          "table"),
    ("sum_col",    "sum_column",        "sum_column"),
    ("group_sum",  "sum_column",        "group_sum"),

    # Snapshot diff
    ("snap_diff",  "snapshot_diff",     "diff"),
    ("snap_delta", "snapshot_diff",     "numeric_delta"),

    # Power BI
    ("pbi_dax",        "pbi", "dax"),
    ("pbi_conn",       "pbi", "conn"),
    ("pbi_instances",  "pbi", "list_instances"),
    ("pbi_refresh",    "pbi", "refresh_table"),

    # Introspection
    ("probe",      "sandbox_introspect","probe"),
    ("probe_all",  "sandbox_introspect","list_all"),
]


def build(skills_namespace: Any) -> dict[str, Any]:
    """Return alias dict ready to merge into execute_code namespace.

    Args:
        skills_namespace: SkillsNamespace instance (may be None if no skills
            are loaded — returns empty dict in that case).

    Skills or functions that are not available are silently skipped, so this
    works on machines with partial skill installations.
    """
    if skills_namespace is None:
        return {}

    out: dict[str, Any] = {}
    for alias, skill_name, func_name in ALIASES:
        try:
            skill_proxy = getattr(skills_namespace, skill_name)
        except (AttributeError, RuntimeError):
            # AttributeError: skill not installed.
            # RuntimeError: skill failed to load (_BrokenSkill placeholder).
            continue
        try:
            fn = getattr(skill_proxy, func_name)
        except (AttributeError, RuntimeError):
            continue
        out[alias] = fn
    return out


def describe() -> list[tuple[str, str]]:
    """Return [(alias, 'skills.<skill>.<fn>'), ...] for documentation/overview."""
    return [(alias, f"skills.{s}.{f}") for alias, s, f in ALIASES]

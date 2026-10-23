# Task: updated_code_runner

**Status:** done
**Done means:** SHA-487: str.format/format_map в in-process режиме не резолвит _-атрибуты; xfail-тест снят и зелёный; uv run pytest зелёный
**Linear:** —
**Updated:** 2026-10-23T10:09:46.577983+00:00

## Steps
- [x] 6/6 done — latest: SHA-484 skill_call + trim report [evidence: pytest 460 passed; живой skill_call в metrics.jsonl после рестарта; commit 936f68e], SHA-486 raw-cap hint [evidence: pytest tests/test_raw_cap_hint.py: 4 failed без фикса → 4 passed; full 464 passed; commit 16fb363], SHA-487 format-string _-attrs [evidence: uv run -m pytest → 484 passed (red: 13 failed + hook-bypass 1 failed); ruff clean] (+3 earlier)

## Checkpoint
закрыта: pytest 484 passed; xfail снят; коммит в fix/sha-487-format-private-attrs

## Next
—

## Refs
- episodes: —
- decisions: —
- why: —

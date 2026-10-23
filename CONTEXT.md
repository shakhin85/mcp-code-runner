# Task: updated_code_runner

**Status:** in_progress
**Done means:** SHA-503: side-effect гейт вычищает секреты из аргументов (inprocess+subprocess); SHA-502: PoC подмены server отклонён, replaces-скилл проходит без allow_side_effects; pytest зелёный, ревью approve
**Linear:** —
**Updated:** 2026-10-23T19:04:20.002435+00:00

## Steps
- [x] 6/6 done — latest: SHA-481 merge+live [evidence: merge 060573c, pytest 561 passed; живой wigolo→linear_createComment: PermissionError, чистый аргумент прошёл гейт], SHA-499+500 coder через herdr-route [evidence: merge 5c99b89, pytest 583 passed, ruff rc=0, ревью approve; живой skills.forgetful_create.memory без allow_side_effects…], SHA-502+503 код+тесты+live [evidence: commit 2420f05 + ~/.claude 123f656; pytest 590 passed, ruff ok; PoC-тест на старом skill_ns 2 failed; live: skills.forg…] (+3 earlier)

## Checkpoint
—

## Next
ждём ревью sonnet → merge fix/sha-502-503 в master, push, Linear SHA-502/503 Done (предложение агента)

## Refs
- episodes: —
- decisions: —
- why: —

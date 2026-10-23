# Task: updated_code_runner

**Status:** in_progress
**Done means:** SHA-503: side-effect гейт вычищает секреты из аргументов (inprocess+subprocess); SHA-502: PoC подмены server отклонён, replaces-скилл проходит без allow_side_effects; pytest зелёный, ревью approve
**Linear:** —
**Updated:** 2026-10-23T18:57:10.543525+00:00

## Steps
- [x] 5/5 done — latest: SHA-483 merge+live [evidence: merge a0c09d6, pytest 543 passed; живой KeyError в main-sha483b → HINT с фиксом из main-sha483a; baseline 3/77=3.9%], SHA-481 merge+live [evidence: merge 060573c, pytest 561 passed; живой wigolo→linear_createComment: PermissionError, чистый аргумент прошёл гейт], SHA-499+500 coder через herdr-route [evidence: merge 5c99b89, pytest 583 passed, ruff rc=0, ревью approve; живой skills.forgetful_create.memory без allow_side_effects…] (+2 earlier)

## Checkpoint
—

## Next
—

## Refs
- episodes: —
- decisions: —
- why: —

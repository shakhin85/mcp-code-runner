# Task: dual-graph-transport

**Status:** in_progress
**Done means:** Незакоммиченное в code-runner и ~/.claude разобрано: закоммичено или явно оставлено с причиной
**Linear:** —
**Updated:** 2026-10-25T14:16:57.080424+00:00

## Steps
- [x] 3/3 done — latest: rm-recursive-gate: scratchpad через hostos — на Windows падал на os.getuid и блокировал любой rm -r (.claude 2122c24) [evidence: git -C C:/Users/shalnazarov/.claude log -1 2122c24 → коммит этого хода (автозапись step-close-gate)], gitignore: локальное состояние patches/*, dgc-policy host-tasks, herdr-gpui-browser; трапп-лог и taskrec defender-exclusions (.claude 296cf1a) [evidence: git -C C:/Users/shalnazarov/.claude log -1 296cf1a → коммит этого хода (автозапись step-close-gate)], skill_status: блокировка через msvcrt на Windows (fcntl там нет) (code-runner 8cf3129) [evidence: git -C C:/mcp-servers/code-runner log -1 8cf3129 → коммит этого хода (автозапись step-close-gate)]

## Checkpoint
хвосты закрыты: code-runner 8cf3129 на fix/skill-status-windows (не смержено), ~/.claude 296cf1a; core.symlinks=true, 3 symlink пересозданы (предложение агента)

## Next
миграция долга invariant_15 партиями (coder) (предложение агента)

## Refs
- episodes: —
- decisions: —
- why: —

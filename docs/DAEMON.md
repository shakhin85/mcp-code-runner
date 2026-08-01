# Деплой: общий HTTP-демон под systemd

С 2026-08-01 предусмотрен основной способ запуска — **один общий демон**
(streamable-http) вместо per-session stdio-спавна из `~/.claude.json`.
Причина: per-session спавн давал `-32000 Connection closed` на первом вызове
(мёртвый pipe старой сессии), утечку процессов-сирот и boot ~8 с на каждую
сессию (коннект к ~12 downstream-серверам).

## Схема

```
Claude Code (любая сессия) ──HTTP──> :8030 (code-runner-proxy.socket, systemd)
                                        │ socket-activation
                                        v
                    systemd-socket-proxyd (idle-stop 15 мин)
                                        │
                                        v
                     :8031 code-runner-backend.service
                     (uv run code-runner, CODE_RUNNER_TRANSPORT=streamable-http)
```

Образец — цепочка forgetful-proxy/-backend: socket поднимает proxy, proxy
требует backend (`Requires=`), при простое proxy выходит и backend гаснет
(`StopWhenUnneeded=true`).

## Файлы

| Что | Где |
|---|---|
| env демона | `~/.config/code-runner/code-runner.env` |
| юниты | `~/.config/systemd/user/code-runner-{backend.service,proxy.service,proxy.socket}` |
| клиентский конфиг | `~/.claude.json` → `mcpServers.code-runner = {"type": "http", "url": "http://127.0.0.1:8030/mcp"}` |

Ключевые env (клиентский `env` из `~/.claude.json` до демона НЕ доезжает —
всё через EnvironmentFile юнита):

- `CODE_RUNNER_TRANSPORT=streamable-http` — включает HTTP-режим (`main()`
  выставляет `mcp.settings.host/port` из `FASTMCP_HOST`/`FASTMCP_PORT`
  явно: конструктор FastMCP перекрывает env-Settings своими дефолтами).
- `CODE_RUNNER_NO_PROJECT_CONFIG=1` — демон не подмешивает `.mcp.json`
  своего cwd; проект резолвится per-request (см. ниже).
- `CODE_RUNNER_SKIP_SERVERS=...` — как раньше.
- `PATH` с `~/.local/bin` — systemd даёт голый PATH, downstream-серверы
  (uv/npx) без него не стартуют.

## Проектные серверы (MCP roots)

В stdio-режиме проектный `.mcp.json` находился через cwd/process-tree.
У демона этого контекста нет — вместо него **MCP roots**: на первый запрос
MCP-сессии сервер спрашивает у клиента `list_roots()` (Claude Code отдаёт
`file://<project-dir>`, проверено по HTTP 01.08.2026), читает
`<root>/.mcp.json` + `<root>/.claude/settings.json` и лениво поднимает
недостающие серверы в **отдельный пул на проект** (`project_pools.py`).
Глобальные имена приоритетнее; пулы гаснут после 30 мин простоя. Roots
кешируются на MCP-сессию.

## Session-state под общим демоном

`execute_code`-сессии (`session_id`, LRU ≤20, idle 10 мин) живут в памяти
одного процесса на ВСЕ Claude-сессии: чужая сессия с тем же `session_id`
попадёт в то же namespace. Конвенция имён `main-<topic>` делает коллизии
маловероятными, но это осознанный компромисс. Исполнение сериализовано
`_exec_lock` (SIGALRM процесс-глобален) — тоже общее на всех клиентов.

## Управление

```bash
systemctl --user status code-runner-backend.service
journalctl --user -u code-runner-backend.service -n 50
systemctl --user restart code-runner-backend.service   # после правок кода
```

stdio-режим остаётся рабочим (дефолт без `CODE_RUNNER_TRANSPORT`) — для
отладки и не-systemd окружений.

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

## Установка одной командой

```bash
scripts/install-daemon.sh [--dry-run] [--unit-dir DIR]
```

Шаблоны юнитов лежат в `deploy/systemd/`. Плейсхолдер один: `@REPO_DIR@` —
путь репо, скрипт берёт его из `BASH_SOURCE` (симлинк разворачивается).
Специфайер `%h` остаётся для systemd.

| Шаг | Поведение |
|---|---|
| юниты | `proxy.socket`, `proxy.service`, `backend.service`, `limits.conf` — всегда; `forgetful-ready.conf` — только если у хоста есть `forgetful-proxy.socket` (иначе строка о пропуске) |
| повторный запуск | файл с тем же содержимым не трогается; запись атомарная (tmp + mv); без изменений печатает `no changes` |
| `code-runner.env` | нет файла — создаётся шаблон с комментариями, `chmod 600`; существующий не читается и не печатается |
| systemd | `daemon-reload` (если юниты изменились) и `enable --now code-runner-proxy.socket`; backend без `[Install]` |
| linger | при `Linger=no` печатает `loginctl enable-linger <user>`, сам не включает |
| `--dry-run` | печатает план, ничего не пишет |
| `--unit-dir DIR` | юниты в DIR (по умолчанию `~/.config/systemd/user`); systemctl, loginctl и smoke не вызываются |
| smoke | `curl` на `:8030/mcp` до 60 с (`CODE_RUNNER_SMOKE_TIMEOUT`); rc≠0 при провале |

Переменные: `CODE_RUNNER_ENV_DIR` (по умолчанию `~/.config/code-runner`),
`CODE_RUNNER_SMOKE_URL`, `CODE_RUNNER_UNIT_SEARCH_PATH` (где искать `forgetful-proxy.socket`).

В конце скрипт печатает команду переключения MCP-конфига на
`http://127.0.0.1:8030/mcp`; сам конфиг Claude Code не правит. Эталон юнитов для
тестов — копия живых юнитов lns-srv-tempdwh внутри `tests/test_install_daemon.py`.

## Файлы

| Что | Где |
|---|---|
| env демона | `~/.config/code-runner/code-runner.env` |
| юниты | `~/.config/systemd/user/code-runner-{backend.service,proxy.service,proxy.socket}` |
| клиентский конфиг | `~/.claude/.config.json` (нет его — `~/.claude.json`) → `mcpServers.code-runner = {"type": "http", "url": "http://127.0.0.1:8030/mcp"}` |

Глобальные downstream-серверы демон читает из того же файла, что Claude Code:
`~/.claude/.config.json`, а без него — `~/.claude.json` (SHA-492). Сервер,
добавленный в конфиг, демон видит после `systemctl --user restart code-runner-backend.service`.

Ключевые env (клиентский `env` из конфига Claude Code до демона НЕ доезжает —
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

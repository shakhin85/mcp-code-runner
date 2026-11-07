# Research: как поднять эффективность code-runner (2026-11-07)

Источники: agent-reach — Exa (7 запросов), Twitter (2 запроса), GitHub (2 запроса).
Reddit — dead: `rdt search` вернул `forbidden` 2 раза подряд.
Свои данные: `~/.cache/code-runner/metrics.jsonl`, окно 2026-11-01 … 2026-11-07.

## Наши метрики за 7 дней

| Показатель | Значение |
|---|---|
| Блоков `execute_code` | 177, упало 39 (22%) |
| Блоков без единого tool-вызова | 47 (27%), из них упало 30 |
| Блоков с 1 tool-вызовом | 57 (32%) |
| Классы ошибок | BlockedImport 11, NameError 9, ValueError 6, Timeout+TimeoutError 4, PermissionError 3 |
| Тяжёлые серверы (median bytes/call) | wigolo 36KB, grabber 30KB, forgetful 30KB, codegraph 27KB, exa 14KB |
| Лёгкие серверы | twitter 211B, gitea 93B, mssql 553B |

Вывод: 30 из 39 падений (77%) происходят до первого tool-вызова. Это трение API песочницы, а не сбой downstream-серверов.

## Инсайты из источников → что делать в code-runner

| # | Инсайт (источник) | Что это значит для нас | Действие | Статус |
|---|---|---|---|---|
| 1 | SQL уже является code mode. На Databricks code mode для SQL-задач экономил ≤6% или стоил дороже (+8%, +376%) и был в 2.5–5.5 раза медленнее. Модель тянула строки в Python, чтобы считать перцентили, вместо одного SQL ([farhathadi](https://farhathadi.substack.com/p/cut-your-mcp-token-bill-by-97-code)) | mssql: median 553B, 10 из 16 блоков — 1 вызов. Эти блоки не дают экономии | HINT в cost receipt: блок с 1 SQL-вызовом и Python-агрегацией результата → «перенеси агрегацию в SQL (UNION ALL / оконные функции), зови напрямую» | сделано (c590341) |
| 2 | Break-even: code mode выигрывает только на fan-out и больших результатах. На τ²-bench (1–2 последовательных вызова) Anthropic получил +8% стоимости ([nodeflare](https://nodeflare.tech/blog/mcp-code-execution-token-savings)) | 32% блоков — один вызов. Решение «звать через песочницу» стоит принимать по серверу, а не по задаче | `get_metrics(servers=True)`: median/p90 bytes по серверу + вердикт `direct`/`sandbox`. Таблицу вердиктов вписать в `@preflight-routing` | сделано (c590341, строка в @preflight-routing) |
| 3 | Модель пишет код лучше против типизированного API, чем против меню tools; скрипт ломается, когда tool отдаёт прозу или недокументированный формат (Cloudflare, nodeflare pitfalls) | NameError 9 + ValueError 6 + KeyError/AttributeError — модель угадывает форму ответа | `search_tools` отдаёт форму **результата** (outputSchema или снятый пример ключей из последнего успешного вызова), не только входные аргументы | сделано (4b5a1cc) |
| 4 | Code mode убрал rate-limit-падения: 100% успеха против 56%, input 31.8k против 93.6k ([zenml/Arcade](https://www.zenml.io/llmops-database/reducing-agent-costs-and-eliminating-rate-limit-failures-with-sandboxed-code-execution)) | Выигрыш приходит через параллельный fan-out (`Promise.all`) | Проверить, что `asyncio.gather` по proxy работает и HINT предлагает его на ≥3 последовательных `await` одного tool в цикле | блокер: `_RpcBridge` (subprocess_worker.py) держит один запрос в полёте, gather идёт последовательно |
| 5 | PTC на свежих моделях +10pp к JSON tool calling, на старых/малых — до −26.9pp (экранированные `\n` ломают синтаксис). Под context rot код держится стабильнее ([Bitter Lesson, arXiv 2608.06370](https://arxiv.org/html/2608.06370)) | Субагенты на haiku зовут code-runner. `syntax_share` 0%, но ошибки по модели не размечены | Писать модель/агента в `execute_code`-событие; сравнить fail_rate haiku vs sonnet/opus. Если у haiku выше — routine-executor на direct-вызовы | частично: caller в метриках (f56517f), модели нет |
| 6 | «search + execute на каждом MCP-сервере — плохой дефолт: клиент уже умеет tool search и code mode» (@mxstbr, 447♥) | В Claude Code есть deferred tools + ToolSearch. Ценность code-runner — агрегация и workspace, не discovery | Не развивать discovery-слой дальше `search_tools(level="servers")`. Мерить успех экономией bytes, а не числом найденных tools | отклонено |
| 7 | Anthropic: «не включайте code execution везде по бенчмарку — замерьте billed input tokens с ним и без на своём трафике» | Receipt считает bytes, но A/B на реальных задачах не делали | Разовый A/B: 10 типовых задач direct vs sandbox, считать токены и wall time | сделано (f5cd0a1) |
| 8 | sPTC: запускать tool-вызовы из частично сгенерированного кода, пока модель ещё пишет ([alexzhang13](https://alexzhang13.github.io/blog/2026/spec-ptc/)) | Выигрыш 1–1.2x по wall time, нужен стриминг кода | Не делать: MCP-клиент отдаёт `code` целиком, стрима нет | отклонено |
| 9 | `allowed_callers` — подсказка модели, не граница безопасности; RCE через `exec()` с полными builtins (AWorld #924) | У нас уже subprocess-изоляция, side-effect gate, taint | Действий нет. Подтверждение текущей модели угроз | покрыто |

## Приоритет

1. **#3 форма результата в `search_tools`** — бьёт по NameError/ValueError/KeyError (~17 из 39 падений).
2. **#2 вердикт по серверу** — убирает 1-вызовные блоки без экономии (57 из 177).
3. **#1 HINT про SQL-агрегацию** — частный случай #2 для mssql/postgres.
4. **#5 модель в метриках** — дешёвая разметка, решает вопрос про haiku.
5. **BlockedImport 11** — разобрать, какие модули импортируют; частые добавить в no-op import или HINT.

Не делать: #8 (sPTC), расширение discovery (#6).

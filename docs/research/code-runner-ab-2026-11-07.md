# A/B: direct vs sandbox на 10 типовых задачах (SHA-811, 2026-11-07)

Скрипт: `scripts/ab_direct_vs_sandbox.py`. Каждый запуск — штатный `claude -p --model sonnet`,
`--tools ""` (встроенные тулы выключены), `--strict-mcp-config`, cwd `~/projects/data_lab`.
direct — в сессии только сервер задачи; sandbox — только code-runner. По 1 прогону на пару.

## Результат

| Задача | Сервер | Вердикт servers | Режим | Ходов | Input всего | Output | $ | Wall, с |
|---|---|---|---|---|---|---|---|---|
| forgetful-search | forgetful | sandbox | direct | 2 | 62 889 | 604 | 0.174 | 17.2 |
| | | | sandbox | 3 | 73 482 | 429 | **0.085** | 18.1 |
| forgetful-recent | forgetful | sandbox | direct | 2 | 60 208 | 247 | 0.157 | 14.4 |
| | | | sandbox | 4 | 104 015 | 709 | **0.127** | 22.1 |
| wigolo-search | wigolo | sandbox | direct | 2 | 70 295 | 317 | 0.156 | 22.9 |
| | | | sandbox | 4 | 77 135 | 602 | **0.094** | 18.1 |
| wigolo-fetch | wigolo | sandbox | direct | 2 | 70 987 | 374 | **0.099** | 24.8 |
| | | | sandbox | 6 | 158 687 | 1 381 | 0.113 | 30.1 |
| exa-search | exa | sandbox | direct | 2 | 43 039 | 373 | 0.103 | 16.3 |
| | | | sandbox | 2 | 52 271 | 418 | **0.096** | 14.5 |
| gitea-repos | gitea-mcp | direct | direct | 2 | 71 533 | 655 | 0.172 | 20.2 |
| | | | sandbox | 3 | 82 764 | 510 | **0.105** | 15.9 |
| codegraph-callers | codegraph | sandbox | direct | 3 | 93 199 | 653 | 0.166 | 20.3 |
| | | | sandbox | 9 | 275 425 | 2 035 | **0.153** | 53.1 |
| twitter-top | twitter | direct | direct | 2 | 56 061 | 923 | 0.142 | 24.7 |
| | | | sandbox | 4 | 108 238 | 748 | **0.104** | 29.3 |
| mssql-dbs | mssql | direct-sql | direct | — | — | — | — | — |
| | | | sandbox | 3 | 81 580 | 335 | 0.096 | 18.9 |
| mssql-top-tables | mssql | direct-sql | direct | — | — | — | — | — |
| | | | sandbox | 6 | 156 734 | 1 388 | 0.112 | 47.3 |

Input всего = input + cache_write + cache_read. Ответы обоих режимов совпали по сути во всех 8 валидных парах.

**mssql direct не замерен:** глобальное `permissions.ask` на `mcp__mssql__execute_sql` в `~/.claude/settings.json`
перекрывает `--allowedTools`, в `-p` вызов отклонён. Через `execute_code` тот же SELECT прошёл без `ask`.

Итог по 8 валидным парам: sandbox дешевле на 25% ($0.876 против $1.169), медленнее на 25% (201 с против 161 с),
input-токенов больше в 8 из 8 (больше ходов, рост за счёт дешёвого cache_read).

## Сверка с вердиктом `get_metrics(servers=True)`

| Сервер | Вердикт | A/B | Совпадает |
|---|---|---|---|
| forgetful | sandbox | sandbox дешевле на 51% и 19% | да |
| wigolo | sandbox | search −40%, fetch +15% | частично: одиночный fetch выгоднее direct |
| exa | sandbox | −7% | да, выигрыш на уровне шума |
| codegraph | sandbox | −8%, в 2.6 раза медленнее (9 ходов) | нет по wall time |
| gitea-mcp | direct | sandbox дешевле на 39% | нет |
| twitter | direct | sandbox дешевле на 27% | нет |
| mssql | direct-sql | не замерен | — |

## Ограничения

1. **Схемы тулов.** С `--tools ""` нет ToolSearch, поэтому direct-сессия грузит все схемы сервера в system
   prompt (gitea-mcp, twitter — десятки тулов). В рабочей сессии эти тулы deferred. Выигрыш sandbox
   на gitea-mcp и twitter объясняется этим, а не размером ответа. На рабочий трафик он не переносится.
2. n = 1 на пару. 4 параллельных запуска делят prompt cache, разброс cache_write между прогонами не оценён.
3. Хуки сессии срабатывают в обоих режимах и одинаково поднимают абсолютные числа.

## Вывод

| # | Идея | Основание | Статус |
|---|---|---|---|
| 1 | Порог `SANDBOX_MEDIAN_BYTES = 5000` не менять | forgetful, wigolo-search, exa: sandbox дешевле | принято |
| 2 | Одиночный `wigolo.fetch` и codegraph звать напрямую | sandbox +4–6 ходов, экономии нет или +15% | предложено |
| 3 | Повтор для direct-серверов с ToolSearch и разрешённым mssql | ограничение 1, mssql не замерен | предложено |

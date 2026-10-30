# code-runner: arXiv 2023-2026, что взять в skills/сервер (2026-10-21)

Метод: firecrawl_research_search_papers, 11 запросов. Отброшено как уже реализованное: CodeAct 2402.01030, TaskWeaver 2311.17541, HyperTool 2606.13663 (код-вызов MCP-тулов), MCP Workflow Engine 2605.00827 (skills), Cartograph 2609.30293 (search_tools + stubs), LATM 2305.17126 / CREATOR 2305.14318 (skills + save_skill), Self-Debug 2304.05128 (HINT + repair на стороне модели).
Не проверял по коду: ищет ли search_tools по skills; есть ли у child сеть. Отмечено "проверить".

| # | arXiv | Название | Механизм | У нас нет / как сделать | Статус |
|---|---|---|---|---|---|
| 1 | 2305.16291 + 2309.17428 | Voyager; CRAFT | Скилл попадает в библиотеку только после self-verification / валидации и dedup | save_skill не прогоняет скилл. Фича: `save_skill(..., example="skills.x.f(...)")` выполняет пример в subprocess, при падении отказ; сохраняет example в SKILL.md. | да: save_skill(example=), dedup нет |
| 2 | 2401.12869 + 2610.09832 | TroVE; SkillForge (lifecycle) | Периодическая обрезка тулбокса; состояния trial/active/stable/retired по fitness | Есть TRIAL/VERIFIED, нет retired и GC. Skill `skill_gc`: по metrics.jsonl найти скиллы с 0 вызовов за 30 дней или success<40%, переместить в `_archive/`. | частично: SHA-484 кандидаты в `get_metrics(skills=True)`, автоархива нет |
| 3 | 2510.07768 | ToolLibGen | Кластеризация тулов + рефакторинг в агрегированные с ревью-агентом | Нет поиска дублей. Skill `skill_dupes`: сравнить сигнатуры+docstring (difflib/Jaccard), вернуть пары-кандидаты на слияние; слияние делает LLM вручную. | нет |
| 4 | 2601.03509 + 2610.11781 | Programmatic Skill Network; Skill-V | Версии скиллов, rollback при регрессии, replay исторических вызовов как non-regression | save_skill перезаписывает без истории. Фича: `.versions/<n>/`, replay последних 5 успешных вызовов из calls.jsonl после обновления, при провале авто-откат. | нет |
| 5 | 2409.07429 | Agent Workflow Memory | Индукция воркфлоу из успешных траекторий на лету | Скиллы только вручную. Фича: executor хранит AST-отпечаток успешных runs (литералы -> плейсхолдеры); 3-й повтор -> HINT "паттерн повторился 3x, save_skill?" с готовым черновиком. Без LLM. | нет |
| 6 | 2609.26121 | DTOC | Обратимая компрессия: полный вывод во внешней памяти, в контексте плейсхолдер + restore | Наш truncate необратим. Фича: при усечении писать полный вывод в workspace `out_<n>.txt`, в хвост добавить "full: open('out_3.txt')". Ablation DTOC: без reversibility качество падает. | сделано: SHA-482 `out_<id>.txt` + `restore()` |
| 7 | 2608.23992 | SCOUT (PayPal) | BM25 + dense, слияние RRF, 2 мета-тула; 140k -> 1.3k токенов | search_tools: все ключевые слова должны совпасть (AND), нет ранжирования. Фича: BM25 (stdlib) с ранжированием и fallback на частичное совпадение; dense+RRF вторым шагом. | сделано: BM25, dense/RRF нет |
| 8 | 2506.01056 | MCP-Zero | Агент формулирует запрос на недостающую capability; иерархический роутинг server -> tool | Нет двухступенчатой выдачи. Фича: `search_tools(level="servers")` сначала список серверов с 1 строкой, затем тулы выбранного. Проверить, ищет ли по skills. | нет (есть `detail=name/desc`) |
| 9 | 2610.10062 | Loud Failures, Quiet Failures | Агент замечает явную ошибку в 91%, правдоподобно-неверный результат в 59%; prompt-напоминание не помогает | HINT ловят только исключения. Фича (детерминированная): предупреждение в receipt: "0 rows", "auto-LIMIT 500 сработал, данные неполные", "вывод усечён", "все None". | сделано: `[cr] ⚠ check before trusting` |
| 10 | 2509.25238 | PALADIN | Банк эксемпляров сбоев, retrieval по похожему -> recovery-действие | HINT статичны. Фича: `hints.jsonl`: пара (нормализованная ошибка, run N падает -> run N+1 того же session проходит, diff) и показ при повторе ошибки. | сделано: SHA-483 `PastFixIndex` |
| 11 | 2605.26298 | Sandlock | Landlock + seccomp без root, ~5 мс, reversible FS | Есть rlimit+AST+scrubbed env. Фича: в child Landlock (FS только workspace+read roots) и seccomp deny socket. Проверить, есть ли сеть у child. | нет |
| 12 | 2609.38224 | Separation of Duties | Approval = одноразовый токен, привязанный к байтам исполняемого | `allow_side_effects` задаёт тот же LLM. Фича: PermissionError печатает хэш кода + список server.tool; повтор разрешён только с `approved_hash`. | нет |

Контекст: обзор 2607.10113 (survey dynamic skills): "flat retrieval деградирует при росте библиотеки", admission и repair повторяются как важные. 2609.22114: сжатие контента даёт ~2% на ход; надёжно положителен только фильтр tool-схем (у нас уже есть через search_tools).

## Топ-5 по польза/стоимость

| Ранг | Идея | Источник | Часы | Статус |
|---|---|---|---|---|
| 1 | Предупреждения о тихих сбоях в receipt (0 rows, LIMIT hit, truncated, all-None) | #9 | 4 | сделано |
| 2 | Обратимое усечение вывода: полный текст в workspace + путь в хвосте | #6 | 3 | сделано: SHA-482 |
| 3 | BM25-ранжирование в search_tools + частичное совпадение | #7 | 4 (+6 на dense/RRF) | сделано: BM25, dense/RRF нет |
| 4 | Admission: `save_skill(example=)` + версии и auto-rollback по replay | #1, #4 | 8 | частично: example= есть, версий и replay нет |
| 5 | Автодетект повторяющегося кода -> HINT "save_skill?" | #5 | 8 | нет |

Дальше по очереди: банк fix-эксемпляров (#10) 10 ч; skill_gc (#2) + skill_dupes (#3) 5 ч вместе; Landlock/seccomp (#11) 12 ч; hash-approval (#12) 5 ч.

# CodeAct / code-as-action для code-runner (обзор arXiv, 2026-10-23)

Источники: wigolo search (`category=papers`, `arxiv.org`) + wigolo fetch abs-страниц (`http_status=200`, `canonical_url=https://arxiv.org/abs/<id>`) + exa fetch HTML CodeAct/ToolGen. Числа — только из abstract/HTML статьи; иначе «числа нет». Код code-runner не менялся.

Проверено на abs: 2402.01030, 2608.06370, 2602.15945, 2506.01056, 2604.21816, 2508.21433, 2609.26121, 2410.03439, 2402.04253, 2305.15334, 2307.16789, 2305.16291, 2401.12869, 2309.17428, 2507.06229, 2304.05128, 2503.18813, 2510.15994, 2607.04576, 2505.22852.

---

## 1. Статьи

| arXiv | Название | Год | Ключевой механизм | Число-эффект |
|---|---|---|---|---|
| [2402.01030](https://arxiv.org/abs/2402.01030) | Executable Code Actions Elicit Better LLM Agents (CodeAct) | 2024 | Python как единое action space: control/data flow, reuse переменных, self-debug по traceback | до **+20%** success vs JSON/text; до **−30%** действий; GPT-4-1106: **+20.7 pp**, **−2.1** turn (HTML) |
| [2608.06370](https://arxiv.org/abs/2608.06370) | The Bitter Lesson of Tool Calling | 2026 | Programmatic tool calling: typed Python stubs, цепочки/параллель в одном ходе | PTC ≥ JSON у **11/14** моделей на BFCL v4; GPT-5.6 **+10.6%**; JSON деградирует **−2.3%** при context rot |
| [2602.15945](https://arxiv.org/abs/2602.15945) | From Tool Orchestration to Code Execution | 2026 | CE-MCP: контекст-развязанный sandbox, данные в runtime, наружу только итог | токенов/latency/turns меньше (HTML); точной % в тексте нет; **16** классов атак (MAESTRO) |
| [2506.01056](https://arxiv.org/abs/2506.01056) | MCP-Zero: Active Tool Discovery | 2025 | Агент сам запрашивает tool, hierarchical routing, без dump всех схем | **98%** меньше токенов на APIBank; 308 серверов / 2797 tools / 248.1k tokens каталога |
| [2604.21816](https://arxiv.org/abs/2604.21816) | Tool Attention Is All You Need | 2026 | ISO-гейтинг + lazy schema: summary pool, full JSON только top-k | симуляция 120 tools: **−95.0%** tool-токенов (**47.3k → 2.4k**); e2e success — проекция, не live |
| [2508.21433](https://arxiv.org/abs/2508.21433) | The Complexity Trap | 2025 | Observation masking старых tool-output vs LLM-summarization | masking **вдвое дешевле** raw, solve rate ≈ summarization; hybrid ещё **−7% / −11%** cost |
| [2609.26121](https://arxiv.org/abs/2609.26121) | DTOC: Dynamic Tool Output Compression | 2026 | Полный output во внешней памяти, в контексте placeholder, обратимое restore | Sonnet 4.6 / GPT-5.4: **−10.3 / −12.7%** input tokens, **−2.4 / −32.3%** steps, solve **×2.5 / ×1.5**, cost/solved **×3 / ×3.5** ниже; irreversible compression портит качество |
| [2410.03439](https://arxiv.org/abs/2410.03439) | ToolGen | 2024 | Tool = токен в vocab, retrieval = generation, без схем в промпте | **>47k** tools; in-domain NDCG@1 **89.17** vs BM25 **29.46** (HTML) |
| [2402.04253](https://arxiv.org/abs/2402.04253) | AnyTool | 2024 | Иерархический API-retriever + solver + self-reflection | **+35.4%** avg pass rate vs ToolLLM на ToolBench; 16k+ RapidAPI |
| [2305.15334](https://arxiv.org/abs/2305.15334) | Gorilla | 2023 | Retriever поверх актуальных docs + finetune API-calls | «превосходит GPT-4» на APIBench; **числа нет** в abstract |
| [2307.16789](https://arxiv.org/abs/2307.16789) | ToolLLM | 2023 | 16k API + neural retriever + DFS decision tree | **16 464** API, 49 категорий; «comparable to ChatGPT»; % нет |
| [2305.16291](https://arxiv.org/abs/2305.16291) | Voyager | 2023 | Растущая skill library исполняемого кода + retrieval + self-verify | **3.3×** unique items, **2.3×** дистанция, milestones до **15.3×** быстрее SOTA |
| [2401.12869](https://arxiv.org/abs/2401.12869) | TroVE | 2024 | Индукция toolbox: grow + periodic trim, без обучения | toolbox **на 79–98% меньше**; human verify **+31%** быстрее и **+13%** точнее |
| [2309.17428](https://arxiv.org/abs/2309.17428) | CRAFT | 2023 | Создать task-specific code-tools, retrieve, execute; без finetune | «substantial improvements»; **числа нет** в abstract |
| [2507.06229](https://arxiv.org/abs/2507.06229) | Agent KB | 2025 | Shared experience KB: plan-seed + diagnostic feedback + disagreement gate | smolagents pass@3 **55.2% → 73.9% (+18.7 pp)**; OpenHands SWE **24.3% → 28.3% (+4.0 pp)** |
| [2304.05128](https://arxiv.org/abs/2304.05128) | Teaching LLMs to Self-Debug | 2023 | Rubber-duck debug по execution results + explanation | Spider **+2–3%**, hardest **+9%**; TransCoder/MBPP до **+12%**; sample efficiency **>10×** |
| [2503.18813](https://arxiv.org/abs/2503.18813) | Defeating Prompt Injections by Design (CaMeL) | 2025 | Control/data flow из trusted query; capabilities на tool-calls; dual-LLM | AgentDojo: **77%** задач с provable security vs **84%** без защиты |
| [2505.22852](https://arxiv.org/abs/2505.22852) | Operationalizing CaMeL | 2025 | Dual-LLM дорог; нужен screening входа, audit выхода, tiered-risk | **числа нет** (position/engineering note) |
| [2510.15994](https://arxiv.org/abs/2510.15994) | MCP Security Bench (MSB) | 2025 | 12 атак на MCP pipeline (в т.ч. injection в описаниях tools и в ответах) | 9 агентов, 405 tools, **2000** атак; сильные модели уязвимее; NRP — метрика; % ASR в abstract нет |
| [2607.04576](https://arxiv.org/abs/2607.04576) | Progressive Disclosure for LLM-Maintained Wikis | 2026 | Catalog-first vs прямой path; preregistered ablation | способный агент **не грузил** большой индекс; quality не доказан при force-catalog; cost падал во всех условиях |

---

## 2. Предложения для code-runner

Уже покрыто (не предлагать как новое): `execute_code` как единственный tool + MCP-прокси; persist `session_id`; AST + SAFE_BUILTINS; subprocess/rlimit, credentials не в child; SIGALRM; truncation + `out_<block>.txt`; auto-LIMIT; workspace `open()` + deny-list; `search_tools` BM25 + stubs; `list_available_tools`; skills/`save_skill`/`replaces:` HINT; side-effect gate; квитанция raw→context; тихие сбои (0 rows / LIMIT / all-None); JSONL metrics + classify_error; error HINTs; no-op import; `calls.jsonl`; per-call timeout downstream.

| # | Идея | Статья | Что даёт | Трудоёмкость | Приоритет | Статус у нас | Статус |
|---|---|---|---|---|---|---|---|
| 1 | Session-level **observation masking**: в контексте агента держать raw только последних K tool-output; старше — одна строка «masked, see workspace» | 2508.21433 | токены: ~½ vs raw, качество ≈ LLM-summary | 1–2 дня | **P0** | per-call truncation есть; masking истории сессии нет | нет |
| 2 | **Обратимое сжатие** DTOC: полный дамп в workspace, в ответе placeholder + `restore(id)` | 2609.26121 | −10–13% input tokens, до −32% steps на сильных моделях; без irreversibility | 2–3 дня | **P0** | `out_*.txt` уже пишет полный вывод; нет placeholder/restore API | сделано: SHA-482 |
| 3 | **Active discovery по умолчанию**: namespace без полного dump stubs; агент зовёт `search_tools` / `request_tool` | 2506.01056 | −98% tool-схем на APIBank | 2–4 дня | **P0** | `search_tools` есть, но часто льют `list_available_tools` целиком | нет |
| 4 | **Lazy schema**: 1-line summary в пуле, полный JSON/stub только top-k | 2604.21816 | −95% tool-токенов в их симуляции (47.3k→2.4k); e2e — проекция | 1–2 дня | **P0** | stubs отдаются целиком | частично: `search_tools(detail=name/desc)` |
| 5 | **Taint/capability** на результаты tools: tainted SQL/HTTP не может быть аргументом write-tool без `allow_side_effects` | 2503.18813 | 77% задач с provable security (плата: −7 pp utility) | 1–2 недели | **P0** | side-effect gate бинарный; taint потока данных нет | сделано: SHA-481 |
| 6 | Санитайз **error/traceback** как untrusted (exception-mediated injection) | 2602.15945 | закрывает класс атак CE-MCP; токенов мало, безопасность | 1–2 дня | **P0** | `MAX_ERROR_BYTES` режет размер, не taint | сделано: SHA-480 |
| 7 | **Grow+trim skills**: после `save_skill` — periodic prune по usage/fail-rate | 2401.12869, 2305.16291 | TroVE: toolbox −79–98%; Voyager: 3.3× items | 2–3 дня | **P1** | `save_skill` есть, trim нет | частично: SHA-484 кандидаты, автоархива нет |
| 8 | **Error-experience retrieve** в HINT: из JSONL metrics похожий traceback → фикс | 2507.06229, 2304.05128 | Agent KB +18.7 pp pass@3; Self-Debug +2–12% | 3–5 дней | **P1** | classify_error + HINT есть; retrieval чужих траекторий нет | сделано: SHA-483 `PastFixIndex` |
| 9 | **Иерархический BM25/embedding** (server → tool → param), не плоский список | 2402.04253, 2305.15334 | AnyTool +35.4% vs ToolLLM | 2–3 дня | **P1** | плоский BM25 | нет |
| 10 | Hybrid masking+лёгкий summary только длинных таблиц | 2508.21433 | ещё −7% vs masking, −11% vs LLM-summary | 1 день после #1 | **P1** | нет | нет |
| 11 | Скан **описаний tools** на injection (MSB: name-collision, preference, prompt-in-schema) | 2510.15994 | качество/безопасность каталога; 12 типов атак | 2–3 дня | **P1** | схемы проксируются as-is | нет |
| 12 | `asyncio.gather` helper + явный parallel fan-out в stub-доке | 2608.06370 | PTC стабилен при context rot (−2.3% у JSON) | 0.5–1 день | **P2** | await уже есть | нет |
| 13 | Не внедрять ToolGen-токены в модель | 2410.03439 | для MCP-сервера слой не тот; NDCG 89 vs 29 — ценой finetune 47k tokens | 0 | **P2 / не делать** | n/a | не делать |
| 14 | Tiered-risk вместо полного dual-LLM CaMeL | 2505.22852 | dual-LLM дорог; screening+audit дешевле | 3–5 дней | **P2** | нет | нет |
| 15 | Не форсировать catalog-first progressive disclosure | 2607.04576 | способный агент и так не грузит индекс; force-catalog не доказал quality | 0 | **не делать** | search_tools уже opt-in | не делать |

---

## 3. Чего НЕ делать

- **Не заменять truncation LLM-саммаризацией наблюдений.** 2508.21433: masking дешевле вдвое и не хуже summarization; summarization — complexity trap.
- **Не сжимать tool-output необратимо.** 2609.26121: disable-only compression деградирует; нужен restore.
- **Не dumpить все схемы каждый ход.** 2506.01056 / 2604.21816: MCP tax 10–60k токенов/ход в практике; active request + lazy load.
- **Не форсировать catalog-first «progressive disclosure».**** 2607.04576: агент ходит напрямую к файлу; экономия vs большого индекса для сильного агента не существует; quality при force-catalog не доказан.
- **Не finetune-ить tool-токены в LLM (ToolGen) как фичу сервера.** 2410.03439 решает retrieval в весах модели — не в MCP middleware.
- **Не ставить полный dual-LLM CaMeL по умолчанию.** 2503.18813: −7 pp utility (84→77); 2505.22852: dual-LLM дорог, trusted-prompt assumption ломается.
- **Не ждать, что code-mode выиграет на итеративном тексте.** 2602.15945: Wikipedia/Reddit лучше multi-turn MCP; CE-MCP — для SQL/файлов/детерминированных пайплайнов (это как раз наш профиль).
- **Не растить skill library без trim.** 2401.12869: без обрезки toolbox раздувается; выигрыш — в маленьком проверенном наборе.
- **Не доверять старым pass-rate протоколам tool-агентов.** 2402.04253: протокол ToolLLM завышал pass rate.
- **Не считать симуляцию Tool Attention live-замером.** 2604.21816 сами помечают e2e как projection.

---

## Топ-5 для оркестратора

1. Observation masking истории tool-output (K последних raw) — 2508.21433 — ~½ cost при том же solve rate — P0
2. Active tool discovery (stubs не в прологе, только по `search_tools`/`request_tool`) — 2506.01056 — −98% схемных токенов — P0
3. Обратимый DTOC: placeholder в контексте + restore из workspace — 2609.26121 — −10–13% input tokens, до −32% steps — P0
4. Taint на tool-output + санитайз traceback (не давать tainted данным вызывать write) — 2503.18813 / 2602.15945 — 77% задач с provable security; закрытие exception-injection — P0
5. Grow+trim skills + retrieval прошлых error→fix в HINT — 2401.12869 / 2507.06229 — toolbox −79–98%; +18.7 pp pass@3 — P1

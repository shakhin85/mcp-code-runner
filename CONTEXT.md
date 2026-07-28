# CONTEXT

## Current Task
Research-находки внедрены и проверены на живом GPU (RTX 3060). Отчёт — Linear SHA-42 (судья дедупликации) и SHA-43 (остальное).

## Key Decisions
- Дедуп-судья допускается по **асимметрии ошибок** (`over_aggressive_rate ≤ 5%`), не по accuracy: при base rate 6.5% «всегда related» даёт 93.5%. Замер на 93 парах: **gemma3:12b PASS** (93.5%/2.3%) — судья по умолчанию; qwen3:14b FAIL (86.0%/13.8%); mistral-nemo упал на контроле. Размер модели не решает: 12B калибрована лучше 14B, а 27–32B не влезает в 12 ГБ VRAM.
- Гейт **запрещающий**: провал A/B снимает `confirmed`-штампы своего прогона (точечно). Деструктивный `consolidate.py --apply` остаётся интерактивным.
- PPR поверх code-графа отклонён по замеру: лексика уже даёт rank=1 (182 узла), PPR 0w/3t/2l. Кандидат — memory-граф, не код.

## Next Steps
- Перегнать ночь на gemma3:12b (ожидание: мало `confirmed`); замерить связку «qwen3 recall-фильтр → gemma3 решающий».
- Graphiti: `poc.py ingest -n 30` на bge-m3 + temporal-вопросы (FalkorDB поднят, пуст).
- Решение по webhook: `~/.claude/tools/gitea-review-hook/BLOCKED.md`; параметризовать топ-3 драфта из `~/.claude/tools/skill-distiller/drafts/`. Задачи: `bd list` (prefix `sha`).

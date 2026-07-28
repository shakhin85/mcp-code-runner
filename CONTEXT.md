# CONTEXT

## Current Task
Внедрение research-находок в харнесс (autonomous agents / persistent memory / graph+loop / GPU-offload): 13/13 задач закрыты, две ждут действий владельца — включить GPU-бокс WS-100 и выбрать вариант по webhook-ревью.

## Key Decisions
- Дедуп-судья допускается по **асимметрии ошибок** (`over_aggressive_rate ≤ 5%`), не по accuracy: при base rate 6.5% «всегда related» даёт 93.5%, а 5 ложных депрекейтов необратимы. Гейт — `~/.claude/tools/memory-consolidate/ab_judge.py`, вшит шагом в ночную цепочку.
- Ночь готовит вердикты и `confirmed`-штампы, **деструктивный `consolidate.py --apply` остаётся интерактивным**.
- PPR поверх code-графа отклонён по замеру: лексика уже даёт rank=1 (182 узла), PPR 0w/3t/2l. Кандидат применения — memory-граф, не код.

## Next Steps
- Включить WS-100 (10.1.1.126) → ночная цепочка сама прогонит судью + A/B; затем `bench_endpoints.py` для SGLang. Задачи и команды: `bd list` (prefix `sha`, 13 issues).
- Решение по event-driven auto-review: `~/.claude/tools/gitea-review-hook/BLOCKED.md` (рекомендация — остаться на ручном `/review`).
- Ветка `feat/metrics-code-fingerprint` (`de99f45`, `code_sha`/`code_lines` в metrics-событии, 268 тестов) ждёт ревью и мержа в master.

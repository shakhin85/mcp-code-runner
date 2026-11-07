"""SHA-811: A/B замер direct vs sandbox (code-runner) на типовых задачах.

Каждая задача гоняется штатным `claude -p` дважды:
  direct  — в сессии только MCP-сервер задачи;
  sandbox — в сессии только code-runner (сервер задачи доступен как прокси).
Из JSON-результата берутся usage, total_cost_usd, duration_ms, num_turns.

Запуск: uv run --no-project python scripts/ab_direct_vs_sandbox.py OUT.jsonl [task_id ...]
Повторный запуск пропускает пары (task, mode), уже записанные в OUT.jsonl.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CWD = Path.home() / "projects/data_lab"  # здесь есть mssql и gitea-mcp
GLOBAL_CFG = Path.home() / ".claude/.config.json"
PROJECT_CFG = CWD / ".mcp.json"
MODEL = "sonnet"
BUDGET_USD = "1"
TIMEOUT_S = 600

TASKS = [
    ("forgetful-search", "forgetful",
     "Найди в Forgetful 5 самых релевантных воспоминаний про таймаут SIGALRM в code-runner."
     " Ответ: id и title, по строке."),
    ("forgetful-recent", "forgetful",
     "Возьми 20 последних воспоминаний Forgetful."
     " Ответ: сколько из них несут тег self-improvement, и их id."),
    ("wigolo-search", "wigolo",
     "Найди в вебе 3 источника про Anthropic programmatic tool calling."
     " Ответ: title и URL, по строке."),
    ("wigolo-fetch", "wigolo",
     "Загрузи https://www.anthropic.com/engineering/code-execution-with-mcp "
     "и перечисли заголовки разделов статьи."),
    ("exa-search", "exa",
     "Найди через Exa 5 статей про экономию токенов при code execution с MCP."
     " Ответ: title и URL, по строке."),
    ("mssql-dbs", "mssql",
     "Сколько пользовательских баз (database_id > 4) на сервере MSSQL? Ответ: число "
     "и список имён."),
    ("mssql-top-tables", "mssql",
     "В первой по алфавиту пользовательской базе MSSQL найди 10 таблиц с наибольшим числом строк."
     " Ответ: схема.таблица и число строк."),
    ("gitea-repos", "gitea-mcp",
     ""
     "Сколько репозиториев доступно текущему пользователю Gitea? Назови 3 последних обновлённых."),
    ("codegraph-callers", "codegraph",
     "В проекте /home/linux_admin/updated_code_runner найди, где определена summarize_servers"
     " и какие функции её вызывают."
     " Ответ: файл:строка и список вызывающих."),
    ("twitter-top", "twitter",
     "Возьми последние 20 твитов @AnthropicAI."
     " Ответ: 3 твита с наибольшим числом лайков — дата, лайки, первые 80 символов."),
]

APPEND = {
    "direct": "Режим замера: вызывай MCP-инструменты напрямую. Ответь коротко, только результат.",
    "sandbox": ("Режим замера: все обращения к данным делай через mcp__code-runner__execute_code, "
                "сервер задачи там доступен как Python-прокси. Печатай из кода только итог. "
                "Ответь коротко, только результат."),
}


def servers() -> dict:
    merged = json.loads(GLOBAL_CFG.read_text())["mcpServers"]
    merged.update(json.loads(PROJECT_CFG.read_text())["mcpServers"])
    return merged


def run(task, mode, all_servers, tmpdir) -> dict:
    tid, server, prompt = task
    name = "code-runner" if mode == "sandbox" else server
    cfg = Path(tmpdir) / f"{tid}-{mode}.json"
    cfg.write_text(json.dumps({"mcpServers": {name: all_servers[name]}}))
    cfg.chmod(0o600)
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", MODEL,
           "--strict-mcp-config", "--mcp-config", str(cfg), "--tools", "",
           "--allowedTools", f"mcp__{name}", "--max-budget-usd", BUDGET_USD,
           "--append-system-prompt", APPEND[mode]]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=CWD, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        p = None
    try:
        out = json.loads(p.stdout) if p else {"is_error": True, "result": "timeout"}
    except json.JSONDecodeError:
        out = {"is_error": True, "result": (p.stdout + p.stderr)[-500:]}
    u = out.get("usage") or {}
    return {
        "task": tid, "server": server, "mode": mode,
        "wall_s": round(time.monotonic() - t0, 1),
        "duration_ms": out.get("duration_ms"),
        "turns": out.get("num_turns"),
        "input": u.get("input_tokens", 0),
        "cache_write": u.get("cache_creation_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "output": u.get("output_tokens", 0),
        "cost_usd": out.get("total_cost_usd"),
        "is_error": out.get("is_error", True),
        "result": str(out.get("result", ""))[:400],
    }


def main():
    out_path = Path(sys.argv[1])
    only = set(sys.argv[2:])
    done = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            r = json.loads(line)
            done.add((r["task"], r["mode"]))
    jobs = [(t, m) for t in TASKS for m in ("direct", "sandbox")
            if (not only or t[0] in only) and (t[0], m) not in done]
    all_servers = servers()
    with tempfile.TemporaryDirectory() as tmpdir, ThreadPoolExecutor(4) as ex:
        for r in ex.map(lambda j: run(j[0], j[1], all_servers, tmpdir), jobs):
            with out_path.open("a") as f:  # одна строка на пару: падение не теряет готовые
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(r["task"], r["mode"], r["cost_usd"], r["is_error"], flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    main()

"""Тесты scripts/install-daemon.sh: юниты, идемпотентность, dry-run, smoke."""
# ruff: noqa: E501  (эталон юнитов дословный)

import os
import re
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install-daemon.sh"
MCP_URL = "http://127.0.0.1:8030/mcp"
OLD_NS = 1_000_000_000 * 10**9

# Эталон: копия живых юнитов lns-srv-tempdwh (снимок при разработке, см. docs/DAEMON.md).
LIVE = {
    "code-runner-proxy.socket": r"""[Unit]
Description=code-runner MCP socket (client-facing :8030)

[Socket]
ListenStream=127.0.0.1:8030

[Install]
WantedBy=sockets.target
""",
    "code-runner-proxy.service": r"""[Unit]
Description=code-runner MCP proxy :8030 -> backend :8031 (socket-activated, idle-stop)
Requires=code-runner-backend.service
After=code-runner-backend.service
# при простое прокси выходит → backend становится unneeded → тоже гаснет

[Service]
# первое подключение поднимает backend; --exit-idle-time гасит цепочку после простоя
ExecStart=/lib/systemd/systemd-socket-proxyd --exit-idle-time=15min 127.0.0.1:8031
""",
    "code-runner-backend.service": r"""[Unit]
Description=code-runner MCP backend (streamable-http, on-demand)
After=network.target
# гасится автоматически, когда прокси перестаёт его требовать (idle)
StopWhenUnneeded=true

[Service]
Type=simple
EnvironmentFile=%h/.config/code-runner/code-runner.env
ExecStart=%h/.local/bin/uv --directory %h/updated_code_runner run code-runner
# держим unit «стартующим», пока порт реально не поднимется (boot ~8с: коннект к downstream MCP)
ExecStartPost=/bin/bash -c 'until curl -s -o /dev/null http://127.0.0.1:8031/mcp; do sleep 0.5; done'
Restart=on-failure
RestartSec=5
TimeoutStartSec=120
# не глушить мгновенно при разрыве, дать дослать ответ
TimeoutStopSec=15

# НЕТ [Install] — юнит не включается сам, его поднимает proxy по требованию
""",
    "code-runner-backend.service.d/forgetful-ready.conf": r"""# Гонка старта (28.08.2026): code-runner подключает downstream MCP один раз при
# старте и не реконнектится. forgetful-proxy socket-activated с idle-stop —
# если он спал в этот момент, namespace forgetful молча отсутствует всю жизнь
# процесса (симптом: NameError: name 'forgetful' is not defined в execute_code).
# Будим прокси и ждём готовности ДО запуска backend.
[Unit]
Wants=forgetful-proxy.socket
After=forgetful-proxy.socket

[Service]
ExecStartPre=/bin/bash -c 'for i in $(seq 1 30); do curl -s -m 2 -o /dev/null http://127.0.0.1:8020/health && exit 0; sleep 1; done; echo "forgetful не поднялся за 30с — стартуем без него" >&2'
""",
    "code-runner-backend.service.d/limits.conf": r"""# Страховка cgroup от накопления дочерних MCP-серверов (02.08.2026).
# Причина: code_runner.client_pool поднимает stdio-серверы на project root
# лениво, но не гасит по idle -> за 8.6ч набралось 1038 проц. / 47.8 GiB.
# Это НЕ фикс, а аварийный стоп-кран: настоящее лечение — idle TTL/eviction
# в client_pool. Потолок намеренно ВЫШЕ рабочего пика: TasksMax=400 упирался
# при холодном старте пула (Errno 11, серверы не подключались).
[Service]
TasksMax=1200
MemoryHigh=16G
MemoryMax=24G
""",
}
FORGETFUL = "code-runner-backend.service.d/forgetful-ready.conf"


def expected(repo: Path) -> dict[str, str]:
    return {k: v.replace("%h/updated_code_runner", str(repo)) for k, v in LIVE.items()}


def make_stubs(tmp: Path, *, fail: bool) -> Path:
    stubs = tmp / "stubs"
    stubs.mkdir(exist_ok=True)
    for name in ("systemctl", "loginctl", "curl"):
        extra = 'echo "$STUB_LINGER"' if name == "loginctl" and not fail else ""
        body = f'#!/bin/sh\necho "{name} $*" >> "$STUB_LOG"\n{extra}\nexit {int(fail)}\n'
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    if not fail:
        (stubs / "curl").unlink()  # smoke-тесты используют настоящий curl
    return stubs


def run(tmp, *args, script=SCRIPT, cwd=None, forgetful=True, stubs=None, **env):
    sysdir = tmp / "sysunits"
    sysdir.mkdir(exist_ok=True)
    if forgetful:
        (sysdir / "forgetful-proxy.socket").write_text("")
    (tmp / "home").mkdir(exist_ok=True)
    e = {
        **os.environ,
        "HOME": str(tmp / "home"),
        "CODE_RUNNER_ENV_DIR": str(tmp / "envdir"),
        "CODE_RUNNER_UNIT_SEARCH_PATH": str(sysdir),
        "STUB_LOG": str(tmp / "stub.log"),
        "STUB_LINGER": "Linger=no",
    }
    e.pop("CODE_RUNNER_SMOKE_TIMEOUT", None)
    e.pop("CODE_RUNNER_SMOKE_URL", None)
    if stubs:
        e["PATH"] = f"{stubs}:{e['PATH']}"
    e.update({k: str(v) for k, v in env.items()})
    return subprocess.run(
        [str(script), *map(str, args)], capture_output=True, text=True, env=e, cwd=cwd, timeout=60
    )


def files(root: Path) -> dict[str, Path]:
    return {str(p.relative_to(root)): p for p in root.rglob("*") if p.is_file()}


def age(*paths: Path) -> dict[Path, int]:
    for p in paths:
        os.utime(p, ns=(OLD_NS, OLD_NS))
    return {p: p.stat().st_mtime_ns for p in paths}


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/mcp"
    srv.shutdown()
    srv.server_close()


def closed_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}/mcp"


def test_static_bash_shellcheck_mode():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert shutil.which("shellcheck"), "shellcheck не установлен"
    sc = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert (sc.returncode, sc.stdout, sc.stderr) == (0, "", "")
    assert "shellcheck disable" not in SCRIPT.read_text()
    ls = subprocess.run(
        ["git", "ls-files", "-s", "scripts/install-daemon.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert ls.stdout.startswith("100755")


def test_templates_set_and_no_hardcoded_paths():
    tpl = ROOT / "deploy" / "systemd"
    assert sorted(p.name for p in tpl.iterdir()) == sorted(Path(k).name for k in LIVE)
    for p in tpl.iterdir():
        text = p.read_text()
        assert "/home/linux_admin" not in text and "updated_code_runner" not in text


def test_install_matches_reference(tmp_path):
    units = tmp_path / "units"
    r = run(tmp_path, "--unit-dir", units)
    assert r.returncode == 0, r.stderr
    got = files(units)
    exp = expected(ROOT)
    assert set(got) == set(exp) and len(got) == 5
    for name, text in exp.items():
        assert got[name].read_text() == text
        assert not re.search(r"@[A-Z_]+@", text)
    assert "[Install]" not in got["code-runner-backend.service"].read_text().splitlines()
    assert "[Install]" in got["code-runner-proxy.socket"].read_text().splitlines()


def test_second_run_is_noop_and_repairs_only_changed(tmp_path):
    units = tmp_path / "units"
    assert run(tmp_path, "--unit-dir", units).returncode == 0
    env_file = tmp_path / "envdir" / "code-runner.env"
    paths = [*files(units).values(), env_file]
    before = age(*paths)
    r = run(tmp_path, "--unit-dir", units)
    assert r.returncode == 0 and "no changes" in r.stdout
    assert {p: p.stat().st_mtime_ns for p in paths} == before
    assert not list(tmp_path.rglob("*.tmp"))
    victim = units / "code-runner-proxy.service"
    victim.write_text("broken\n")
    before = age(*paths)
    assert run(tmp_path, "--unit-dir", units).returncode == 0
    after = {p: p.stat().st_mtime_ns for p in paths}
    assert [p for p in paths if after[p] != before[p]] == [victim]
    assert victim.read_text() == expected(ROOT)["code-runner-proxy.service"]


def test_dry_run_writes_nothing(tmp_path):
    units = tmp_path / "units"
    r = run(tmp_path, "--dry-run", "--unit-dir", units)
    assert r.returncode == 0 and "would write" in r.stdout
    assert not units.exists() and not (tmp_path / "envdir").exists()
    r = run(tmp_path, "--dry-run", stubs=make_stubs(tmp_path, fail=True))
    assert r.returncode == 0 and "enable --now code-runner-proxy.socket" in r.stdout
    assert not (tmp_path / "home" / ".config").exists()
    assert not (tmp_path / "stub.log").exists()


@pytest.mark.parametrize("forgetful", [True, False])
def test_forgetful_conf_only_with_forgetful_socket(tmp_path, forgetful):
    units = tmp_path / "units"
    r = run(tmp_path, "--unit-dir", units, forgetful=forgetful)
    assert r.returncode == 0
    got = files(units)
    assert (FORGETFUL in got) is forgetful
    assert "code-runner-backend.service.d/limits.conf" in got
    assert ("skip: forgetful-ready.conf" in r.stdout) is (not forgetful)


def test_env_file_created_0600_with_comments(tmp_path):
    assert run(tmp_path, "--unit-dir", tmp_path / "u").returncode == 0
    env_file = tmp_path / "envdir" / "code-runner.env"
    assert env_file.stat().st_mode & 0o777 == 0o600
    lines = [x for x in env_file.read_text().splitlines() if x.strip()]
    assert lines and all(x.startswith("#") for x in lines)


def test_existing_env_untouched_and_not_printed(tmp_path):
    env_dir = tmp_path / "envdir"
    env_dir.mkdir()
    env_file = env_dir / "code-runner.env"
    env_file.write_bytes(b"TOKEN=SENTINEL_9f3a\n")
    before = age(env_file)
    r = run(tmp_path, "--unit-dir", tmp_path / "u")
    assert r.returncode == 0
    assert env_file.read_bytes() == b"TOKEN=SENTINEL_9f3a\n"
    assert env_file.stat().st_mtime_ns == before[env_file]
    assert "SENTINEL_9f3a" not in r.stdout + r.stderr


def test_repo_path_from_bash_source(tmp_path):
    copy = tmp_path / "copy_repo"
    (copy / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, copy / "scripts" / "install-daemon.sh")
    shutil.copytree(ROOT / "deploy", copy / "deploy")
    link = tmp_path / "elsewhere" / "link.sh"
    link.parent.mkdir()
    link.symlink_to(copy / "scripts" / "install-daemon.sh")
    other = tmp_path / "cwd"
    other.mkdir()
    units = tmp_path / "units"
    r = run(tmp_path, "--unit-dir", units, script=link, cwd=other)
    assert r.returncode == 0, r.stderr
    backend = (units / "code-runner-backend.service").read_text()
    assert f"--directory {copy.resolve()} run code-runner" in backend
    assert f"--directory {ROOT} " not in backend


def test_unit_dir_calls_no_system_tools(tmp_path):
    stubs = make_stubs(tmp_path, fail=True)
    r = run(tmp_path, "--unit-dir", tmp_path / "u", stubs=stubs, CODE_RUNNER_SMOKE_TIMEOUT=60)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "stub.log").exists()


def test_linger_warning_and_systemctl_calls(tmp_path, server):
    r = run(
        tmp_path,
        stubs=make_stubs(tmp_path, fail=False),
        CODE_RUNNER_SMOKE_URL=server,
        CODE_RUNNER_SMOKE_TIMEOUT=5,
    )
    assert r.returncode == 0, r.stderr
    assert "loginctl enable-linger" in r.stdout and MCP_URL in r.stdout
    log = (tmp_path / "stub.log").read_text().splitlines()
    assert [x for x in log if x.startswith("systemctl")] == [
        "systemctl --user daemon-reload",
        "systemctl --user enable --now code-runner-proxy.socket",
    ]
    assert not any("enable-linger" in x for x in log)
    assert not (tmp_path / "home" / ".claude.json").exists()
    assert not (tmp_path / "home" / ".claude").exists()


def test_smoke_default_timeout_is_60(tmp_path):
    r = run(tmp_path, "--dry-run")
    assert "timeout 60s" in r.stdout


def test_smoke_fails_when_port_closed(tmp_path):
    r = run(
        tmp_path,
        stubs=make_stubs(tmp_path, fail=False),
        CODE_RUNNER_SMOKE_URL=closed_url(),
        CODE_RUNNER_SMOKE_TIMEOUT=2,
    )
    assert r.returncode != 0 and "smoke: FAIL" in r.stderr


def test_docs_mention_install():
    doc = (ROOT / "docs" / "DAEMON.md").read_text()
    for needle in (
        "scripts/install-daemon.sh",
        "--dry-run",
        "--unit-dir",
        "loginctl enable-linger",
        "code-runner.env",
        MCP_URL,
        "@REPO_DIR@",
    ):
        assert needle in doc

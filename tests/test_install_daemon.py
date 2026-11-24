"""Тесты scripts/install-daemon.sh: юниты, идемпотентность, dry-run, smoke."""
# ruff: noqa: E501  (эталон юнитов дословный)

import os
import re
import shlex
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
MEASURED_CODE = 406  # GET /mcp без Accept: text/event-stream на streamable-http
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


def run(tmp, *args, script=SCRIPT, cwd=None, stubs=None, **env):
    (tmp / "home").mkdir(exist_ok=True)
    e = {
        **os.environ,
        "HOME": str(tmp / "home"),
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


def seed_forgetful(units: Path) -> None:
    units.mkdir(parents=True, exist_ok=True)
    (units / "forgetful-proxy.socket").write_text("")


def files(root: Path) -> dict[str, Path]:
    return {str(p.relative_to(root)): p for p in root.rglob("*") if p.is_file()}


def snapshot(root: Path) -> dict[str, int]:
    return {k: p.stat().st_mtime_ns for k, p in files(root).items()} if root.exists() else {}


def age(*paths: Path) -> dict[Path, int]:
    for p in paths:
        os.utime(p, ns=(OLD_NS, OLD_NS))
    return {p: p.stat().st_mtime_ns for p in paths}


@pytest.fixture
def serve():
    servers = []

    def start(code: int) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(code)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}/mcp"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def closed_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}/mcp"


def systemctl_log(tmp: Path) -> list[str]:
    log = (tmp / "stub.log").read_text().splitlines()
    return [x for x in log if x.startswith("systemctl")]


def test_static_bash_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert "shellcheck disable" not in SCRIPT.read_text()
    ls = subprocess.run(
        ["git", "ls-files", "-s", "scripts/install-daemon.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert ls.stdout.startswith("100755")


def test_f9_shellcheck_clean_or_skip():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck не установлен")
    sc = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert (sc.returncode, sc.stdout, sc.stderr) == (0, "", "")


def test_templates_set_and_no_hardcoded_paths():
    tpl = ROOT / "deploy" / "systemd"
    assert sorted(p.name for p in tpl.iterdir()) == sorted(Path(k).name for k in LIVE)
    for p in tpl.iterdir():
        text = p.read_text()
        assert "/home/linux_admin" not in text and "updated_code_runner" not in text


def test_install_matches_reference(tmp_path):
    units = tmp_path / "units"
    seed_forgetful(units)
    r = run(tmp_path, "--unit-dir", units)
    assert r.returncode == 0, r.stderr
    got = files(units)
    del got["forgetful-proxy.socket"]
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
    env_file = tmp_path / "code-runner" / "code-runner.env"
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
    assert not units.exists() and not (tmp_path / "code-runner").exists()
    r = run(tmp_path, "--dry-run", stubs=make_stubs(tmp_path, fail=True))
    assert r.returncode == 0 and "enable --now code-runner-proxy.socket" in r.stdout
    assert not (tmp_path / "home" / ".config").exists()
    assert not (tmp_path / "stub.log").exists()


def test_f1_env_creation_counts_as_change(tmp_path):
    units = tmp_path / "units"
    assert run(tmp_path, "--unit-dir", units).returncode == 0
    env_file = tmp_path / "code-runner" / "code-runner.env"
    env_file.unlink()
    r = run(tmp_path, "--unit-dir", units)
    assert r.returncode == 0 and env_file.exists()
    assert "created:" in r.stdout and "no changes" not in r.stdout
    dry = run(tmp_path, "--dry-run", "--unit-dir", tmp_path / "u2", CODE_RUNNER_X="1")
    assert "no changes" not in dry.stdout
    env_file.unlink()
    dry = run(tmp_path, "--dry-run", "--unit-dir", units)
    assert "would create" in dry.stdout and "no changes" not in dry.stdout


def test_f3_ports_single_source(tmp_path):
    for p in (ROOT / "deploy" / "systemd").iterdir():
        assert not re.search(r"803[01]", p.read_text()), p.name
    text = SCRIPT.read_text()
    assert text.count("8030") == 1 and text.count("8031") == 1
    assert re.search(r"^CLIENT_PORT=8030$", text, re.M)
    assert re.search(r"^BACKEND_PORT=8031$", text, re.M)
    copy = tmp_path / "copy_repo"
    (copy / "scripts").mkdir(parents=True)
    changed = text.replace("CLIENT_PORT=8030", "CLIENT_PORT=9030").replace(
        "BACKEND_PORT=8031", "BACKEND_PORT=9031"
    )
    (copy / "scripts" / "install-daemon.sh").write_text(changed)
    (copy / "scripts" / "install-daemon.sh").chmod(0o755)
    shutil.copytree(ROOT / "deploy", copy / "deploy")
    units = tmp_path / "units"
    r = run(tmp_path, "--unit-dir", units, script=copy / "scripts" / "install-daemon.sh")
    assert r.returncode == 0, r.stderr
    sock = (units / "code-runner-proxy.socket").read_text()
    svc = (units / "code-runner-proxy.service").read_text()
    backend = (units / "code-runner-backend.service").read_text()
    assert "ListenStream=127.0.0.1:9030" in sock
    assert "127.0.0.1:9031" in svc and "http://127.0.0.1:9031/mcp" in backend
    assert "FASTMCP_PORT=9031" in (tmp_path / "code-runner" / "code-runner.env").read_text()
    assert not re.search(r"803[01]", sock + svc + backend)


def test_f4_env_dir_beside_unit_dir_not_real_home(tmp_path):
    real = tmp_path / "home" / ".config"
    units = tmp_path / "work" / "units"
    r = run(tmp_path, "--unit-dir", units)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "work" / "code-runner" / "code-runner.env").is_file()
    assert not real.exists()
    explicit = tmp_path / "explicit"
    r = run(tmp_path, "--unit-dir", tmp_path / "u3", "--env-dir", explicit)
    assert r.returncode == 0, r.stderr
    assert (explicit / "code-runner.env").is_file()
    assert not (tmp_path / "code-runner").exists() and not real.exists()
    before = snapshot(real)
    assert run(tmp_path, "--unit-dir", units).returncode == 0
    assert snapshot(real) == before == {}


def test_f5_forgetful_socket_in_unit_dir_present(tmp_path):
    units = tmp_path / "units"
    seed_forgetful(units)
    r = run(tmp_path, "--unit-dir", units)
    assert r.returncode == 0
    assert "code-runner-backend.service.d/forgetful-ready.conf" in files(units)
    assert "code-runner-backend.service.d/limits.conf" in files(units)
    assert "skip: forgetful-ready.conf" not in r.stdout


def test_f5_forgetful_socket_in_unit_dir_absent(tmp_path):
    units = tmp_path / "units"
    other = tmp_path / "elsewhere"
    seed_forgetful(other)
    r = run(tmp_path, "--unit-dir", units)
    assert r.returncode == 0
    assert "code-runner-backend.service.d/forgetful-ready.conf" not in files(units)
    assert "skip: forgetful-ready.conf" in r.stdout


def test_f5_default_unit_dir_is_home_config(tmp_path):
    seed_forgetful(tmp_path / "home" / ".config" / "systemd" / "user")
    r = run(tmp_path, "--dry-run")
    assert r.returncode == 0
    assert "forgetful-ready.conf" in r.stdout and "skip: forgetful-ready.conf" not in r.stdout


def test_f6_smoke_timeout_not_a_number(tmp_path):
    r = run(tmp_path, "--dry-run", "--unit-dir", tmp_path / "u", CODE_RUNNER_SMOKE_TIMEOUT="abc")
    assert r.returncode == 2
    assert "CODE_RUNNER_SMOKE_TIMEOUT" in r.stderr
    assert not (tmp_path / "u").exists()
    ok = run(tmp_path, "--dry-run", "--unit-dir", tmp_path / "u", CODE_RUNNER_SMOKE_TIMEOUT=7)
    assert ok.returncode == 0, ok.stderr


def test_f7_proxy_change_restarts_socket_only_then(tmp_path, serve):
    stubs = make_stubs(tmp_path, fail=False)
    url = serve(406)
    kw = {"stubs": stubs, "CODE_RUNNER_SMOKE_URL": url, "CODE_RUNNER_SMOKE_TIMEOUT": 5}
    units = tmp_path / "home" / ".config" / "systemd" / "user"
    r = run(tmp_path, **kw)
    assert r.returncode == 0, r.stderr
    assert systemctl_log(tmp_path) == [
        "systemctl --user daemon-reload",
        "systemctl --user stop code-runner-proxy.service",
        "systemctl --user restart code-runner-proxy.socket",
        "systemctl --user enable --now code-runner-proxy.socket",
    ]
    for name in (
        "code-runner-proxy.socket",
        "code-runner-proxy.service",
        "code-runner-backend.service",
    ):
        assert name in r.stdout.split("изменены юниты:")[1]
    (tmp_path / "stub.log").unlink()
    r = run(tmp_path, **kw)
    assert "no changes" in r.stdout
    assert systemctl_log(tmp_path) == ["systemctl --user enable --now code-runner-proxy.socket"]
    (tmp_path / "stub.log").unlink()
    (units / "code-runner-backend.service.d" / "limits.conf").write_text("x\n")
    run(tmp_path, **kw)
    log = systemctl_log(tmp_path)
    assert "systemctl --user daemon-reload" in log and not any(
        "stop" in x or "restart" in x for x in log
    )
    (tmp_path / "stub.log").unlink()
    (units / "code-runner-proxy.service").write_text("x\n")
    (units / "code-runner-proxy.socket").write_text("x\n")
    r = run(tmp_path, **kw)
    assert "systemctl --user stop code-runner-proxy.service" in systemctl_log(tmp_path)
    assert "systemctl --user restart code-runner-proxy.socket" in systemctl_log(tmp_path)
    tail = r.stdout.split("изменены юниты:")[1].splitlines()[0]
    assert "code-runner-proxy.socket" in tail and "code-runner-proxy.service" in tail


def test_f7_dry_run_plan_names_restart(tmp_path):
    r = run(tmp_path, "--dry-run")
    assert "stop code-runner-proxy.service; restart code-runner-proxy.socket" in r.stdout


def test_existing_env_untouched_and_not_printed(tmp_path):
    env_dir = tmp_path / "code-runner"
    env_dir.mkdir()
    env_file = env_dir / "code-runner.env"
    env_file.write_bytes(b"TOKEN=SENTINEL_9f3a\n")
    before = age(env_file)
    r = run(tmp_path, "--unit-dir", tmp_path / "u")
    assert r.returncode == 0
    assert env_file.read_bytes() == b"TOKEN=SENTINEL_9f3a\n"
    assert env_file.stat().st_mtime_ns == before[env_file]
    assert "SENTINEL_9f3a" not in r.stdout + r.stderr


def test_env_file_created_0600_with_comments(tmp_path):
    assert run(tmp_path, "--unit-dir", tmp_path / "u").returncode == 0
    env_file = tmp_path / "code-runner" / "code-runner.env"
    assert env_file.stat().st_mode & 0o777 == 0o600
    lines = [x for x in env_file.read_text().splitlines() if x.strip()]
    assert any(x.startswith("#") for x in lines)
    settings = dict(x.split("=", 1) for x in lines if not x.startswith("#"))
    assert settings["CODE_RUNNER_TRANSPORT"] == "streamable-http"
    assert settings["FASTMCP_PORT"] == "8031"
    assert settings["PATH"].startswith(str(tmp_path / "home" / ".local" / "bin"))
    backend = ROOT / "deploy" / "systemd" / "code-runner-backend.service"
    assert "EnvironmentFile=%h/.config/code-runner/code-runner.env" in backend.read_text()


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


def test_linger_warning_and_systemctl_calls(tmp_path, serve):
    r = run(
        tmp_path,
        stubs=make_stubs(tmp_path, fail=False),
        CODE_RUNNER_SMOKE_URL=serve(406),
        CODE_RUNNER_SMOKE_TIMEOUT=5,
    )
    assert r.returncode == 0, r.stderr
    assert "loginctl enable-linger" in r.stdout and MCP_URL in r.stdout
    log = (tmp_path / "stub.log").read_text().splitlines()
    assert not any("enable-linger" in x for x in log)
    assert not (tmp_path / "home" / ".claude.json").exists()
    assert not (tmp_path / "home" / ".claude").exists()


def test_smoke_default_timeout_is_60(tmp_path):
    r = run(tmp_path, "--dry-run")
    assert "timeout 60s" in r.stdout


def test_f10_smoke_accepts_only_measured_code(tmp_path, serve):
    stubs = make_stubs(tmp_path, fail=False)
    ok = run(
        tmp_path,
        stubs=stubs,
        CODE_RUNNER_SMOKE_URL=serve(MEASURED_CODE),
        CODE_RUNNER_SMOKE_TIMEOUT=3,
    )
    assert ok.returncode == 0 and "smoke: ok" in ok.stdout, ok.stderr
    for bad in (200, 404, 500):
        r = run(
            tmp_path, stubs=stubs, CODE_RUNNER_SMOKE_URL=serve(bad), CODE_RUNNER_SMOKE_TIMEOUT=1
        )
        assert r.returncode != 0 and "smoke: FAIL" in r.stderr and str(bad) in r.stderr
    r = run(tmp_path, stubs=stubs, CODE_RUNNER_SMOKE_URL=closed_url(), CODE_RUNNER_SMOKE_TIMEOUT=1)
    assert r.returncode != 0 and "smoke: FAIL" in r.stderr and "000" in r.stderr
    assert "curl -fs" in SCRIPT.read_text()


def test_f8_docs_install_command_runs(tmp_path):
    doc = (ROOT / "docs" / "DAEMON.md").read_text()
    cmds = re.findall(r"^scripts/install-daemon\.sh (--dry-run --unit-dir \S+)$", doc, re.M)
    assert cmds, "в docs/DAEMON.md нет команды проверки install-daemon.sh --dry-run --unit-dir"
    units = tmp_path / "units"
    args = [units if a.startswith("/") else a for a in shlex.split(cmds[0])]
    r = run(tmp_path, *args, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert "would write" in r.stdout and not units.exists()
    assert str(MEASURED_CODE) in doc

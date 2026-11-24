#!/usr/bin/env bash
# Ставит code-runner как user-демон systemd. Порядок и схема: docs/DAEMON.md.
# Использование: scripts/install-daemon.sh [--dry-run] [--unit-dir DIR] [--env-dir DIR]
set -euo pipefail

SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="$(dirname "$(dirname "$SCRIPT")")"
TEMPLATES="$REPO_DIR/deploy/systemd"
UNIT_DIR="$HOME/.config/systemd/user"
ENV_DIR=""
CLIENT_PORT=8030
BACKEND_PORT=8031
# GET /mcp без заголовка Accept: text/event-stream отвечает 406 (замер на streamable-http)
SMOKE_OK_CODE=406
MCP_URL="http://127.0.0.1:$CLIENT_PORT/mcp"
SMOKE_URL="${CODE_RUNNER_SMOKE_URL:-$MCP_URL}"
SMOKE_TIMEOUT="${CODE_RUNNER_SMOKE_TIMEOUT:-60}"
DRY_RUN=0
CUSTOM_UNIT_DIR=0
CHANGED=0
CHANGED_UNITS=()
CURRENT_TMP=""

trap 'rm -f "$CURRENT_TMP"' EXIT

usage() {
  cat <<'EOF'
Использование: install-daemon.sh [--dry-run] [--unit-dir DIR] [--env-dir DIR]
  --dry-run       напечатать план, ничего не писать
  --unit-dir DIR  каталог юнитов (по умолчанию ~/.config/systemd/user);
                  с ним systemctl, loginctl и smoke не вызываются
  --env-dir DIR   каталог code-runner.env (по умолчанию ~/.config/code-runner;
                  с --unit-dir — DIR/../code-runner)
Переменные: CODE_RUNNER_SMOKE_TIMEOUT (целое, с, по умолчанию 60), CODE_RUNNER_SMOKE_URL.
EOF
}

die_usage() {
  echo "ошибка: $1" >&2
  usage >&2
  exit 2
}

render() {
  local line repo_unit="${REPO_DIR//%/%%}"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line//@REPO_DIR@/"$repo_unit"}"
    line="${line//@CLIENT_PORT@/$CLIENT_PORT}"
    line="${line//@BACKEND_PORT@/$BACKEND_PORT}"
    printf '%s\n' "$line"
  done < "$1"
}

make_tmp() {
  CURRENT_TMP="$(mktemp --suffix=.tmp "$1/.$2.XXXXXX")"
}

install_unit() {
  local template="$TEMPLATES/$1" dest="$UNIT_DIR/$2" dir
  dir="$(dirname "$dest")"
  if [ -f "$dest" ] && cmp -s "$dest" <(render "$template"); then
    echo "unchanged: $dest"
    return
  fi
  CHANGED=$((CHANGED + 1))
  CHANGED_UNITS+=("$(basename "$dest")")
  if [ "$DRY_RUN" = 1 ]; then
    echo "would write: $dest"
    return
  fi
  mkdir -p "$dir"
  make_tmp "$dir" "$(basename "$dest")"
  render "$template" > "$CURRENT_TMP"
  chmod 644 "$CURRENT_TMP"
  mv "$CURRENT_TMP" "$dest"
  CURRENT_TMP=""
  echo "written: $dest"
}

has_forgetful() {
  [ -e "$UNIT_DIR/forgetful-proxy.socket" ]
}

unit_changed() {
  local u
  for u in "${CHANGED_UNITS[@]}"; do
    [ "$u" != "$1" ] || return 0
  done
  return 1
}

proxy_changed() {
  unit_changed code-runner-proxy.socket || unit_changed code-runner-proxy.service
}

ensure_env() {
  if [ -e "$ENV_FILE" ]; then
    echo "env exists, not touched: $ENV_FILE"
    return
  fi
  CHANGED=$((CHANGED + 1))
  if [ "$DRY_RUN" = 1 ]; then
    echo "would create: $ENV_FILE (0600)"
    return
  fi
  mkdir -p "$ENV_DIR"
  make_tmp "$ENV_DIR" code-runner.env
  cat > "$CURRENT_TMP" <<EOF
# Окружение демона code-runner (EnvironmentFile юнита code-runner-backend.service).
# Файл содержит секреты: права 0600, скрипт установки его не перезаписывает.
# Клиентский env из конфига Claude Code до демона не доезжает — задавай здесь.
# Ниже настройки, без которых backend не слушает порт $BACKEND_PORT (см. docs/DAEMON.md).
# Секреты и необязательные переменные добавляй сюда же.
CODE_RUNNER_TRANSPORT=streamable-http
FASTMCP_HOST=127.0.0.1
FASTMCP_PORT=$BACKEND_PORT
CODE_RUNNER_NO_PROJECT_CONFIG=1
PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
# CODE_RUNNER_SKIP_SERVERS=
EOF
  chmod 600 "$CURRENT_TMP"
  mv "$CURRENT_TMP" "$ENV_FILE"
  CURRENT_TMP=""
  echo "created: $ENV_FILE (0600, настройки HTTP-режима; секреты добавь сам)"
}

check_linger() {
  local user="${USER:-$(id -un)}" state
  state="$(loginctl show-user "$user" --property=Linger 2>/dev/null || true)"
  if [ "$state" = "Linger=no" ]; then
    echo "WARNING: Linger=no — демон остановится после выхода из сессии. Включи сам: loginctl enable-linger $user"
  fi
}

smoke() {
  local deadline=$((SECONDS + SMOKE_TIMEOUT))
  echo "smoke: curl $SMOKE_URL (до ${SMOKE_TIMEOUT} с)"
  local code=""
  while :; do
    code="$(curl -fs -o /dev/null -m 5 -w '%{http_code}' "$SMOKE_URL" || true)"
    if [ "$code" = "$SMOKE_OK_CODE" ]; then
      echo "smoke: ok (HTTP $code)"
      return 0
    fi
    [ "$SECONDS" -lt "$deadline" ] || break
    sleep 1
  done
  echo "smoke: FAIL — $SMOKE_URL: ожидался HTTP $SMOKE_OK_CODE, последний ответ: ${code:-нет} (за ${SMOKE_TIMEOUT} с)" >&2
  return 1
}

print_switch() {
  cat <<EOF
Переключи MCP-конфиг Claude Code на демон (скрипт его не правит):
  claude mcp remove code-runner --scope user
  claude mcp add --scope user --transport http code-runner $MCP_URL
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --unit-dir)
      [ "$#" -ge 2 ] || die_usage "--unit-dir требует аргумент"
      UNIT_DIR="$2"
      CUSTOM_UNIT_DIR=1
      shift
      ;;
    --env-dir)
      [ "$#" -ge 2 ] || die_usage "--env-dir требует аргумент"
      ENV_DIR="$2"
      shift
      ;;
    -h | --help) usage; exit 0 ;;
    *) die_usage "неизвестный аргумент: $1" ;;
  esac
  shift
done

case "$SMOKE_TIMEOUT" in
  '' | *[!0-9]*) die_usage "CODE_RUNNER_SMOKE_TIMEOUT должен быть целым числом секунд, получено: $SMOKE_TIMEOUT" ;;
  *) ;;
esac
if [ -z "$ENV_DIR" ]; then
  if [ "$CUSTOM_UNIT_DIR" = 1 ]; then
    ENV_DIR="$UNIT_DIR/../code-runner"
  else
    ENV_DIR="$HOME/.config/code-runner"
  fi
fi
ENV_FILE="$ENV_DIR/code-runner.env"

case "$REPO_DIR" in
  *[[:space:]]*) echo "ошибка: путь репо с пробелами не поддержан: $REPO_DIR" >&2; exit 1 ;;
  *) ;;
esac
[ -d "$TEMPLATES" ] || { echo "ошибка: нет $TEMPLATES" >&2; exit 1; }

[ "$DRY_RUN" = 0 ] || echo "dry-run: план, ничего не пишется"
echo "repo: $REPO_DIR"
echo "unit-dir: $UNIT_DIR"

install_unit code-runner-proxy.socket code-runner-proxy.socket
install_unit code-runner-proxy.service code-runner-proxy.service
install_unit code-runner-backend.service code-runner-backend.service
install_unit limits.conf code-runner-backend.service.d/limits.conf
if has_forgetful; then
  install_unit forgetful-ready.conf code-runner-backend.service.d/forgetful-ready.conf
else
  echo "skip: forgetful-ready.conf (нет forgetful-proxy.socket у хоста)"
fi
ensure_env
[ "$CHANGED" -gt 0 ] || echo "no changes"

if [ "$CUSTOM_UNIT_DIR" = 1 ]; then
  echo "systemctl, loginctl и smoke пропущены (--unit-dir)"
  print_switch
  exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
  [ "${#CHANGED_UNITS[@]}" = 0 ] || echo "plan: systemctl --user daemon-reload"
  if proxy_changed; then
    echo "plan: systemctl --user stop code-runner-proxy.service; restart code-runner-proxy.socket"
  fi
  echo "plan: systemctl --user enable --now code-runner-proxy.socket"
  echo "plan: loginctl show-user — проверка Linger"
  echo "plan: smoke: curl $SMOKE_URL, timeout ${SMOKE_TIMEOUT}s"
  print_switch
  exit 0
fi

if [ "${#CHANGED_UNITS[@]}" -gt 0 ]; then
  systemctl --user daemon-reload
  echo "изменены юниты: ${CHANGED_UNITS[*]}"
  echo "если backend уже запущен: systemctl --user restart code-runner-backend.service"
fi
if proxy_changed; then
  systemctl --user stop code-runner-proxy.service
  systemctl --user restart code-runner-proxy.socket
fi
systemctl --user enable --now code-runner-proxy.socket
check_linger
print_switch
smoke

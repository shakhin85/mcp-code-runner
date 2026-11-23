#!/usr/bin/env bash
# Ставит code-runner как user-демон systemd. Порядок и схема: docs/DAEMON.md.
# Использование: scripts/install-daemon.sh [--dry-run] [--unit-dir DIR]
set -euo pipefail

SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="$(dirname "$(dirname "$SCRIPT")")"
TEMPLATES="$REPO_DIR/deploy/systemd"
UNIT_DIR="$HOME/.config/systemd/user"
ENV_DIR="${CODE_RUNNER_ENV_DIR:-$HOME/.config/code-runner}"
ENV_FILE="$ENV_DIR/code-runner.env"
SYSTEM_UNIT_DIRS="${CODE_RUNNER_UNIT_SEARCH_PATH-/etc/systemd/user:/usr/lib/systemd/user:/lib/systemd/user}"
MCP_URL="http://127.0.0.1:8030/mcp"
SMOKE_URL="${CODE_RUNNER_SMOKE_URL:-$MCP_URL}"
SMOKE_TIMEOUT="${CODE_RUNNER_SMOKE_TIMEOUT:-60}"
DRY_RUN=0
CUSTOM_UNIT_DIR=0
CHANGED=0
CURRENT_TMP=""

trap 'rm -f "$CURRENT_TMP"' EXIT

usage() {
  cat <<'EOF'
Использование: install-daemon.sh [--dry-run] [--unit-dir DIR]
  --dry-run       напечатать план, ничего не писать
  --unit-dir DIR  каталог юнитов (по умолчанию ~/.config/systemd/user);
                  с ним systemctl, loginctl и smoke не вызываются
Переменные: CODE_RUNNER_ENV_DIR, CODE_RUNNER_SMOKE_TIMEOUT (с, по умолчанию 60),
  CODE_RUNNER_SMOKE_URL, CODE_RUNNER_UNIT_SEARCH_PATH.
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
    printf '%s\n' "${line//@REPO_DIR@/"$repo_unit"}"
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
  local dirs d
  IFS=: read -ra dirs <<< "$UNIT_DIR:$SYSTEM_UNIT_DIRS"
  for d in "${dirs[@]}"; do
    if [ -n "$d" ] && [ -e "$d/forgetful-proxy.socket" ]; then
      return 0
    fi
  done
  return 1
}

ensure_env() {
  if [ -e "$ENV_FILE" ]; then
    echo "env exists, not touched: $ENV_FILE"
    return
  fi
  if [ "$DRY_RUN" = 1 ]; then
    echo "would create: $ENV_FILE (0600)"
    return
  fi
  mkdir -p "$ENV_DIR"
  make_tmp "$ENV_DIR" code-runner.env
  cat > "$CURRENT_TMP" <<'EOF'
# Окружение демона code-runner (EnvironmentFile юнита code-runner-backend.service).
# Файл содержит секреты: права 0600, скрипт установки его не перезаписывает.
# Клиентский env из конфига Claude Code до демона не доезжает — задавай здесь.
# Раскомментируй и поправь значения (см. docs/DAEMON.md):
#
# CODE_RUNNER_TRANSPORT=streamable-http
# FASTMCP_HOST=127.0.0.1
# FASTMCP_PORT=8031
# CODE_RUNNER_NO_PROJECT_CONFIG=1
# CODE_RUNNER_SKIP_SERVERS=
# PATH=/home/USER/.local/bin:/usr/local/bin:/usr/bin:/bin
EOF
  chmod 600 "$CURRENT_TMP"
  mv "$CURRENT_TMP" "$ENV_FILE"
  CURRENT_TMP=""
  echo "created: $ENV_FILE (0600, шаблон с комментариями: заполни и перезапусти backend)"
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
  while :; do
    if curl -s -o /dev/null -m 5 "$SMOKE_URL"; then
      echo "smoke: ok"
      return 0
    fi
    [ "$SECONDS" -lt "$deadline" ] || break
    sleep 1
  done
  echo "smoke: FAIL — $SMOKE_URL не ответил за ${SMOKE_TIMEOUT} с" >&2
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
    -h | --help) usage; exit 0 ;;
    *) die_usage "неизвестный аргумент: $1" ;;
  esac
  shift
done

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
  echo "plan: systemctl --user daemon-reload (если юниты изменились)"
  echo "plan: systemctl --user enable --now code-runner-proxy.socket"
  echo "plan: loginctl show-user — проверка Linger"
  echo "plan: smoke: curl $SMOKE_URL, timeout ${SMOKE_TIMEOUT}s"
  print_switch
  exit 0
fi

if [ "$CHANGED" -gt 0 ]; then
  systemctl --user daemon-reload
  echo "если backend уже запущен: systemctl --user restart code-runner-backend.service"
fi
systemctl --user enable --now code-runner-proxy.socket
check_linger
print_switch
smoke

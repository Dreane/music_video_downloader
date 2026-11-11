#!/usr/bin/env bash
# Поднимает бота, если он не запущен. Безопасно звать из cron раз в 5 минут.
#
# На сервере подставь свой путь к проекту:
# crontab -e
# */5 * * * * /bin/bash /ПУТЬ/К/ПРОЕКТУ/scripts/start.sh
set -euo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/run/bot.pid"
LOG_FILE="$ROOT/logs/bot.log"
CRON_LOG="$ROOT/logs/cron.log"
LOCK_FILE="$ROOT/run/start.lock"
PYTHON="$ROOT/venv/bin/python"

mkdir -p "$ROOT/run" "$ROOT/logs"

note() {
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$CRON_LOG"
}

trap 'note "ошибка, строка $LINENO: $BASH_COMMAND"' ERR

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    note "пропуск: предыдущий запуск ещё не закончился"
    exit 0
  fi
fi

process_cmd() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline"
}

is_our_process() {
  local cmd
  cmd="$(process_cmd "$1" || true)"
  [[ "$cmd" == *"${ROOT}/bot.py"* || "$cmd" == *"${ROOT}/run.sh"* ]]
}

find_running_pid() {
  local pid cmd
  if [[ -f "$PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
    if is_our_process "$pid"; then
      printf '%s\n' "$pid"
      return 0
    fi
  fi
  for pid in /proc/[0-9]*; do
    pid="${pid#/proc/}"
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    cmd="$(process_cmd "$pid" || true)"
    if [[ "$cmd" == *"${ROOT}/bot.py"* || "$cmd" == *"${ROOT}/run.sh supervise"* ]]; then
      printf '%s\n' "$pid"
      return 0
    fi
  done
  return 1
}

if running="$(find_running_pid)"; then
  printf '%s\n' "$running" > "$PID_FILE"
  exit 0
fi

if [[ ! -x "$PYTHON" ]] || ! "$PYTHON" -c 'import sys' >/dev/null 2>&1; then
  note "venv не работает, создаю заново"
  rm -rf "$ROOT/venv"
  python3 -m venv "$ROOT/venv"
  "$PYTHON" -m pip install -q -r "$ROOT/requirements.txt"
fi

cd "$ROOT"
if command -v setsid >/dev/null 2>&1; then
  setsid bash "$ROOT/run.sh" supervise >> "$LOG_FILE" 2>&1 < /dev/null &
else
  bash "$ROOT/run.sh" supervise >> "$LOG_FILE" 2>&1 < /dev/null &
fi
pid="$!"
printf '%s\n' "$pid" > "$PID_FILE"
sleep 1

if ! is_our_process "$pid"; then
  note "процесс $pid сразу завершился, смотри logs/bot.log"
  exit 1
fi

note "бот запущен, pid $pid"

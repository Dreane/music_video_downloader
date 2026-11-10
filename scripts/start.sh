#!/usr/bin/env bash
# Поднимает бота, если он не запущен. Безопасно звать из cron каждую минуту.
#
# crontab -e
# * * * * * /home/user/music_video_downloader/scripts/start.sh
set -euo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT/run/bot.pid"
LOG_FILE="$ROOT/logs/bot.log"
LOCK_FILE="$ROOT/run/start.lock"
PYTHON="$ROOT/venv/bin/python"

mkdir -p "$ROOT/run" "$ROOT/logs"

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  if pgrep -f "${PYTHON} ${ROOT}/bot.py" >/dev/null 2>&1; then
    return 0
  fi
  if pgrep -f "${ROOT}/run.sh supervise" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if is_running; then
  exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
  python3 -m venv "$ROOT/venv"
  "$PYTHON" -m pip install -q -r "$ROOT/requirements.txt"
fi

cd "$ROOT"
setsid "$ROOT/run.sh" supervise >> "$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$PID_FILE"

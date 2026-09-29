#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
UNIT_NAME="tiktok-bot.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PID_FILE="$ROOT/run/bot.pid"
LOG_FILE="$ROOT/logs/bot.log"

have_user_systemd() {
  systemctl --user show-environment >/dev/null 2>&1
}

install_unit() {
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$UNIT_NAME" << EOF
[Unit]
Description=Telegram-бот скачивания TikTok
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$ROOT/venv/bin/python $ROOT/bot.py
Restart=always
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=default.target
EOF
}

ensure_venv() {
  if [[ ! -d venv ]]; then
    python3 -m venv venv
  fi
  venv/bin/pip install -q -r requirements.txt
}

enable_linger() {
  if loginctl show-user "$USER" -p Linger | grep -q '=yes'; then
    return
  fi
  if sudo -n loginctl enable-linger "$USER" 2>/dev/null; then
    return
  fi
  echo "Чтобы служба жила после закрытия всех окон, один раз выполни:"
  echo "  sudo loginctl enable-linger $USER"
}

supervise() {
  cd "$ROOT"
  mkdir -p "$ROOT/logs"
  while true; do
    "$ROOT/venv/bin/python" "$ROOT/bot.py" >> "$LOG_FILE" 2>&1 || true
    echo "$(date -Is) процесс завершился, перезапуск через 5 с" >> "$LOG_FILE"
    sleep 5
  done
}

daemon_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

start_daemon() {
  mkdir -p "$ROOT/run" "$ROOT/logs"
  if daemon_running; then
    echo "Бот уже запущен, pid $(cat "$PID_FILE")"
    return
  fi
  setsid "$0" supervise >> "$LOG_FILE" 2>&1 < /dev/null &
  echo $! > "$PID_FILE"
  sleep 1
  if daemon_running; then
    echo "Бот запущен в фоне, pid $(cat "$PID_FILE")"
    echo "Лог: $LOG_FILE"
  else
    echo "Бот не запустился. Последние строки лога:"
    tail -n 30 "$LOG_FILE" || true
    exit 1
  fi
}

stop_daemon() {
  if ! daemon_running; then
    rm -f "$PID_FILE"
    echo "Бот не запущен"
    return
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "Бот остановлен"
}

status_daemon() {
  if daemon_running; then
    echo "Бот запущен, pid $(cat "$PID_FILE")"
  else
    echo "Бот не запущен"
  fi
}

cmd="${1:-start}"
if [[ "$cmd" == "supervise" ]]; then
  supervise
  exit 0
fi

if have_user_systemd; then
  case "$cmd" in
    start)
      ensure_venv
      install_unit
      systemctl --user daemon-reload
      systemctl --user enable --now "$UNIT_NAME"
      enable_linger
      systemctl --user --no-pager --lines=15 status "$UNIT_NAME"
      ;;
    stop)
      systemctl --user stop "$UNIT_NAME"
      ;;
    restart)
      ensure_venv
      install_unit
      systemctl --user daemon-reload
      systemctl --user restart "$UNIT_NAME"
      systemctl --user --no-pager --lines=15 status "$UNIT_NAME"
      ;;
    status)
      systemctl --user --no-pager --lines=20 status "$UNIT_NAME"
      ;;
    logs)
      journalctl --user -u "$UNIT_NAME" -n 80 --no-pager
      ;;
    *)
      echo "Использование: ./run.sh [start|stop|restart|status|logs]"
      exit 1
      ;;
  esac
else
  case "$cmd" in
    start)
      ensure_venv
      start_daemon
      ;;
    stop)
      stop_daemon
      ;;
    restart)
      stop_daemon
      ensure_venv
      start_daemon
      ;;
    status)
      status_daemon
      ;;
    logs)
      tail -n 80 "$LOG_FILE"
      ;;
    *)
      echo "Использование: ./run.sh [start|stop|restart|status|logs]"
      exit 1
      ;;
  esac
fi

#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
UNIT_NAME="tiktok-bot.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

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
  echo "Чтобы служба жила после закрытия всех окон WSL, один раз выполни:"
  echo "  sudo loginctl enable-linger $USER"
}

cmd="${1:-start}"
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

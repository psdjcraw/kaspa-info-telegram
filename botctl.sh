#!/usr/bin/env zsh
set -euo pipefail

cd "$(dirname "$0")"

PID_FILE="ticker.pid"
LOG_FILE="ticker.log"

load_env() {
  if [[ -f .env ]]; then
    set -a
    source .env
    set +a
  fi
}

is_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

case "${1:-status}" in
  start)
    load_env
    if is_running; then
      echo "running pid=$(cat "$PID_FILE")"
      exit 0
    fi
    PYTHONUNBUFFERED=1 nohup python3 ticker_bot.py >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    echo "started pid=$(cat "$PID_FILE")"
    ;;
  stop)
    if is_running; then
      kill "$(cat "$PID_FILE")"
      echo "stopped pid=$(cat "$PID_FILE")"
    else
      echo "not running"
    fi
    rm -f "$PID_FILE"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    if is_running; then
      echo "running pid=$(cat "$PID_FILE")"
    else
      echo "not running"
    fi
    ;;
  tail)
    touch "$LOG_FILE"
    tail -n 80 -f "$LOG_FILE"
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|tail}"
    exit 2
    ;;
esac

#!/usr/bin/env bash
# Start/stop the Telegram listener.
#
#   ./bin/listen.sh start | stop | status | tail
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/state"
PIDFILE="$STATE/listener.pid"
LOGFILE="$STATE/listener.log"
mkdir -p "$STATE"

running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "${1:-status}" in
  start)
    if running; then echo "already running (pid $(cat "$PIDFILE"))"; exit 0; fi
    setsid "$ROOT/bin/listener.py" >>"$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if running; then
      echo "started (pid $(cat "$PIDFILE")) — log: $LOGFILE"
      tail -3 "$LOGFILE"
    else
      echo "failed to start:" >&2
      tail -20 "$LOGFILE" >&2
      exit 1
    fi
    ;;
  stop)
    if running; then
      kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
      echo "stopped"
    else
      echo "not running"
      rm -f "$PIDFILE"
    fi
    ;;
  status)
    if running; then echo "running (pid $(cat "$PIDFILE"))"; else echo "not running"; fi
    ;;
  tail)
    tail -f "$LOGFILE"
    ;;
  *)
    echo "usage: $0 {start|stop|status|tail}" >&2
    exit 1
    ;;
esac

#!/usr/bin/env bash
# Manage the Entity Highlighter API on hosts without Docker or systemd
# (proot-distro, a VPS, a Raspberry Pi).
#
#   ./server.sh start   [--profile phone|small|team10]
#   ./server.sh stop
#   ./server.sh restart
#   ./server.sh reset   [--purge-logs]   stop everything, free the port, start clean
#   ./server.sh status
#   ./server.sh logs
#   ./server.sh run                      foreground and auto-restarting (used by start)
#
# Settings: environment variables or a .env file next to this script
# (see .env.example). NER_API_KEY is required.
#
# Profiles (override any value by setting the variable yourself):
#   phone   1 worker, NLTK off     low-RAM devices
#   small   1 worker, NLTK on      about 2 GB RAM
#   team10  2 workers, NLTK on     about 10 concurrent users, 3 GB+ RAM
# Re-run under bash if started with `sh server.sh` (dash lacks bash features).
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -u

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
cd "$(dirname "$SCRIPT")" || exit 1
BASE="$(pwd -P)"
[ -f .env ] && { set -a; . ./.env; set +a; }
VENV="${VENV:-$HOME/venv}"
[ -f "$VENV/bin/activate" ] && . "$VENV/bin/activate"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
CHECK_HOST="$HOST"; [ "$HOST" = "0.0.0.0" ] && CHECK_HOST="127.0.0.1"
RUN_DIR="${RUN_DIR:-$HOME/.ner-server}"; mkdir -p "$RUN_DIR"
PID_FILE="$RUN_DIR/$PORT.pid"
LOG_FILE="$RUN_DIR/$PORT.log"
PROFILE="${PROFILE:-phone}"
PATTERN="uvicorn app:app --host $HOST --port $PORT"

say() { printf '[server] %s\n' "$*"; }
die() { printf '[server] ERROR: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,19p' "$SCRIPT" | sed 's/^# \{0,1\}//'; }

apply_profile() {
  local workers nltk
  case "$PROFILE" in
    phone)  workers=1; nltk=0 ;;
    small)  workers=1; nltk=1 ;;
    team10) workers=2; nltk=1 ;;
    *) die "unknown profile '$PROFILE' (phone | small | team10)" ;;
  esac
  export WEB_CONCURRENCY="${WEB_CONCURRENCY:-$workers}"
  export ENABLE_NLTK="${ENABLE_NLTK:-$nltk}"
  export MAX_INFLIGHT="${MAX_INFLIGHT:-10}"
  export QUEUE_WAIT_S="${QUEUE_WAIT_S:-30}"
  export RATE_LIMIT_PER_MIN="${RATE_LIMIT_PER_MIN:-600}"
}

need_key() { [ -n "${NER_API_KEY:-}" ] || die "NER_API_KEY is not set (export it or put it in .env)"; }

# ---- process helpers (pure /proc, so no procps/lsof needed) -----------------
find_pids() {          # PIDs whose command line contains $1
  local p cmd
  for p in /proc/[0-9]*; do
    p="${p#/proc/}"
    [ "$p" = "$$" ] && continue
    cmd="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)" || continue
    case "$cmd" in *"$1"*) echo "$p" ;; esac
  done
}

find_workers() {       # orphaned uvicorn worker processes started from this folder
  local p cmd
  for p in /proc/[0-9]*; do
    p="${p#/proc/}"
    cmd="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)" || continue
    case "$cmd" in *multiprocessing.spawn*|*multiprocessing.resource_tracker*) ;; *) continue ;; esac
    [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$BASE" ] && echo "$p"
  done
}

is_alive() {           # true if PID exists and is not a zombie
  local state
  kill -0 "$1" 2>/dev/null || return 1
  state="$(sed 's/.*) //' "/proc/$1/stat" 2>/dev/null | cut -c1)"
  [ "$state" != "Z" ]
}

alive() { local p; for p in "$@"; do is_alive "$p" && return 0; done; return 1; }

terminate() {          # TERM, wait up to 10 s, then KILL
  local pids="$*" i
  [ -z "${pids// /}" ] && return 0
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null
  for i in $(seq 1 20); do
    # shellcheck disable=SC2086
    alive $pids || return 0
    sleep 0.5
  done
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null
}

running() { [ -f "$PID_FILE" ] && is_alive "$(cat "$PID_FILE")"; }

port_busy() {
  python3 - "$HOST" "$PORT" <<'PY'
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind((sys.argv[1], int(sys.argv[2])))
except OSError:
    sys.exit(0)   # busy
sys.exit(1)       # free
PY
}

fetch() {              # fetch PATH -> prints body; non-zero on failure
  python3 - "$CHECK_HOST" "$PORT" "$1" "${NER_API_KEY:-}" <<'PY'
import sys, urllib.request
host, port, path, key = sys.argv[1:5]
req = urllib.request.Request(f"http://{host}:{port}{path}", headers={"X-API-Key": key})
try:
    print(urllib.request.urlopen(req, timeout=5).read().decode())
except Exception as exc:
    print(f"error: {exc}")
    sys.exit(1)
PY
}

healthy() { fetch /health >/dev/null 2>&1; }

wait_healthy() {       # wait_healthy SECONDS
  local i
  for i in $(seq 1 "$(( $1 / 2 ))"); do
    healthy && return 0
    [ "$i" -gt 3 ] && ! running && return 1     # supervisor died early
    sleep 2
  done
  return 1
}

rotate_log() {
  [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt 5242880 ] && mv -f "$LOG_FILE" "$LOG_FILE.1"
  return 0
}

# ---- commands ---------------------------------------------------------------
cmd_run() {            # foreground supervisor: restarts uvicorn with backoff
  need_key
  running && die "already running (pid $(cat "$PID_FILE"))"
  echo $$ > "$PID_FILE"
  local child="" delay=2 start ran code
  cleanup() {
    [ -n "$child" ] && kill "$child" 2>/dev/null && wait "$child" 2>/dev/null
    rm -f "$PID_FILE"; exit 0
  }
  trap cleanup TERM INT
  say "profile=$PROFILE workers=$WEB_CONCURRENCY nltk=$ENABLE_NLTK on $HOST:$PORT"
  while true; do
    start=$(date +%s)
    uvicorn app:app --host "$HOST" --port "$PORT" --workers "$WEB_CONCURRENCY" \
      --limit-max-requests 1000 --timeout-graceful-shutdown 20 &
    child=$!
    wait "$child"; code=$?
    child=""
    ran=$(( $(date +%s) - start ))
    if [ "$ran" -gt 60 ]; then delay=2; else delay=$(( delay * 2 )); [ "$delay" -gt 60 ] && delay=60; fi
    say "uvicorn exited with $code after ${ran}s; restarting in ${delay}s"
    [ "$code" = 137 ] && say "137 = killed (out of memory or the Android process killer)"
    sleep "$delay" & wait $!
  done
}

cmd_start() {
  need_key
  [ -f "$PID_FILE" ] && ! running && rm -f "$PID_FILE"
  if running; then say "already running (pid $(cat "$PID_FILE")) on $HOST:$PORT"; return 0; fi
  port_busy && die "port $PORT is in use by something else. Run: ./server.sh reset   (or use PORT=8001)"
  rotate_log
  nohup bash "$SCRIPT" run --profile "$PROFILE" >> "$LOG_FILE" 2>&1 &
  say "starting; log: $LOG_FILE"
  if ! wait_healthy "${START_TIMEOUT:-180}"; then
    tail -n 25 "$LOG_FILE" >&2
    die "server did not become healthy (log above)"
  fi
  say "up: http://$CHECK_HOST:$PORT"
}

cmd_stop() {
  local pids
  running && terminate "$(cat "$PID_FILE")"       # supervisor first: no auto-restart
  pids="$(find_pids "$PATTERN") $(find_workers)"
  terminate $pids
  rm -f "$PID_FILE"
  if port_busy; then say "stopped, but port $PORT is still busy (another program?)"; else say "stopped"; fi
}

cmd_reset() {
  say "resetting: stop, free port, clean state, start"
  cmd_stop
  if port_busy && command -v fuser >/dev/null; then fuser -k "$PORT/tcp" >/dev/null 2>&1; sleep 1; fi
  port_busy && die "port $PORT is held by another program; stop it or use PORT=8001"
  [ "${PURGE:-0}" = 1 ] && rm -f "$LOG_FILE" "$LOG_FILE.1"
  cmd_start
}

cmd_status() {
  if running; then say "supervisor running (pid $(cat "$PID_FILE"))"; else say "supervisor not running"; fi
  if healthy; then say "health: $(fetch /health)"; else say "no answer on $CHECK_HOST:$PORT"; return 1; fi
  if [ -n "${NER_API_KEY:-}" ]; then
    if st="$(fetch /v1/stats)"; then say "stats:  $st"
    else say "stats unavailable ($st). An older server version may be running: ./server.sh reset"; fi
  fi
  return 0
}

# ---- main -------------------------------------------------------------------
cmd="${1:-help}"; [ $# -gt 0 ] && shift
PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile)     [ $# -ge 2 ] || die "--profile needs a value"; PROFILE="$2"; shift 2 ;;
    --purge-logs)  PURGE=1; shift ;;
    *) die "unknown option: $1" ;;
  esac
done
apply_profile

case "$cmd" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  reset)   cmd_reset ;;
  status)  cmd_status ;;
  logs)    tail -n 100 -f "$LOG_FILE" ;;
  run)     cmd_run ;;
  help|-h|--help) usage ;;
  *) usage; exit 1 ;;
esac

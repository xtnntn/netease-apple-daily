#!/usr/bin/env bash
# Entry: Apple Music favorites/recent → NetEase like/scrobble
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy no_proxy NO_PROXY

if [[ -f "$ROOT/config.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/config.env"
  set +a
fi

# cron often supplies only /usr/bin:/bin. Preserve custom tools first.
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

resolve_path() {
  local p="$1"
  if [[ -z "$p" ]]; then
    printf '\n'
    return
  fi
  if [[ "$p" = /* ]]; then
    printf '%s\n' "$p"
  else
    printf '%s\n' "$ROOT/${p#./}"
  fi
}

COOKIE_FILE="$(resolve_path "${COOKIE_FILE:-./data/cookie.txt}")"
export COOKIE_FILE
LOG_DIR="$(resolve_path "${LOG_DIR:-./logs}")"
export LOG_DIR
DATA_DIR="$(resolve_path "${DATA_DIR:-./data}")"
export DATA_DIR
if [[ -n "${AM_CONFIG:-}" ]]; then
  AM_CONFIG="$(resolve_path "$AM_CONFIG")"
  export AM_CONFIG
fi
if [[ -n "${FEEDBACK_STATE_FILE:-}" ]]; then
  FEEDBACK_STATE_FILE="$(resolve_path "$FEEDBACK_STATE_FILE")"
  export FEEDBACK_STATE_FILE
fi

mkdir -p "$LOG_DIR" "$DATA_DIR"
LOCK="$DATA_DIR/feedback.lock"

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "[$(date -Iseconds)] feedback already running" >>"$LOG_DIR/cron.log"
    exit 0
  fi
else
  echo "Warning: flock unavailable; concurrent runs are not prevented." >&2
  echo "[$(date -Iseconds)] warning: flock unavailable; concurrent runs are not prevented" >>"$LOG_DIR/cron.log"
fi

echo "[$(date -Iseconds)] feedback start" >>"$LOG_DIR/cron.log"

PYTHON="${PYTHON:-python3}"
if "$PYTHON" "$ROOT/apple_to_netease_feedback.py" "$@"; then
  rc=0
else
  rc=$?
fi
echo "[$(date -Iseconds)] feedback finished rc=$rc" >>"$LOG_DIR/cron.log"
exit "$rc"

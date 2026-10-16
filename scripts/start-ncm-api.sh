#!/usr/bin/env bash
# Start api-enhanced in the background on 127.0.0.1 if it is not already up.
# Idempotent: safe for NCM_API_START_CMD, @reboot and a */10 watchdog cron.
#
# Environment (all optional):
#   NCM_API_DIR   npm install directory (default: $HOME/ncm-api)
#   NCM_NODE_BIN  directory containing node, prepended to PATH
#   NCM_API_PORT  listen port (default: 3000)
set -uo pipefail

DIR="${NCM_API_DIR:-$HOME/ncm-api}"
APP="$DIR/node_modules/@neteasecloudmusicapienhanced/api"
if [[ -n "${NCM_NODE_BIN:-}" ]]; then
  export PATH="$NCM_NODE_BIN:$PATH"
fi
export HOST=127.0.0.1
export PORT="${NCM_API_PORT:-3000}"
export ENABLE_GENERAL_UNBLOCK=false
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY

alive() { curl -fsS --noproxy '*' --max-time 3 "http://$HOST:$PORT/banner?type=0" >/dev/null 2>&1; }

[[ -f "$APP/app.js" ]] || { echo "api-enhanced not found in $APP" >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo "node not found; set NCM_NODE_BIN" >&2; exit 1; }

# flock prevents concurrent starts (Linux); macOS without flock just skips the lock.
exec 9>"$DIR/start.lock"
if command -v flock >/dev/null 2>&1; then
  flock -n 9 || exit 0
fi

alive && exit 0

# Clean up a stale or hung process from a previous run.
if [[ -f "$DIR/ncm-api.pid" ]]; then
  old="$(cat "$DIR/ncm-api.pid")"
  if [[ -n "$old" ]] && ps -p "$old" -o args= 2>/dev/null | grep -q 'node app.js'; then
    kill "$old" 2>/dev/null
    sleep 2
  fi
fi

cd "$APP" || exit 1
nohup node app.js >>"$DIR/ncm-api.log" 2>&1 9>&- &
echo $! >"$DIR/ncm-api.pid"
echo "$(date '+%F %T') started pid $!" >>"$DIR/ncm-api.log"

for _ in $(seq 1 20); do
  alive && exit 0
  sleep 1
done
echo "$(date '+%F %T') not ready after 20s" >>"$DIR/ncm-api.log"
exit 1

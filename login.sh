#!/usr/bin/env bash
# 网易云登录：qr-init / qr-poll / qr / status / cookie - (stdin preferred)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [[ -f "$ROOT/config.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/config.env"
  set +a
fi
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy no_proxy NO_PROXY
umask 077

resolve_path() {
  if [[ "$1" = /* ]]; then
    printf '%s\n' "$1"
  else
    printf '%s\n' "$ROOT/${1#./}"
  fi
}

NCM_API_BASE="${NCM_API_BASE:-http://127.0.0.1:3000}"
COOKIE_FILE="$(resolve_path "${COOKIE_FILE:-./data/cookie.txt}")"
DATA_DIR="$(resolve_path "${DATA_DIR:-./data}")"
export NCM_API_BASE COOKIE_FILE DATA_DIR
PYTHON="${PYTHON:-python3}"
for dependency in curl jq "$PYTHON"; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    echo "缺少必要依赖: $dependency" >&2
    exit 1
  fi
done
mkdir -p "$DATA_DIR" "$(dirname "$COOKIE_FILE")"
CURL=(curl -fsS --noproxy '*' --connect-timeout 3 --max-time 15)
KEY_FILE="$DATA_DIR/login-key.txt"
QR_PNG="$DATA_DIR/login-qr.png"
QR_URL_FILE="$DATA_DIR/login-qrurl.txt"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ncm-login.XXXXXXXX")"
trap 'rm -rf "$TEMP_DIR"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
HEADERS_FILE="$TEMP_DIR/headers"

need_api() {
  # Shared startup handles both Node NCM_API_START_CMD and Docker deployments.
  "$PYTHON" -c 'from daily_recommend import ensure_ncm_api; ensure_ncm_api()'
}

private_file() {
  # Tighten existing files before truncating; new files inherit umask 077.
  touch "$1"
  chmod 600 "$1"
}

save_cookie() {
  cookie="$(printf '%s' "$cookie" | tr -d '\r\n')"
  if [[ -z "$cookie" ]]; then
    echo "Cookie 不能为空" >&2
    return 1
  fi
  private_file "$COOKIE_FILE"
  printf '%s\n' "$cookie" >"$COOKIE_FILE"
  echo "Cookie 已写入: $COOKIE_FILE"
}

check_status() {
  if [[ ! -s "$COOKIE_FILE" ]]; then
    echo "无 cookie: $COOKIE_FILE" >&2
    return 1
  fi
  # curl reads the body from a private file: no cookie in URL or argv.
  tr -d '\n\r' <"$COOKIE_FILE" >"$TEMP_DIR/cookie"
  local response
  if ! response="$("${CURL[@]}" --request POST \
    --data-urlencode "cookie@$TEMP_DIR/cookie" \
    "$NCM_API_BASE/login/status?timestamp=$(date +%s)" 2>/dev/null)"; then
    echo "登录状态请求失败（HTTP 或网络错误）" >&2
    return 1
  fi
  if ! printf '%s' "$response" | jq -e \
    '(.data.code == 200) and ((.data.profile | type) == "object") and
     (.data.profile.userId | tonumber? | . > 0 and . == floor)' >/dev/null 2>&1; then
    echo "Cookie 已失效或登录状态响应无效，请重新登录" >&2
    return 1
  fi
  printf '%s' "$response" | jq '{code: .data.code, profile: .data.profile.nickname}'
}

qr_init() {
  need_api
  local key_json key create_json qrurl qrimg b64
  if ! key_json="$("${CURL[@]}" "$NCM_API_BASE/login/qr/key?timestamp=$(date +%s)" 2>/dev/null)" ||
    ! key="$(printf '%s' "$key_json" | jq -er '.data.unikey | select(type == "string" and length > 0)' 2>/dev/null)"; then
    echo "获取登录 key 失败（HTTP、网络或响应错误）" >&2
    return 1
  fi
  if ! create_json="$("${CURL[@]}" --get "$NCM_API_BASE/login/qr/create" \
    --data-urlencode "key=$key" --data-urlencode 'qrimg=true' \
    --data-urlencode "timestamp=$(date +%s)" 2>/dev/null)" ||
    ! qrurl="$(printf '%s' "$create_json" | jq -er '.data.qrurl | select(type == "string" and length > 0)' 2>/dev/null)"; then
    echo "生成二维码失败（HTTP、网络或响应错误）" >&2
    return 1
  fi
  private_file "$KEY_FILE"
  printf '%s\n' "$key" >"$KEY_FILE"
  private_file "$QR_URL_FILE"
  printf '%s\n' "$qrurl" >"$QR_URL_FILE"
  qrimg="$(printf '%s' "$create_json" | jq -r '.data.qrimg // empty' 2>/dev/null)"
  rm -f "$QR_PNG"
  if [[ -n "$qrimg" ]]; then
    b64="${qrimg#*,}"
    if ! printf '%s' "$b64" | base64 -d >"$QR_PNG" 2>/dev/null; then
      rm -f "$QR_PNG"
    fi
  fi
  if command -v qrencode >/dev/null 2>&1; then
    qrencode -t PNG -o "$QR_PNG" "$qrurl" 2>/dev/null || true
    qrencode -t ANSIUTF8 "$qrurl" 2>/dev/null || true
  fi
  if [[ -f "$QR_PNG" ]]; then
    chmod 644 "$QR_PNG"
  fi
  echo "QR_INIT_OK"
  echo "key_file=$KEY_FILE"
  echo "qr_png=$QR_PNG"
  echo "qr_url=$qrurl"
  echo "请用网易云 App 扫码；扫码后执行: $0 qr-poll"
}

qr_poll() {
  local max="${1:-90}" key check code cookie i
  if [[ ! "$max" =~ ^[1-9][0-9]*$ || ${#max} -gt 4 ]] || (( max > 1000 )); then
    echo "轮询次数必须为 1 到 1000" >&2
    return 1
  fi
  need_api
  if [[ ! -s "$KEY_FILE" ]]; then
    echo "无 login key，先运行: $0 qr-init" >&2
    return 1
  fi
  key="$(tr -d '\n\r' <"$KEY_FILE")"
  for ((i=1; i<=max; i++)); do
    : >"$HEADERS_FILE"
    if ! check="$("${CURL[@]}" -D "$HEADERS_FILE" --get "$NCM_API_BASE/login/qr/check" \
      --data-urlencode "key=$key" --data-urlencode "timestamp=$(date +%s)" 2>/dev/null)"; then
      printf '[%s/%s] QR 网络或 HTTP 错误，稍后重试\n' "$i" "$max" >&2
    elif ! code="$(printf '%s' "$check" | jq -er '.code | select(type == "number")' 2>/dev/null)"; then
      printf '[%s/%s] QR 响应无效，稍后重试\n' "$i" "$max" >&2
    else
      case "$code" in
        800)
          rm -f "$KEY_FILE"
          echo "QR_EXPIRED: 二维码过期，请重新 qr-init" >&2
          return 1
          ;;
        801) printf '[%s/%s] waiting_scan\n' "$i" "$max" ;;
        802) printf '[%s/%s] scanned_confirm_on_phone\n' "$i" "$max" ;;
        803)
          cookie="$(printf '%s' "$check" | jq -r '.cookie | select(type == "string")' 2>/dev/null)"
          if [[ -z "$cookie" ]]; then
            cookie="$(awk 'tolower($0) ~ /^set-cookie:/ {sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); sub(/;.*/, ""); if (length) {printf "%s%s", sep, $0; sep=";"}}' "$HEADERS_FILE")"
          fi
          if [[ -z "$cookie" ]]; then
            echo "LOGIN_FAIL no cookie" >&2
            return 1
          fi
          save_cookie
          rm -f "$KEY_FILE"
          check_status
          echo "LOGIN_OK"
          return
          ;;
        *) printf '[%s/%s] QR 状态异常，稍后重试\n' "$i" "$max" >&2 ;;
      esac
    fi
    if (( i < max )); then sleep 2; fi
  done
  echo "QR_TIMEOUT" >&2
  return 1
}

case "${1:-}" in
  qr-init) qr_init ;;
  qr-poll) qr_poll "${2:-90}" ;;
  qr) qr_init; qr_poll 90 ;;
  status) need_api; check_status ;;
  cookie)
    need_api
    if [[ "${2:-}" == '-' ]]; then
      cookie="$(cat)"
    elif [[ -n "${2:-}" ]]; then
      cookie="$2"
    else
      echo "用法: $0 cookie - < cookie-file（推荐 stdin）" >&2
      exit 1
    fi
    save_cookie
    check_status
    ;;
  *)
    echo "用法: $0 {qr-init|qr-poll [次数]|qr|status|cookie -}"
    exit 1
    ;;
esac

#!/usr/bin/env python3
"""
每日：网易云日推 → 匹配 Apple Music 曲库 → 在 Apple Music 资料库新建「日期」播放列表并加歌

不写本地文件；操作对象是账号云端资料库（api.music.apple.com）。
"""
from __future__ import annotations

import base64
import http.client
import json
import math
import os
import re
import socket
import subprocess
from email.utils import parsedate_to_datetime
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from ai_matching import AIMatcher, AIUnavailable


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


ROOT = Path(__file__).resolve().parent
load_env_file(ROOT / "config.env")

# 本机 ncm-api / Apple 公网均可直连；socks5h 代理会让 urllib 访问 127.0.0.1 失败
for _k in list(os.environ):
    if "proxy" in _k.lower():
        os.environ.pop(_k, None)


def _resolve_path(value: str, default: Path) -> Path:
    raw = (value or "").strip()
    if not raw:
        return default
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    return p


NCM_API_BASE = os.environ.get("NCM_API_BASE", "http://127.0.0.1:3000").rstrip("/")

COOKIE_FILE = _resolve_path(
    os.environ.get("COOKIE_FILE", ""), ROOT / "data" / "cookie.txt"
)
_am_cfg = os.environ.get("AM_CONFIG", "").strip()
AM_CONFIG = (
    _resolve_path(_am_cfg, ROOT / "secrets" / "apple-music-config.yaml")
    if _am_cfg
    else ROOT / "secrets" / "apple-music-config.yaml"
)
STOREFRONT = os.environ.get("STOREFRONT", "cn")
PLAYLIST_PREFIX = os.environ.get("PLAYLIST_PREFIX", "网易云日推")
PLAYLIST_DESCRIPTION = os.environ.get(
    "PLAYLIST_DESCRIPTION", "Auto-synced from NetEase Cloud Music daily recommend"
)
# 资料库播放列表文件夹：优先 ID，否则按名称查找/创建
PLAYLIST_FOLDER_ID = os.environ.get("PLAYLIST_FOLDER_ID", "").strip()
PLAYLIST_FOLDER_NAME = os.environ.get("PLAYLIST_FOLDER_NAME", "网易云每日推荐").strip()
SKIP_IF_EXISTS = os.environ.get("SKIP_IF_EXISTS", "1") == "1"
SKIP_UNMATCHED = os.environ.get("SKIP_UNMATCHED", "1") == "1"
LOG_DIR = _resolve_path(os.environ.get("LOG_DIR", ""), ROOT / "logs")
DATA_DIR = _resolve_path(os.environ.get("DATA_DIR", ""), ROOT / "data")
# 可选：config.env 直接写 token；否则从 AM_CONFIG yaml 读 media-user-token
MUSIC_USER_TOKEN = os.environ.get("MUSIC_USER_TOKEN", "").strip()
DEVELOPER_TOKEN = os.environ.get("DEVELOPER_TOKEN", "").strip()  # 一般留空，自动抓取
# Apple Music API 限速 / 429 重试
try:
    AM_MIN_INTERVAL = float(os.environ.get("AM_MIN_INTERVAL", "0.75") or "0.75")
except ValueError:
    AM_MIN_INTERVAL = 0.75
try:
    AM_429_RETRIES = int(os.environ.get("AM_429_RETRIES", "6") or "6")
except ValueError:
    AM_429_RETRIES = 6
try:
    AM_429_BASE_WAIT = float(os.environ.get("AM_429_BASE_WAIT", "3") or "3")
except ValueError:
    AM_429_BASE_WAIT = 3.0
# 匹配时若首条搜索已高分则不再搜其它 term
MATCH_EARLY_SCORE = float(os.environ.get("MATCH_EARLY_SCORE", "90") or "90")

TODAY = date.today().isoformat()
RUN_TS = datetime.now().strftime("%Y%m%d-%H%M%S")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class Tee:
    """Mirror stdout/stderr into the run log; shared by both entry points."""

    def __init__(self, *files):
        self.files = files

    def write(self, data):
        for f in self.files:
            f.write(data)
            f.flush()

    def flush(self):
        for f in self.files:
            f.flush()


class AuthenticationError(RuntimeError):
    """Authentication failed; callers must stop before further writes."""


class NCMAuthenticationError(AuthenticationError):
    pass


class AppleAuthenticationError(AuthenticationError):
    pass


class TransportError(RuntimeError):
    def __init__(self, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        # A write may have reached the server. Do not automatically replay it.
        self.ambiguous = ambiguous


class APIError(RuntimeError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urlopen(req: urllib.request.Request, timeout: int):
    # urllib otherwise forwards explicit Authorization / Music-User-Token headers.
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


def http_json(
    url: str,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    body: Any = None,
    timeout: int = 30,
    *,
    ambiguous: Optional[bool] = None,
) -> tuple[int, Any]:
    may_write = method.upper() not in ("GET", "HEAD", "OPTIONS") if ambiguous is None else ambiguous
    data = None if body is None else json.dumps(body).encode("utf-8")
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        with _urlopen(req, timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                result = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                raise TransportError("API 返回非 JSON 响应，请检查服务地址或网关。", ambiguous=may_write) from None
            return resp.status, result
    except urllib.error.HTTPError as e:
        # Never expose response text: gateways may echo credentials or request URLs.
        try:
            parsed = json.loads(e.read().decode("utf-8"))
        except (ValueError, UnicodeError, OSError):
            parsed = {}
        result = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("code"), int):
            result["code"] = parsed["code"]
        if e.headers and e.headers.get("Retry-After") is not None:
            result["_retry_after"] = e.headers.get("Retry-After")
        return e.code, result
    except TransportError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError, UnicodeError) as e:
        reason = "请求超时" if isinstance(e, (TimeoutError, socket.timeout)) else "网络连接失败或响应无效"
        suffix = "；写入结果未知，请先核对服务端状态，勿自动重试" if may_write else "；请检查服务地址和网络后重试"
        raise TransportError(reason + suffix, ambiguous=may_write) from None


def apple_page_path(next_url: str, base: str = "https://api.music.apple.com") -> str:
    """Validate Apple API pagination before attaching authentication headers."""
    if not isinstance(next_url, str) or not next_url:
        raise TransportError("Apple 分页地址为空或无效。")
    if any(c.isspace() or ord(c) < 32 for c in next_url) or "\\" in next_url:
        raise TransportError("Apple 分页地址无效。")
    try:
        target = urllib.parse.urlsplit(next_url)
        origin = urllib.parse.urlsplit(base)
        if (origin.scheme != "https" or origin.hostname != "api.music.apple.com"
                or origin.port not in (None, 443) or origin.username or origin.password):
            raise ValueError
        if target.scheme or target.netloc:
            if (target.scheme != "https" or target.hostname != origin.hostname
                    or target.port not in (None, 443) or target.username or target.password):
                raise ValueError
        if target.fragment or not target.path.startswith("/v1/"):
            raise ValueError
        decoded = urllib.parse.unquote(target.path)
        if "\\" in decoded or any(p in (".", "..") for p in decoded.split("/")):
            raise ValueError
    except ValueError:
        raise TransportError("Apple 分页只允许本站 /v1/ 路径或同源 HTTPS 地址。") from None
    return urllib.parse.urlunsplit(("", "", target.path, target.query, ""))


def parse_yaml_simple_value(path: Path, key: str) -> str:
    """极简 YAML 单行 key: value 读取（够用，不引 PyYAML）。"""
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"^{re.escape(key)}:\s*(.*)$", line)
        if not m:
            continue
        v = m.group(1).strip()
        if v.startswith(("'", '"')) and v.endswith(("'", '"')) and len(v) >= 2:
            v = v[1:-1]
        return v
    return ""


def get_music_user_token() -> str:
    tok = MUSIC_USER_TOKEN or parse_yaml_simple_value(AM_CONFIG, "media-user-token")
    if not tok or tok.startswith("your-"):
        raise SystemExit(
            f"缺少有效的 Music User Token。\n"
            f"请在浏览器打开 https://music.apple.com 登录后，\n"
            f"F12 → Application → Cookies → media-user-token，\n"
            f"写入 {AM_CONFIG} 的 media-user-token，或 config.env 的 MUSIC_USER_TOKEN。"
        )
    return tok


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def _unexpired_jwt(payload: dict[str, Any]) -> bool:
    exp = payload.get("exp")
    try:
        return not isinstance(exp, bool) and math.isfinite(float(exp)) and float(exp) > time.time() + 60
    except (TypeError, ValueError, OverflowError):
        return False


def _apple_web_text(url: str) -> str:
    # Public pages redirect /cn to /cn/new. Follow at most three same-origin
    # redirects manually, leaving the authenticated API opener redirect-free.
    try:
        for attempt in range(4):
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname != "music.apple.com"
                    or parsed.port not in (None, 443) or parsed.username or parsed.password
                    or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url):
                raise TransportError("Apple Music 网页重定向只允许 music.apple.com 的 HTTPS 地址。")
            try:
                # Construct each request afresh: public discovery never uses API credentials.
                with _urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), 30) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                location = e.headers.get("Location") if e.headers else None
                e.close()
                if e.code not in (301, 302, 303, 307, 308) or not location:
                    raise TransportError("无法抓取 Apple Music 网页资源，请检查网络后重试。") from None
                if attempt == 3:
                    raise TransportError("Apple Music 网页重定向次数超过上限，请稍后重试。") from None
                url = urllib.parse.urljoin(url, location)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        raise TransportError("无法抓取 Apple Music 网页资源，请检查网络后重试。") from None


def fetch_web_developer_token() -> str:
    """Extract a current web JWT; an explicitly configured token always takes precedence."""
    if DEVELOPER_TOKEN:
        if not _unexpired_jwt(_jwt_payload(DEVELOPER_TOKEN)):
            raise AppleAuthenticationError("配置的 DEVELOPER_TOKEN 无效或即将过期，请更新该配置。")
        return DEVELOPER_TOKEN
    page_url = f"https://music.apple.com/{STOREFRONT}"
    html = _apple_web_text(page_url)
    # Support hashed bundles, modulepreload links, relative paths and query strings.
    resources = re.findall(r'''(?:src|href)\s*=\s*["']([^"']+)["']''', html, re.I)
    resources += re.findall(r'''["'](/assets/[^"']+\.js(?:\?[^"']*)?)["']''', html)
    urls: list[str] = []
    for resource in resources:
        url = urllib.parse.urljoin(page_url, resource.replace("&amp;", "&"))
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme == "https" and parsed.netloc == "music.apple.com"
                and parsed.path.endswith(".js") and url not in urls):
            urls.append(url)
    if not urls:
        raise AppleAuthenticationError("Apple Music 网页未找到 JS 资源，页面结构可能已变化；可配置 DEVELOPER_TOKEN。")
    fetched = False
    for url in urls[:12]:
        try:
            js = _apple_web_text(url)
        except TransportError:
            continue
        fetched = True
        for token in re.findall(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", js):
            payload = _jwt_payload(token)
            if payload.get("iss") == "AMPWebPlay" and _unexpired_jwt(payload):
                log("已获取有效的 Apple developer token")
                return token
    if not fetched:
        raise TransportError("Apple Music JS 资源抓取失败，请检查网络后重试。")
    raise AppleAuthenticationError("Apple Music JS 中没有有效的 developer token（可能已过期），请稍后重试或配置 DEVELOPER_TOKEN。")


class AppleMusicClient:
    def __init__(self, developer_token: str, user_token: str, storefront: str = "cn"):
        self.dev = developer_token
        self.user = user_token
        self.storefront = storefront
        self.base = "https://api.music.apple.com"
        self.min_interval = min(60.0, max(0.1, AM_MIN_INTERVAL)) if math.isfinite(AM_MIN_INTERVAL) else 0.75
        self.max_429_retries = min(10, max(0, AM_429_RETRIES))
        self._last_request_at = 0.0
        self._consecutive_429 = 0
        self._developer_refresh_attempted = False
        self._configured_developer_token = bool(DEVELOPER_TOKEN)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.dev}",
            "Music-User-Token": self.user,
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
            "User-Agent": UA,
        }

    def _pace(self) -> None:
        """请求间最小间隔，减轻 429。"""
        gap = self.min_interval
        # 连续撞限后临时拉大间隔
        if self._consecutive_429 > 0:
            gap = max(gap, min(5.0, self.min_interval * (1.0 + 0.5 * self._consecutive_429)))
        elapsed = time.time() - self._last_request_at
        if elapsed < gap:
            time.sleep(gap - elapsed)

    @staticmethod
    def _retry_after_seconds(data: Any, attempt: int) -> float:
        ra = None
        if isinstance(data, dict):
            ra = data.get("_retry_after")
        if ra is not None:
            try:
                seconds = float(str(ra).strip())
            except (TypeError, ValueError, OverflowError):
                try:
                    seconds = parsedate_to_datetime(str(ra)).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    seconds = -1.0
            if math.isfinite(seconds) and seconds >= 0:
                return min(60.0, seconds)
        base_wait = AM_429_BASE_WAIT if math.isfinite(AM_429_BASE_WAIT) and AM_429_BASE_WAIT >= 0 else 3.0
        base = base_wait * (2 ** min(10, max(0, attempt)))
        jitter = 0.25 * (min(10, max(0, attempt)) + 1)
        return min(60.0, base + jitter)

    def request(
        self, path: str, method: str = "GET", body: Any = None, timeout: int = 45
    ) -> tuple[int, Any]:
        path = apple_page_path(path, self.base)
        method = method.upper()
        last_code = 0
        last_data: Any = {}
        for attempt in range(self.max_429_retries + 1):
            self._pace()
            code, data = http_json(
                self.base + path,
                method=method,
                headers=self._headers(),
                body=body,
                timeout=timeout,
            )
            self._last_request_at = time.time()
            if code in (401, 403):
                if (method in ("GET", "HEAD") and not self._configured_developer_token
                        and not self._developer_refresh_attempted):
                    self._developer_refresh_attempted = True
                    self.dev = fetch_web_developer_token()
                    return self.request(path, method=method, body=body, timeout=timeout)
                configured = "请更新配置的 DEVELOPER_TOKEN，并" if self._configured_developer_token else "developer token 刷新后仍无权限，请"
                raise AppleAuthenticationError(f"Apple Music 认证失败 (HTTP {code})；{configured}检查 Music User Token / media-user-token 和订阅权限。")
            if 300 <= code < 400:
                raise TransportError("Apple API 重定向已拒绝；请检查服务地址。")
            if not isinstance(data, dict):
                raise TransportError("Apple API 返回无效 JSON 结构。", ambiguous=method not in ("GET", "HEAD"))
            last_code, last_data = code, data
            if code != 429:
                if 200 <= code < 300:
                    self._consecutive_429 = 0
                return code, data
            self._consecutive_429 += 1
            if attempt >= self.max_429_retries:
                break
            wait = self._retry_after_seconds(data, attempt)
            log(
                f"  Apple API 429，{wait:.1f}s 后重试 "
                f"({attempt + 1}/{self.max_429_retries}) {path[:80]}"
            )
            time.sleep(wait)
            # 撞限后略提高全局间隔，后续请求更慢
            self.min_interval = min(3.0, self.min_interval * 1.25)
        return last_code, last_data

    def storefront_info(self) -> dict[str, Any]:
        code, data = self.request("/v1/me/storefront")
        if code != 200:
            raise APIError(f"Apple Music storefront 读取失败 (HTTP {code})。", code)
        return data

    def search_songs(self, term: str, limit: int = 10) -> list[dict[str, Any]]:
        qs = urllib.parse.urlencode(
            {"term": term, "types": "songs", "limit": str(limit)}
        )
        code, data = self.request(
            f"/v1/catalog/{self.storefront}/search?{qs}"
        )
        if code != 200:
            raise APIError(f"Apple Music 搜索失败 (HTTP {code})；请稍后重试。", code)
        return list(data.get("results", {}).get("songs", {}).get("data") or [])

    def list_library_playlists(self, limit: int = 100) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        path: Optional[str] = f"/v1/me/library/playlists?limit={min(limit, 100)}"
        while path and len(out) < limit:
            code, data = self.request(path)
            if code != 200:
                raise APIError(f"列出 Apple Music 播放列表失败 (HTTP {code})。", code)
            out.extend(data.get("data") or [])
            nxt = data.get("next") or ""
            path = apple_page_path(nxt, self.base) if nxt else None
        return out

    def find_playlist_by_name(self, name: str) -> Optional[dict[str, Any]]:
        for pl in self.list_library_playlists(limit=200):
            if (pl.get("attributes") or {}).get("name") == name:
                return pl
        return None

    def list_playlist_folders(self, limit: int = 100) -> list[dict[str, Any]]:
        code, data = self.request(
            f"/v1/me/library/playlist-folders?limit={min(limit, 100)}"
        )
        if code != 200:
            raise APIError(f"列出 Apple Music 播放列表文件夹失败 (HTTP {code})。", code)
        return list(data.get("data") or [])

    def find_folder_by_name(self, name: str) -> Optional[dict[str, Any]]:
        for folder in self.list_playlist_folders(limit=100):
            if (folder.get("attributes") or {}).get("name") == name:
                return folder
        return None

    def create_playlist_folder(self, name: str) -> dict[str, Any]:
        body = {"attributes": {"name": name}}
        code, data = self.request(
            "/v1/me/library/playlist-folders", method="POST", body=body
        )
        if code not in (200, 201):
            raise APIError(f"创建 Apple Music 播放列表文件夹失败 (HTTP {code})。", code)
        items = data.get("data") or []
        if not items:
            raise TransportError("创建 Apple Music 文件夹后未返回 data；请核对资料库后重试。", ambiguous=True)
        return items[0]

    def resolve_playlist_folder(self) -> Optional[dict[str, Any]]:
        """
        解析目标文件夹：PLAYLIST_FOLDER_ID > 按名称查找 > 按名称创建。
        返回 None 表示不放进文件夹（根目录）。
        """
        if PLAYLIST_FOLDER_ID:
            code, data = self.request(
                f"/v1/me/library/playlist-folders/{PLAYLIST_FOLDER_ID}"
            )
            if code == 200 and (data.get("data") or []):
                return (data.get("data") or [None])[0]
            log(f"警告: 配置的文件夹 ID 无效: {PLAYLIST_FOLDER_ID}")
        if not PLAYLIST_FOLDER_NAME:
            return None
        found = self.find_folder_by_name(PLAYLIST_FOLDER_NAME)
        if found:
            return found
        log(f"资料库无文件夹「{PLAYLIST_FOLDER_NAME}」，正在创建…")
        return self.create_playlist_folder(PLAYLIST_FOLDER_NAME)

    def folder_children(self, folder_id: str) -> list[dict[str, Any]]:
        code, data = self.request(
            f"/v1/me/library/playlist-folders/{folder_id}/children?limit=100"
        )
        if code != 200:
            return []
        return list(data.get("data") or [])

    def find_playlist_in_folder(
        self, folder_id: str, name: str
    ) -> Optional[dict[str, Any]]:
        for item in self.folder_children(folder_id):
            if item.get("type") != "library-playlists":
                continue
            if (item.get("attributes") or {}).get("name") == name:
                return item
        return None

    def create_playlist(
        self,
        name: str,
        description: str,
        track_ids: list[str],
        parent_folder_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        track_ids: catalog song ids（纯数字字符串）。
        parent_folder_id: 资料库播放列表文件夹 id，写入 relationships.parent。
        注意：parent 与 tracks 可同时传；若 parent 被忽略则先建空列表再补歌。
        """
        body: dict[str, Any] = {
            "attributes": {
                "name": name,
                "description": description,
            }
        }
        rel: dict[str, Any] = {}
        if parent_folder_id:
            rel["parent"] = {
                "data": [
                    {
                        "id": parent_folder_id,
                        "type": "library-playlist-folders",
                    }
                ]
            }
        if track_ids:
            rel["tracks"] = {
                "data": [{"id": tid, "type": "songs"} for tid in track_ids]
            }
        if rel:
            body["relationships"] = rel

        code, data = self.request("/v1/me/library/playlists", method="POST", body=body)
        if code not in (200, 201):
            # 回退：仅 parent 建空列表，再 add_tracks（部分环境同时带 tracks 时 parent 不生效）
            if code in (400, 422) and parent_folder_id and track_ids:
                log("创建时 parent+tracks 参数被拒绝，改为先建空列表再加歌…")
                body2: dict[str, Any] = {
                    "attributes": {"name": name, "description": description},
                    "relationships": {
                        "parent": {
                            "data": [
                                {
                                    "id": parent_folder_id,
                                    "type": "library-playlist-folders",
                                }
                            ]
                        }
                    },
                }
                code, data = self.request(
                    "/v1/me/library/playlists", method="POST", body=body2
                )
                if code not in (200, 201):
                    raise APIError(f"创建 Apple Music 播放列表失败 (HTTP {code})。", code)
                items = data.get("data") or []
                if not items:
                    raise TransportError("创建 Apple Music 播放列表后未返回 data；请核对资料库后重试。", ambiguous=True)
                pl = items[0]
                self.add_tracks(str(pl.get("id")), track_ids)
                return pl
            raise APIError(f"创建 Apple Music 播放列表失败 (HTTP {code})。", code)
        items = data.get("data") or []
        if not items:
            raise TransportError("创建 Apple Music 播放列表后未返回 data；请核对资料库后重试。", ambiguous=True)
        pl = items[0]
        # 若创建时带了 tracks 但实际为空，补加
        if track_ids and parent_folder_id:
            time.sleep(0.5)
            pid = str(pl.get("id"))
            code_t, detail = self.request(
                f"/v1/me/library/playlists/{pid}/tracks?limit=5"
            )
            have = len((detail.get("data") or [])) if code_t == 200 else -1
            if have == 0:
                log("创建后曲目为空，正在补加…")
                self.add_tracks(pid, track_ids)
        return pl

    def add_tracks(self, playlist_id: str, track_ids: list[str]) -> None:
        if not track_ids:
            return
        # 分批，避免请求过大
        for i in range(0, len(track_ids), 25):
            batch = track_ids[i : i + 25]
            body = {"data": [{"id": tid, "type": "songs"} for tid in batch]}
            code, data = self.request(
                f"/v1/me/library/playlists/{playlist_id}/tracks",
                method="POST",
                body=body,
            )
            if code not in (200, 201, 204):
                raise APIError(f"添加 Apple Music 曲目失败 (HTTP {code})。", code)
            time.sleep(0.3)


def ncm_get(path: str, cookie: str, extra: Optional[dict[str, str]] = None) -> Any:
    params = dict(extra or {})
    params.pop("cookie", None)
    params["timestamp"] = str(int(time.time() * 1000))
    # Legacy name retained for callers; POST carries the cookie without URL leakage.
    url = f"{NCM_API_BASE}{path}?{urllib.parse.urlencode(params)}"
    is_mutation = path.split("?", 1)[0].rstrip("/") in ("/like", "/scrobble")
    code, data = http_json(url, method="POST", body={"cookie": cookie}, ambiguous=is_mutation)
    if not isinstance(data, dict):
        raise TransportError("网易云 API 返回无效 JSON 结构。", ambiguous=is_mutation)
    nested = data.get("data")
    api_codes = [data.get("code")]
    if isinstance(nested, dict):
        api_codes.append(nested.get("code"))
    if code in (301, 302, 401, 403) or any(c in (301, 302, 401, 403) for c in api_codes):
        raise NCMAuthenticationError("网易云登录已失效或无权限，请运行 login.sh qr 更新 cookie 后重试。")
    if code == 429 or (is_mutation and code == 405 and data.get("code") == 405):
        # Preserve explicit NCM rate limits for caller backoff/stop handling.
        return {"code": code}
    if is_mutation and 500 <= code < 600:
        # A gateway/server failure does not prove that the upstream write failed.
        raise TransportError(f"网易云 API HTTP {code}；写入结果未知，请先核对服务端状态，勿自动重试。", ambiguous=True)
    if code != 200:
        raise APIError(f"网易云 API HTTP {code}；请检查服务状态后重试。", code)
    if path == "/login/status":
        profile = nested.get("profile") if isinstance(nested, dict) else data.get("profile")
        if not isinstance(profile, dict) or not profile.get("userId"):
            raise NCMAuthenticationError("网易云登录状态无有效账号，请运行 login.sh qr 更新 cookie 后重试。")
    if data.get("code") not in (200, None):
        # Business failures may echo request credentials, too. Preserve only the code.
        api_code = data.get("code")
        return {"code": api_code} if isinstance(api_code, int) else {}
    if is_mutation and data.get("code") != 200:
        return {}
    return data


def load_cookie() -> str:
    if not COOKIE_FILE.is_file():
        raise SystemExit(f"缺少网易云 cookie: {COOKIE_FILE}\n请运行: {ROOT}/login.sh qr")
    cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
    if not cookie:
        raise SystemExit(f"cookie 为空: {COOKIE_FILE}")
    return cookie


def ncm_alive() -> bool:
    try:
        code, data = http_json(f"{NCM_API_BASE}/banner?type=0", timeout=2)
        return code == 200 and isinstance(data, dict)
    except (TransportError, APIError):
        return False


def ensure_ncm_api() -> None:
    if ncm_alive():
        return
    env = {
        key: value for key, value in os.environ.items()
        if not any(part in key.upper() for part in ("APPLE", "MUSIC_USER_TOKEN", "DEVELOPER_TOKEN", "AM_CONFIG", "MEDIA_USER_TOKEN", "AI_MATCHING"))
    }
    start_cmd = os.environ.get("NCM_API_START_CMD", "").strip()
    if not start_cmd:
        raise SystemExit("NCM API 未就绪；请启动 api-enhanced，或配置 NCM_API_START_CMD 为启动后台服务并及时返回的命令。")
    log("NCM API 未就绪，执行配置的后台启动命令…")
    command = ["bash", "-c", start_cmd]
    try:
        result = subprocess.run(
            command, cwd=str(ROOT), check=False, env=env, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("NCM API 启动命令超过 30 秒；请确保命令启动后台服务并及时返回。") from None
    except OSError:
        raise SystemExit("无法执行 NCM API 启动命令；请检查命令路径和依赖。") from None
    if result.returncode:
        raise SystemExit(f"NCM API 启动失败 (退出码 {result.returncode})；请检查服务日志和启动配置。")
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if ncm_alive():
            log("NCM API 已就绪")
            return
        time.sleep(1)
    raise SystemExit("NCM API 在 40 秒内未就绪；请检查 NCM_API_BASE、监听端口和服务日志。")


def artists_str(ar: list[Any]) -> str:
    names = []
    for a in ar or []:
        if isinstance(a, dict) and a.get("name"):
            names.append(str(a["name"]))
        elif isinstance(a, str):
            names.append(a)
    return " / ".join(names)


@dataclass
class NeteaseSong:
    id: int
    name: str
    artists: str
    album: str
    duration_ms: int = 0

    @property
    def search_term(self) -> str:
        primary = self.artists.split(" / ")[0] if self.artists else ""
        return f"{primary} {self.name}".strip()


def fetch_daily_songs(cookie: str) -> list[NeteaseSong]:
    body = ncm_get("/recommend/songs", cookie)
    if body.get("code") != 200:
        raise APIError("网易云每日推荐读取失败；请检查 API 服务和账号状态。", int(body.get("code") or 0))
    data = body.get("data") or body
    daily = data.get("dailySongs") or data.get("recommend") or []
    songs: list[NeteaseSong] = []
    for item in daily:
        song = item.get("song") if isinstance(item.get("song"), dict) else item
        name = str(song.get("name") or "").strip()
        if not name:
            continue
        ar = song.get("ar") or song.get("artists") or []
        al = song.get("al") or song.get("album") or {}
        album = al.get("name", "") if isinstance(al, dict) else str(al or "")
        songs.append(
            NeteaseSong(
                id=int(song.get("id") or 0),
                name=name,
                artists=artists_str(ar),
                album=str(album or ""),
                duration_ms=int(song.get("dt") or song.get("duration") or 0),
            )
        )
    return songs


def normalize_title(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\(（\[【].*?[\)）\]】]", "", s)
    s = re.sub(r"[·・\.\,\!\?\:\;\'\"“”‘’]", "", s)
    return s.strip()


def version_tags(title: str) -> set[str]:
    """Keep recording qualifiers before normalize_title removes brackets."""
    text = title.casefold()
    patterns = {
        "instrumental": r"\b(?:karaoke|instrumental|off[\s-]*vocal)\b|カラオケ|インスト(?:ゥルメンタル)?|伴奏|无人声|無人声",
        "live": r"\blive\b|ライブ|现场版|現場版",
        "remix": r"\bremix\b|リミックス|混音版",
        "acoustic": r"\bacoustic\b|アコースティック|不插电|不插電",
        "solo": r"\bsolo\b|ソロ|独唱|獨唱",
        "game": r"\bgame[\s-]+(?:version|ver\.?)\b|ゲーム[\s・]*(?:バージョン|サイズ)",
        "short": r"\b(?:tv[\s-]*(?:size|version|ver\.?)|short[\s-]*(?:version|ver\.?))\b|ショート|电视版|電視版",
        "rerecording": r"\bre[\s-]*record(?:ing|ed)\b|再録|再录",
        "tv": r"\btaylor[’']s version\b",
    }
    # Qualifiers belong to brackets or a version suffix, not ordinary lyrics
    # such as "I Live For You" or the Japanese title "ショートケーキ".
    fragments = re.findall(r"[\(（\[【]([^\)）\]】]*)[\)）\]】]", text)
    suffix = re.search(r"\s[-–—]\s+(.+)$", text)
    if suffix:
        fragments.append(suffix.group(1))
    suffix = re.search(
        r"\s((?:karaoke|instrumental|off[\s-]*vocal|remix|acoustic|solo)"
        r"(?:\s+(?:version|ver\.?))?|live\s+(?:version|ver\.?)|"
        r"伴奏(?:版)?|カラオケ|インスト(?:ゥルメンタル)?|现场版|現場版|ライブ版)$", text)
    if suffix:
        fragments.append(suffix.group(1))
    return {tag for tag, pattern in patterns.items()
            if any(re.search(pattern, fragment) for fragment in fragments)}


def versions_compatible(source: str, candidate: str) -> bool:
    return version_tags(source) == version_tags(candidate)


def score_match(ns: NeteaseSong, song: dict[str, Any]) -> float:
    attrs = song.get("attributes") or {}
    if not versions_compatible(ns.name, str(attrs.get("name") or "")):
        return 0.0
    t = normalize_title(str(attrs.get("name") or ""))
    a = normalize_title(str(attrs.get("artistName") or ""))
    al = normalize_title(str(attrs.get("albumName") or ""))
    nt = normalize_title(ns.name)
    na = normalize_title(ns.artists.split(" / ")[0] if ns.artists else "")
    nal = normalize_title(ns.album)
    if not t or not nt:
        return 0.0
    score = 0.0
    if t == nt:
        score += 50
    elif nt in t or t in nt:
        score += 30
    else:
        score += min(20, len(set(t) & set(nt)) * 2)
    if na:
        if a == na:
            score += 40
        elif na in a or a in na:
            score += 25
        else:
            for part in re.split(r"[,/&]| featuring | feat\.| ft\.", a):
                part = part.strip()
                if part and (part == na or na in part or part in na):
                    score += 20
                    break
    if nal and al:
        if al == nal:
            score += 10
        elif nal in al or al in nal:
            score += 5
    dur = int(attrs.get("durationInMillis") or 0)
    if ns.duration_ms and dur:
        diff = abs(ns.duration_ms - dur)
        if diff <= 3000:
            score += 8
        elif diff <= 8000:
            score += 4
    return score


def _strong_catalog_match(ns: NeteaseSong, song: dict[str, Any]) -> bool:
    attrs = song.get("attributes") or {}
    artist = normalize_title(str(attrs.get("artistName") or ""))
    primary = normalize_title(ns.artists.split(" / ")[0])
    return (score_match(ns, song) >= max(90, MATCH_EARLY_SCORE)
            and normalize_title(ns.name) == normalize_title(str(attrs.get("name") or ""))
            and bool(primary) and artist == primary)


def _catalog_metadata(song: dict[str, Any]) -> dict[str, Any]:
    attrs = song.get("attributes") or {}
    return {"id": str(song["id"]), "name": attrs.get("name") or "",
            "artists": attrs.get("artistName") or "", "album": attrs.get("albumName") or "",
            "duration_ms": attrs.get("durationInMillis") or 0, "isrc": attrs.get("isrc") or ""}


def match_catalog(
    am: AppleMusicClient, ns: NeteaseSong, *, matcher: Optional[AIMatcher] = None,
) -> Optional[dict[str, Any]]:
    # A caller that processes a batch owns one matcher, sharing budget and cache.
    if matcher is None:
        matcher = AIMatcher.from_env(ROOT)
    use_ai = matcher.enabled
    terms = list(dict.fromkeys(term.strip() for term in (
        ns.search_term,
        f"{ns.name} {ns.artists.split(' / ')[0]}" if ns.artists else ns.name,
        ns.name,
    ) if term.strip()))
    candidates: dict[str, dict[str, Any]] = {}

    def consider(batch: list[dict[str, Any]]) -> None:
        for song in batch:
            if not isinstance(song, dict):
                continue
            attrs = song.get("attributes")
            if not isinstance(attrs, dict) or not all(isinstance(attrs.get(k), str)
                    for k in ("name", "artistName", "albumName")) or not attrs["name"].strip():
                continue
            duration = attrs.get("durationInMillis") or 0
            if type(duration) is not int or not 0 <= duration <= 86400000:
                continue
            if use_ai and ns.duration_ms and duration and abs(ns.duration_ms - duration) > 15000:
                continue
            sid = str(song.get("id") or "")
            if sid.isdigit() and versions_compatible(ns.name, attrs["name"]):
                candidates.setdefault(sid, song)

    def ranked() -> list[dict[str, Any]]:
        return sorted(candidates.values(), key=lambda s: (
            score_match(ns, s),
            str((s.get("attributes") or {}).get("name") or "").casefold() == ns.name.casefold(),
        ), reverse=True)

    for i, term in enumerate(terms):
        batch = am.search_songs(term, limit=25 if use_ai else 10)
        consider(batch)
        ordered = ranked()
        best = ordered[0] if ordered else None
        if best:
            if use_ai and _strong_catalog_match(ns, best):
                break
            if not use_ai and (score_match(ns, best) >= MATCH_EARLY_SCORE
                               or (i >= 1 and score_match(ns, best) >= 70)):
                break
        if not batch and am._consecutive_429 > 0:
            time.sleep(min(8.0, 1.5 * am._consecutive_429))

    ordered = ranked()
    song = ordered[0] if ordered else None
    method = "rules"
    reason = ""
    if use_ai and (song is None or not _strong_catalog_match(ns, song)):
        source = {"id": str(ns.id), "name": ns.name, "artists": ns.artists,
                  "album": ns.album, "duration_ms": ns.duration_ms}
        # Only eligible, bounded real candidates can be selected, even by AI.
        def eligible() -> list[dict[str, Any]]:
            result = []
            for candidate in ranked():
                metadata = _catalog_metadata(candidate)
                duration = metadata["duration_ms"]
                if ns.duration_ms and duration and abs(ns.duration_ms - duration) > 15000:
                    continue
                result.append(metadata)
            return result[:30]

        try:
            supplied = eligible()
            decision = matcher.select(source, supplied)
            if decision.search_terms:
                for term in decision.search_terms:
                    if term not in terms:
                        consider(am.search_songs(term, limit=25))
                supplied = eligible()
                decision = matcher.select(source, supplied, allow_search=False)
            allowed = {s["id"] for s in supplied}
            if decision.candidate_id is None:
                log("  AI 未确认可靠匹配")
                return None
            if decision.candidate_id not in allowed:
                raise AIUnavailable("AI 返回无效候选")
            song = candidates[decision.candidate_id]
            method = "ai"
            reason = getattr(decision, "reason", "")
        except AIUnavailable:
            log("  AI 辅助匹配不可用，本曲保留为未匹配")
            return None
    elif song is None or score_match(ns, song) < 55:
        return None
    attrs = song.get("attributes") or {}
    return {
        "id": str(song["id"]), "name": attrs.get("name"),
        "artistName": attrs.get("artistName"), "albumName": attrs.get("albumName"),
        "url": attrs.get("url"), "score": score_match(ns, song),
        "match_method": method,
        **({"ai_reason": reason} if method == "ai" else {}),
    }


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"daily-{TODAY}.log"
    log_fp = open(log_file, "a", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log_fp)  # type: ignore
    sys.stderr = Tee(sys.__stderr__, log_fp)  # type: ignore

    log(f"=== 网易云日推 → Apple Music 资料库 {TODAY} ({RUN_TS}) ===")
    ensure_ncm_api()
    cookie = load_cookie()

    st = ncm_get("/login/status", cookie)
    profile = (st.get("data") or {}).get("profile") or st.get("profile") or {}
    log(f"网易云账号: {profile.get('nickname') or '?'}")

    songs = fetch_daily_songs(cookie)
    log(f"日推曲目数: {len(songs)}")
    if not songs:
        log("今日无推荐，结束")
        return 1

    user_token = get_music_user_token()
    dev_token = fetch_web_developer_token()
    am = AppleMusicClient(dev_token, user_token, STOREFRONT)

    sf = am.storefront_info()
    sf_id = (sf.get("data") or [{}])[0].get("id")
    log(f"Apple Music storefront: {sf_id}")

    folder = am.resolve_playlist_folder()
    folder_id = str(folder.get("id")) if folder else None
    folder_name = (
        (folder.get("attributes") or {}).get("name") if folder else None
    ) or PLAYLIST_FOLDER_NAME or ""
    if folder_id:
        log(f"目标文件夹: 「{folder_name}」(id={folder_id})")
    else:
        log("未配置文件夹，播放列表将建在资料库根目录")

    playlist_name = f"{PLAYLIST_PREFIX}-{TODAY}"
    existing = None
    if folder_id:
        existing = am.find_playlist_in_folder(folder_id, playlist_name)
    if not existing:
        existing = am.find_playlist_by_name(playlist_name)
    if existing and SKIP_IF_EXISTS:
        log(f"资料库已存在播放列表「{playlist_name}」(id={existing.get('id')})，跳过")
        man = {
            "date": TODAY,
            "skipped": True,
            "folder": {"id": folder_id, "name": folder_name} if folder_id else None,
            "playlist": {
                "id": existing.get("id"),
                "name": playlist_name,
                "href": (existing.get("attributes") or {}).get("url"),
            },
        }
        (DATA_DIR / f"manifest-{TODAY}.json").write_text(
            json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0

    matcher = AIMatcher.from_env(ROOT)
    results: list[dict[str, Any]] = []
    track_ids: list[str] = []
    for i, ns in enumerate(songs, 1):
        log(f"[{i}/{len(songs)}] {ns.artists} - {ns.name}")
        m = match_catalog(am, ns, matcher=matcher)
        row: dict[str, Any] = {
            "netease": asdict(ns),
            "match": m,
        }
        if not m:
            log("  未匹配")
            row["status"] = "unmatched"
            results.append(row)
            if not SKIP_UNMATCHED:
                return 2
            continue
        log(
            f"  → {m['artistName']} - {m['name']} [{m['albumName']}] "
            f"id={m['id']} score={m['score']:.0f}"
        )
        row["status"] = "matched"
        track_ids.append(m["id"])
        results.append(row)

    # 去重保序
    seen_ids: set[str] = set()
    unique_ids: list[str] = []
    for tid in track_ids:
        if tid not in seen_ids:
            seen_ids.add(tid)
            unique_ids.append(tid)

    where = f"文件夹「{folder_name}」" if folder_id else "资料库根目录"
    log(
        f"匹配成功 {len(unique_ids)}/{len(songs)}，"
        f"在{where}创建播放列表「{playlist_name}」…"
    )
    desc = f"{PLAYLIST_DESCRIPTION} | {TODAY} | {len(unique_ids)} 首"
    pl = am.create_playlist(
        playlist_name, desc, unique_ids, parent_folder_id=folder_id
    )
    pl_id = pl.get("id")
    log(f"已创建: id={pl_id} name={(pl.get('attributes') or {}).get('name')}")
    if folder_id:
        in_folder = None
        for wait in (0.5, 1.5, 3.0):
            time.sleep(wait)
            in_folder = am.find_playlist_in_folder(folder_id, playlist_name)
            if in_folder:
                break
        if in_folder:
            log(f"已确认位于文件夹「{folder_name}」内")
        else:
            log(
                f"警告: 创建后暂未在文件夹「{folder_name}」中索引到该列表 "
                "（偶发延迟）；请稍后在 App 中刷新确认。"
                "若仍在根目录，可手动拖入（API 无法移动已有列表）。"
            )
    # 若创建时未带上全部 tracks（部分环境忽略 relationships），再 POST 一次
    if unique_ids:
        try:
            # 仅当创建 body 可能被忽略时补加；重复添加可能导致重复曲目，故先读 tracks 数
            code, detail = am.request(
                f"/v1/me/library/playlists/{pl_id}/tracks?limit=100"
            )
            have = len((detail.get("data") or [])) if code == 200 else -1
            log(f"播放列表当前曲目数: {have}")
            if have == 0 and unique_ids:
                log("创建时未写入曲目，正在追加…")
                am.add_tracks(str(pl_id), unique_ids)
                code, detail = am.request(
                    f"/v1/me/library/playlists/{pl_id}/tracks?limit=100"
                )
                have = len((detail.get("data") or [])) if code == 200 else -1
                log(f"追加后曲目数: {have}")
        except (AuthenticationError, TransportError):
            raise
        except Exception:
            log("校验/补加曲目失败；播放列表可能已含歌，请在 App 中核对。")

    man = {
        "date": TODAY,
        "run_ts": RUN_TS,
        "folder": {"id": folder_id, "name": folder_name} if folder_id else None,
        "playlist": {
            "id": pl_id,
            "name": playlist_name,
            "attributes": pl.get("attributes"),
        },
        "stats": {
            "total": len(songs),
            "matched": len(unique_ids),
            "unmatched": sum(1 for r in results if r.get("status") == "unmatched"),
        },
        "tracks": results,
    }
    man_path = DATA_DIR / f"manifest-{TODAY}.json"
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"清单: {man_path}")
    log(f"统计: {json.dumps(man['stats'], ensure_ascii=False)}")
    if folder_name:
        log(
            f"请在「音乐」App → 资料库 → 播放列表 → 文件夹「{folder_name}」中查看"
        )
    else:
        log("请在 iPhone / Mac「音乐」App → 资料库 → 播放列表 中查看")
    log("=== 完成 ===")
    return 0 if unique_ids else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AuthenticationError, TransportError, APIError, AIUnavailable) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("中断", file=sys.stderr)
        raise SystemExit(130)

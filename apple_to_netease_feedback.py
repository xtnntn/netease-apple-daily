#!/usr/bin/env python3
"""
Apple Music → 网易云 反馈回写

1. 喜爱歌曲（Favorite Songs / 喜爱歌曲）→ 网易云红心 /like
2. 最近播放 recent/played/tracks 增量 → 网易云听歌打卡 /scrobble

默认会真实写入；请先用 --dry-run 看匹配结果。
状态与去重：data/feedback-state.json（gitignore）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 复用日推脚本的配置加载、HTTP、Apple/NCM 客户端
import daily_recommend as dr


# ---------------------------------------------------------------------------
# 配置（config.env 可覆盖）
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = (os.environ.get(name) or "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


FEEDBACK_SYNC_LIKES = _env_bool("FEEDBACK_SYNC_LIKES", True)
FEEDBACK_SYNC_SCROBBLE = _env_bool("FEEDBACK_SYNC_SCROBBLE", True)
FEEDBACK_DRY_RUN = _env_bool("FEEDBACK_DRY_RUN", False)

FEEDBACK_FAVORITES_PLAYLIST_ID = os.environ.get(
    "FEEDBACK_FAVORITES_PLAYLIST_ID", ""
).strip()
FEEDBACK_FAVORITES_PLAYLIST_NAME = os.environ.get(
    "FEEDBACK_FAVORITES_PLAYLIST_NAME", "喜爱歌曲"
).strip()

FEEDBACK_RECENT_LIMIT = _env_int("FEEDBACK_RECENT_LIMIT", 50)
FEEDBACK_MAX_LIKES_PER_RUN = _env_int("FEEDBACK_MAX_LIKES_PER_RUN", 40)
FEEDBACK_MAX_SCROBBLE_PER_RUN = _env_int("FEEDBACK_MAX_SCROBBLE_PER_RUN", 25)
# 本轮最多对多少首喜爱做反查（含已红心/未匹配），防止全库 400+ 首拖很久
FEEDBACK_MAX_LIKE_SCAN_PER_RUN = _env_int("FEEDBACK_MAX_LIKE_SCAN_PER_RUN", 80)
FEEDBACK_MIN_SCORE = float(os.environ.get("FEEDBACK_MIN_SCORE", "55") or "55")
FEEDBACK_SCROBBLE_TIME = _env_int("FEEDBACK_SCROBBLE_TIME", 240)
# 红心接口易 405，默认稍慢
FEEDBACK_REQUEST_SLEEP = float(os.environ.get("FEEDBACK_REQUEST_SLEEP", "1.2") or "1.2")
FEEDBACK_LIKE_SLEEP = float(os.environ.get("FEEDBACK_LIKE_SLEEP", "2.0") or "2.0")
# 首次运行只做 recent 快照、不 scrobble（避免把整窗历史一次打卡）
FEEDBACK_SCROBBLE_SEED_ONLY_FIRST = _env_bool("FEEDBACK_SCROBBLE_SEED_ONLY_FIRST", True)
# 红心是否使用 AI 辅助匹配（还需 AI_MATCHING_ENABLED=1）；打卡始终只用规则
FEEDBACK_AI_MATCHING = _env_bool("FEEDBACK_AI_MATCHING", False)

STATE_FILE = dr._resolve_path(
    os.environ.get("FEEDBACK_STATE_FILE", ""),
    dr.DATA_DIR / "feedback-state.json",
)
MANIFEST_PREFIX = "feedback-manifest"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class AppleSong:
    apple_id: str  # catalog id（数字字符串）
    name: str
    artists: str
    album: str
    duration_ms: int = 0
    isrc: str = ""
    library_id: str = ""  # i.xxx 若来自资料库

    @property
    def search_term(self) -> str:
        primary = self.artists.split(",")[0].split("&")[0].strip()
        primary = re.split(r"\s+feat\.?\s+", primary, flags=re.I)[0].strip()
        return f"{primary} {self.name}".strip()


def log(msg: str) -> None:
    dr.log(msg)


# ---------------------------------------------------------------------------
# Apple Music 读取
# ---------------------------------------------------------------------------

def am_paginate(
    am: dr.AppleMusicClient, first_path: str, limit: int = 500
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    path: Optional[str] = first_path
    while path and len(out) < limit:
        code, data = am.request(path)
        if code != 200:
            raise RuntimeError(f"Apple 分页读取失败 HTTP {code}")
        batch = data.get("data") or []
        out.extend(batch)
        nxt = data.get("next") or ""
        path = dr.apple_page_path(nxt, am.base) if nxt else None
        if path:
            time.sleep(0.15)
    return out[:limit]


def apple_resource_to_song(item: dict[str, Any]) -> Optional[AppleSong]:
    attrs = item.get("attributes") or {}
    name = str(attrs.get("name") or "").strip()
    if not name:
        return None
    pp = attrs.get("playParams") or {}
    catalog_id = str(
        pp.get("catalogId") or pp.get("reportingId") or item.get("id") or ""
    ).strip()
    # library id 形如 i.xxx，catalog 应为纯数字
    library_id = ""
    raw_id = str(item.get("id") or "")
    if raw_id.startswith("i.") or item.get("type") == "library-songs":
        library_id = raw_id
        if not catalog_id.isdigit():
            catalog_id = str(pp.get("catalogId") or pp.get("reportingId") or "")
    if not catalog_id or not catalog_id.replace("-", "").isalnum():
        # 仍可用 library 信息搜，apple_id 用 library_id 兜底
        catalog_id = catalog_id or library_id or raw_id
    return AppleSong(
        apple_id=catalog_id,
        name=name,
        artists=str(attrs.get("artistName") or "").strip(),
        album=str(attrs.get("albumName") or "").strip(),
        duration_ms=int(attrs.get("durationInMillis") or 0),
        isrc=str(attrs.get("isrc") or "").strip(),
        library_id=library_id,
    )


def find_favorites_playlist(am: dr.AppleMusicClient) -> dict[str, Any]:
    if FEEDBACK_FAVORITES_PLAYLIST_ID:
        code, data = am.request(
            f"/v1/me/library/playlists/{FEEDBACK_FAVORITES_PLAYLIST_ID}"
        )
        if code == 200 and (data.get("data") or []):
            return (data.get("data") or [None])[0]
        raise SystemExit(
            f"FEEDBACK_FAVORITES_PLAYLIST_ID 无效: {FEEDBACK_FAVORITES_PLAYLIST_ID}"
        )

    candidates_names = []
    if FEEDBACK_FAVORITES_PLAYLIST_NAME:
        candidates_names.append(FEEDBACK_FAVORITES_PLAYLIST_NAME)
    candidates_names.extend(
        [
            "喜爱歌曲",
            "Favorite Songs",
            "Favourites",
            "Favorites",
            "喜欢的音乐",
        ]
    )
    # 去重保序
    seen: set[str] = set()
    names: list[str] = []
    for n in candidates_names:
        if n and n not in seen:
            seen.add(n)
            names.append(n)

    playlists = am_paginate(am, "/v1/me/library/playlists?limit=100", limit=300)
    # 优先精确名 + canEdit=false（系统喜爱列表）
    exact: list[dict[str, Any]] = []
    fuzzy: list[dict[str, Any]] = []
    for pl in playlists:
        attrs = pl.get("attributes") or {}
        name = str(attrs.get("name") or "")
        if name in names:
            exact.append(pl)
        else:
            low = name.lower()
            if any(
                k in low
                for k in ("favorit", "favourite", "喜爱", "喜欢的")
            ):
                fuzzy.append(pl)

    def rank(pl: dict[str, Any]) -> tuple:
        attrs = pl.get("attributes") or {}
        can_edit = bool(attrs.get("canEdit"))
        name = str(attrs.get("name") or "")
        # canEdit False 优先（系统歌单）
        return (0 if not can_edit else 1, 0 if name in names else 1, name)

    pool = exact or fuzzy
    if not pool:
        raise SystemExit(
            "未找到 Apple Music「喜爱歌曲」播放列表。"
            "请在 config.env 设置 FEEDBACK_FAVORITES_PLAYLIST_ID 或 "
            "FEEDBACK_FAVORITES_PLAYLIST_NAME。"
        )
    pool.sort(key=rank)
    return pool[0]


def fetch_favorite_songs(am: dr.AppleMusicClient) -> tuple[dict[str, Any], list[AppleSong]]:
    pl = find_favorites_playlist(am)
    pid = str(pl.get("id"))
    name = (pl.get("attributes") or {}).get("name")
    log(f"喜爱歌单: 「{name}」(id={pid})")
    items = am_paginate(
        am, f"/v1/me/library/playlists/{pid}/tracks?limit=100", limit=2000
    )
    songs: list[AppleSong] = []
    for it in items:
        s = apple_resource_to_song(it)
        if s:
            songs.append(s)
    log(f"喜爱曲目: {len(songs)} 首")
    return pl, songs


def fetch_recent_tracks(am: dr.AppleMusicClient, limit: int) -> list[AppleSong]:
    items = am_paginate(
        am, f"/v1/me/recent/played/tracks?limit={min(limit, 30)}", limit=limit
    )
    songs: list[AppleSong] = []
    for it in items:
        s = apple_resource_to_song(it)
        if s:
            songs.append(s)
    log(f"最近播放: 拉取 {len(songs)} 首（上限 {limit}）")
    return songs


# ---------------------------------------------------------------------------
# 网易云：搜索 / 红心 / 打卡
# ---------------------------------------------------------------------------

def ncm_cloudsearch(cookie: str, keywords: str, limit: int = 8) -> list[dict[str, Any]]:
    data = dr.ncm_get(
        "/cloudsearch",
        cookie,
        {"keywords": keywords, "limit": str(limit), "type": "1"},
    )
    if not isinstance(data, dict):
        raise RuntimeError("网易云搜索响应无效")
    if data.get("code") not in (200, None):
        data = dr.ncm_get(
            "/search", cookie,
            {"keywords": keywords, "limit": str(limit), "type": "1"},
        )
    if not isinstance(data, dict) or data.get("code") not in (200, None):
        raise RuntimeError("网易云搜索失败")
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("songs", []), list):
        raise RuntimeError("网易云搜索响应缺少结果")
    return list(result.get("songs") or [])


def _version_tags(title: str) -> set[str]:
    s = title.lower()
    tags: set[str] = set()
    checks = (
        ("taylor's version", "tv"),
        ("taylor’s version", "tv"),
        ("live", "live"),
        ("remix", "remix"),
        ("acoustic", "acoustic"),
        ("deluxe", "deluxe"),
        ("karaoke", "karaoke"),
        ("instrumental", "instrumental"),
    )
    for needle, tag in checks:
        if needle in s:
            tags.add(tag)
    return tags


def score_apple_vs_ncm(apple: AppleSong, ncm: dict[str, Any]) -> float:
    """与 daily_recommend.score_match 对称：Apple 为「真」，NCM 为候选。"""
    ncm_name = str(ncm.get("name") or "")
    if not dr.versions_compatible(apple.name, ncm_name):
        return 0.0
    nt = dr.normalize_title(ncm_name)
    ar = ncm.get("ar") or ncm.get("artists") or []
    # 主艺人 + 全体艺人串，便于 feat.
    ar_names: list[str] = []
    for a in ar:
        if isinstance(a, dict) and a.get("name"):
            ar_names.append(str(a["name"]))
        elif isinstance(a, str):
            ar_names.append(a)
    na = dr.normalize_title(ar_names[0]) if ar_names else ""
    na_all = dr.normalize_title(" ".join(ar_names))
    al_obj = ncm.get("al") or ncm.get("album") or {}
    nal = dr.normalize_title(
        str(al_obj.get("name") if isinstance(al_obj, dict) else al_obj or "")
    )
    t = dr.normalize_title(apple.name)
    a_raw = apple.artists.split(",")[0].split("&")[0]
    a_raw = re.split(r"\s+feat\.?\s+", a_raw, flags=re.I)[0].strip()
    a = dr.normalize_title(a_raw)
    al = dr.normalize_title(apple.album)
    if not t or not nt:
        return 0.0
    score = 0.0
    if t == nt:
        score += 50
    elif nt in t or t in nt:
        score += 30
    else:
        score += min(20, len(set(t) & set(nt)) * 2)

    # 未去括号的完整标题更一致时加分（Taylor's Version 等）
    if apple.name.strip().lower() == ncm_name.strip().lower():
        score += 12

    artist_pts = 0.0
    if a and ar_names:
        norms = [dr.normalize_title(x) for x in ar_names if x]
        if a in norms:
            artist_pts = 40
        else:
            for n in norms:
                if not n:
                    continue
                if a == n or a in n or n in a:
                    artist_pts = max(artist_pts, 28)
                    break
            if artist_pts < 28:
                # 分词：防止 "iu" 误命中 "iu piano" 以外的拼接串
                a_parts = [p for p in re.split(r"[\s,./&]+", a) if len(p) >= 2]
                for n in norms:
                    n_parts = [p for p in re.split(r"[\s,./&]+", n) if len(p) >= 2]
                    if a_parts and n_parts and (set(a_parts) & set(n_parts)):
                        artist_pts = max(artist_pts, 22)
    score += artist_pts

    if nal and al:
        if al == nal:
            score += 10
        elif nal in al or al in nal:
            score += 5
    dt = int(ncm.get("dt") or ncm.get("duration") or 0)
    if apple.duration_ms and dt:
        diff = abs(apple.duration_ms - dt)
        if diff <= 3000:
            score += 8
        elif diff <= 8000:
            score += 4
        elif diff > 45000:
            score -= 10

    # 版本标签（live / TV / remix）不一致则惩罚
    va = _version_tags(apple.name)
    vn = _version_tags(ncm_name)
    if va or vn:
        if va == vn:
            score += 12
        else:
            score -= 18

    # 艺人完全对不上时大幅降权（避免同名异曲）
    if a and na and artist_pts < 20:
        score *= 0.55

    return score


class MatchDeferred(RuntimeError):
    """AI review is needed but unavailable this run; retry later without cooldown."""


def _artist_names(ncm: dict[str, Any]) -> list[str]:
    return [str(x.get("name") if isinstance(x, dict) else x) for x in ncm.get("ar") or [] if x]


def _primary_artist(apple: AppleSong) -> str:
    return dr.normalize_title(
        re.split(r"\s+feat\.?\s+", apple.artists.split(",")[0], flags=re.I)[0]
    )


def _artist_conflict(apple: AppleSong, ncm: dict[str, Any]) -> bool:
    """True when artists exist on both sides but share nothing (same title, other song)."""
    a = _primary_artist(apple)
    norms = [dr.normalize_title(x) for x in _artist_names(ncm)]
    if not a or not norms:
        return False
    if a in norms:
        return False
    a_parts = set(p for p in re.split(r"[\s,./&]+", a) if len(p) >= 2)
    for n in norms:
        if a in n or n in a:
            return False
        if a_parts & set(p for p in re.split(r"[\s,./&]+", n) if len(p) >= 2):
            return False
    return True


def _strong_ncm_match(apple: AppleSong, ncm: dict[str, Any]) -> bool:
    """Exact normalized title and primary artist, same version, near-identical duration."""
    name = str(ncm.get("name") or "")
    title = dr.normalize_title(apple.name)
    primary = _primary_artist(apple)
    dt = ncm.get("dt")
    return (bool(title) and title == dr.normalize_title(name)
            and dr.versions_compatible(apple.name, name)
            and bool(primary) and primary in [dr.normalize_title(x) for x in _artist_names(ncm)]
            and bool(apple.duration_ms) and type(dt) is int and dt > 0
            and abs(apple.duration_ms - dt) <= 3000
            and score_apple_vs_ncm(apple, ncm) >= 90)


def _netease_result(s: dict[str, Any], score: float, method: str) -> dict[str, Any]:
    al = s.get("al") or {}
    return {
        "id": int(s.get("id")),
        "name": s.get("name"),
        "artists": " / ".join(_artist_names(s)),
        "album": al.get("name") if isinstance(al, dict) else "",
        "duration_ms": int(s.get("dt") or 0),
        "album_id": int(al.get("id") or 0) if isinstance(al, dict) else 0,
        "score": score,
        "match_method": method,
    }


def _summary(s: Optional[dict[str, Any]], score: Optional[float] = None) -> Optional[dict[str, Any]]:
    if not s:
        return None
    return {"id": int(s.get("id")), "name": s.get("name"),
            "artists": " / ".join(_artist_names(s)), "score": score}


def _ai_source(apple: AppleSong) -> dict[str, Any]:
    duration = apple.duration_ms if 0 <= apple.duration_ms <= 86_400_000 else None
    return {"id": apple.apple_id[:256], "name": apple.name[:512], "artists": apple.artists[:512],
            "album": apple.album[:512], "duration_ms": duration, "isrc": apple.isrc[:512]}


def _ai_candidates(apple: AppleSong, candidates: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Bounded, version- and duration-compatible real search results for the model."""
    out: list[dict[str, Any]] = []
    for s in sorted(candidates.values(), key=lambda c: score_apple_vs_ncm(apple, c), reverse=True):
        name = s.get("name")
        if not isinstance(name, str) or not name.strip() or not dr.versions_compatible(apple.name, name):
            continue
        dt = s.get("dt")
        dt = dt if type(dt) is int and 0 <= dt <= 86_400_000 else None
        if apple.duration_ms and dt and abs(apple.duration_ms - dt) > 15000:
            continue
        al = s.get("al")
        album = al.get("name") if isinstance(al, dict) and isinstance(al.get("name"), str) else ""
        out.append({"id": str(int(s["id"])), "name": name[:512],
                    "artists": [n[:256] for n in _artist_names(s)][:20],
                    "album": album[:512], "duration_ms": dt})
        if len(out) >= 30:
            break
    return out


def _add_search(cookie: str, term: str, candidates: dict[int, dict[str, Any]]) -> None:
    for s in ncm_cloudsearch(cookie, term, limit=8):
        sid = int(s.get("id") or 0)
        if sid and sid not in candidates:
            candidates[sid] = s
    time.sleep(FEEDBACK_REQUEST_SLEEP * 0.5)


def _search_terms(apple: AppleSong) -> list[str]:
    terms = [
        apple.search_term,
        f"{apple.name} {apple.artists.split(',')[0].strip()}" if apple.artists else apple.name,
        apple.name,
    ]
    return list(dict.fromkeys(t.strip() for t in terms if t.strip()))


def match_netease(
    cookie: str, apple: AppleSong, *, ai_review: bool = False,
    matcher: Optional[dr.AIMatcher] = None, review: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Rule match; with ai_review, every result short of a strong match goes to AI.

    Raises MatchDeferred when review is needed but the matcher is unavailable.
    """
    terms = _search_terms(apple)
    candidates: dict[int, dict[str, Any]] = {}
    for term in terms:
        _add_search(cookie, term, candidates)

    best: Optional[tuple[float, dict[str, Any]]] = None
    for s in candidates.values():
        sc = score_apple_vs_ncm(apple, s)
        if best is None or sc > best[0]:
            best = (sc, s)
    # 艺人无任何重合则拒绝（避免同名异曲，如 Autumn Morning 翻奏）
    rule = best if best and best[0] >= FEEDBACK_MIN_SCORE and not _artist_conflict(apple, best[1]) else None

    if not ai_review:
        return _netease_result(rule[1], rule[0], "rules") if rule else None
    if best and _strong_ncm_match(apple, best[1]):
        return _netease_result(best[1], best[0], "rules")
    if review is not None:
        review["rule_candidate"] = _summary(rule[1], rule[0]) if rule else None
    if matcher is None or not matcher.enabled:
        raise MatchDeferred("AI 本轮不可用")
    try:
        supplied = _ai_candidates(apple, candidates)
        decision = matcher.select(_ai_source(apple), supplied)
        if decision.search_terms:
            for term in decision.search_terms:
                if term not in terms:
                    _add_search(cookie, term, candidates)
            supplied = _ai_candidates(apple, candidates)
            decision = matcher.select(_ai_source(apple), supplied, allow_search=False)
        if review is not None:
            review["ai_reason"] = decision.reason
        if decision.candidate_id is None:
            if review is not None:
                review["ai"] = "rejected"
            return None
        if decision.candidate_id not in {c["id"] for c in supplied}:
            raise dr.AIUnavailable("AI 返回无效候选")
    except dr.AIUnavailable:
        raise MatchDeferred("AI 辅助匹配不可用") from None
    chosen = candidates[int(decision.candidate_id)]
    approved = bool(rule) and int(rule[1]["id"]) == int(chosen["id"])
    if review is not None:
        review["ai"] = "approved" if approved else "selected"
    return _netease_result(chosen, score_apple_vs_ncm(apple, chosen), "ai_review" if approved else "ai")


def ncm_likelist_ids(cookie: str, uid: int) -> set[int]:
    data = dr.ncm_get("/likelist", cookie, {"uid": str(uid)})
    if not isinstance(data, dict) or data.get("code") != 200 or not isinstance(data.get("ids"), list):
        raise RuntimeError("网易云 likelist 读取失败；停止反馈写入")
    return {int(x) for x in data["ids"] if x is not None}


def ncm_like(
    cookie: str,
    song_id: int,
    like: bool = True,
    retries: int = 3,
) -> dict[str, Any]:
    """红心；遇 405「操作频繁」指数退避重试。"""
    last: dict[str, Any] = {}
    for attempt in range(retries + 1):
        last = dr.ncm_get(
            "/like",
            cookie,
            {"id": str(song_id), "like": "true" if like else "false"},
        )
        code = last.get("code")
        if code == 200:
            return last
        msg = str(last.get("msg") or last.get("message") or "")
        if (code == 405 or "频繁" in msg) and attempt < retries:
            wait = 8 * (attempt + 1) + (attempt * 2)
            log(f"  红心限流，{wait}s 后重试 ({attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        return last
    return last


def ncm_scrobble(
    cookie: str, song_id: int, source_id: int, play_seconds: int
) -> dict[str, Any]:
    return dr.ncm_get(
        "/scrobble",
        cookie,
        {
            "id": str(song_id),
            "sourceid": str(source_id or 0),
            "time": str(max(1, play_seconds)),
        },
    )


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

# Completed histories are oldest-to-newest; pending records are never truncated.
RECENT_HISTORY_LIMIT = 500
SCROBBLE_HISTORY_LIMIT = 800
UNMATCHED_RETRY_SECONDS = 7 * 24 * 60 * 60


def _prepare_state(state: dict[str, Any], existing: bool = False) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("反馈状态必须是 JSON object")
    for key in ("liked_netease_ids", "liked_apple_ids", "scrobbled_apple_ids", "seen_recent_apple_ids"):
        if key in state and not isinstance(state[key], list):
            raise ValueError(f"反馈状态字段 {key} 必须是 list")
        state.setdefault(key, [])
    for key in ("unmatched_apple_ids", "pending_scrobbles", "pending_likes"):
        if key in state and not isinstance(state[key], dict):
            raise ValueError(f"反馈状态字段 {key} 必须是 object")
        state.setdefault(key, {})
    # Any existing legacy file counts as initialized, even an empty snapshot.
    # Replaying old history cannot safely recover the previous version's losses.
    state.setdefault("recent_initialized", existing)
    if not isinstance(state["recent_initialized"], bool):
        raise ValueError("反馈状态 recent_initialized 必须是 bool")
    for key in ("pending_scrobbles", "pending_likes"):
        for aid, record in state[key].items():
            if not isinstance(record, dict) or record.get("status") not in ("pending", "unmatched", "unknown"):
                raise ValueError(f"反馈状态 {key} 记录无效")
            try:
                song = AppleSong(**record["apple"])
            except (KeyError, TypeError) as exc:
                raise ValueError(f"反馈状态 {key} 缺少歌曲元数据") from exc
            text_fields = (song.apple_id, song.name, song.artists, song.album, song.isrc, song.library_id)
            if (not all(isinstance(value, str) for value in text_fields)
                    or not song.apple_id or not song.name
                    or type(song.duration_ms) is not int or song.duration_ms < 0
                    or str(aid) != song.apple_id):
                raise ValueError(f"反馈状态 {key} 歌曲元数据无效")
            matched = record.get("netease")
            if matched is not None or key == "pending_likes":
                if (not isinstance(matched, dict) or type(matched.get("id")) is not int
                        or matched["id"] <= 0
                        or not all(isinstance(matched.get(field), str) for field in ("name", "artists"))
                        or type(matched.get("score")) not in (int, float)
                        or type(matched.get("album_id", 0)) is not int):
                    raise ValueError(f"反馈状态 {key} 匹配元数据无效")
    state.setdefault("favorites_playlist_id", "")
    state.setdefault("updated_at", None)
    return state


class FeedbackStateError(RuntimeError):
    pass


def load_state() -> dict[str, Any]:
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
        return _prepare_state(json.loads(raw), existing=True)
    except FileNotFoundError:
        return _prepare_state({})
    except (ValueError, TypeError) as exc:
        raise FeedbackStateError("反馈状态损坏，已保留原文件，请人工检查 FEEDBACK_STATE_FILE") from exc


def save_state(state: dict[str, Any]) -> None:
    updated = datetime.now().isoformat(timespec="seconds")
    payload = json.dumps({**state, "updated_at": updated}, ensure_ascii=False, indent=2)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE_FILE.parent,
                                         prefix=f".{STATE_FILE.name}.", delete=False) as fp:
            temp_path = Path(fp.name)
            os.fchmod(fp.fileno(), 0o600)
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp_path, STATE_FILE)
        state["updated_at"] = updated
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _checkpoint(state: dict[str, Any], dry_run: bool) -> None:
    if not dry_run:
        save_state(state)


def _as_id_set(vals: Any) -> set[str]:
    return {str(v) for v in vals or []}


def _as_int_set(vals: Any) -> set[int]:
    out: set[int] = set()
    for v in vals or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _remember(state: dict[str, Any], field: str, value: Any, bound: int = 0) -> None:
    values = state[field]
    if value in values:
        values.remove(value)
    values.append(value)
    if bound:
        del values[:-bound]


def _cooling_down(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        checked = datetime.fromisoformat(record.get("at", "")).timestamp()
        return time.time() - checked < UNMATCHED_RETRY_SECONDS
    except (ValueError, TypeError, OverflowError):
        return False


def _safe_error(exc: Exception) -> str:
    # Never serialize raw API responses or exception messages, which can contain cookies.
    if isinstance(exc, dr.AuthenticationError):
        return "认证失效，请更新凭据后重试"
    if isinstance(exc, FeedbackStateError):
        return "反馈状态损坏，已保留原文件，请人工检查 FEEDBACK_STATE_FILE"
    return f"请求失败 ({type(exc).__name__})"


def _response_error(response: Any) -> str:
    code = response.get("code") if isinstance(response, dict) else None
    return f"网易云未确认成功 (code={code if isinstance(code, int) else 'missing'})"


def _rate_limited(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    return response.get("code") in (405, 429) or "频繁" in str(response.get("msg") or response.get("message") or "")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def sync_likes(
    cookie: str, am: dr.AppleMusicClient, state: dict[str, Any],
    already_liked: set[int], dry_run: bool, limit: int,
) -> dict[str, Any]:
    _prepare_state(state, existing=True)
    stats = dict(source_total=0, already_liked=0, matched=0, unmatched=0,
                 liked=0, skipped_state=0, skipped_cooldown=0, errors=0,
                 ai_reviewed=0, ai_rejected=0, deferred=0, items=[])
    matcher: Optional[dr.AIMatcher] = None
    ai_review = False
    if FEEDBACK_AI_MATCHING:
        try:
            matcher = dr.AIMatcher.from_env(dr.ROOT)
            ai_review = matcher.enabled
            if not ai_review:
                log("FEEDBACK_AI_MATCHING=1 但 AI_MATCHING_ENABLED=0，红心仅使用规则匹配")
        except dr.AIUnavailable:
            # Never fall back to unreviewed weak matches: defer them instead.
            log("AI 配置无效：本轮仅红心规则高把握匹配，其余留到下次")
            ai_review = True
    pl, songs = fetch_favorite_songs(am)
    state["favorites_playlist_id"] = str(pl.get("id") or "")
    stats["source_total"] = len(songs)
    pending = state["pending_likes"]
    unmatched = state["unmatched_apple_ids"]
    liked_apple = _as_id_set(state["liked_apple_ids"])
    liked_ncm = _as_int_set(state["liked_netease_ids"]) | already_liked
    # An unknown like is reconciled against a fresh likelist before another write.
    for aid, record in list(pending.items()):
        nid = int(record["netease"]["id"])
        if nid in already_liked:
            _remember(state, "liked_apple_ids", aid)
            _remember(state, "liked_netease_ids", nid)
            liked_apple.add(aid)
            pending.pop(aid)
            unmatched.pop(aid, None)
            stats["already_liked"] += 1
    _checkpoint(state, dry_run)
    # Pending matched writes retain their metadata even if removed from favorites.
    work = {aid: AppleSong(**record["apple"]) for aid, record in pending.items()}
    for song in songs:
        work.setdefault(song.apple_id, song)
    actions = scanned = 0
    uncertain_ncm: set[int] = set()
    scan_limit = max(limit, FEEDBACK_MAX_LIKE_SCAN_PER_RUN)
    for key, apple in work.items():
        if key in liked_apple:
            stats["skipped_state"] += 1
            continue
        # Do this before consuming the search budget, so old misses cannot starve new songs.
        if _cooling_down(unmatched.get(key)):
            stats["skipped_cooldown"] += 1
            continue
        if actions >= limit or scanned >= scan_limit:
            break
        log(f"[like] {apple.artists} - {apple.name}")
        row: dict[str, Any] = {"apple": asdict(apple), "action": None}
        record = pending.get(key)
        review: dict[str, Any] = {}
        try:
            if record:
                m = record["netease"]
            else:
                scanned += 1
                m = match_netease(cookie, apple, ai_review=ai_review, matcher=matcher, review=review)
        except dr.AuthenticationError:
            _checkpoint(state, dry_run)
            raise
        except MatchDeferred:
            # No cooldown: the song is retried as soon as AI works again.
            if matcher is not None and matcher.enabled:
                log("  AI 辅助匹配不可用，本轮其余待审核歌曲留到下次")
                matcher = None
            stats["deferred"] += 1
            row.update(action="deferred", **review)
            stats["items"].append(row)
            continue
        except Exception as exc:
            stats["errors"] += 1
            row.update(action="error", error=_safe_error(exc))
            stats["items"].append(row)
            continue
        row.update(review)
        if review.get("ai"):
            stats["ai_reviewed"] += 1
        if not m:
            stats["unmatched"] += 1
            if review.get("ai") == "rejected":
                stats["ai_rejected"] += 1
                log("  AI 未确认可靠匹配")
            unmatched[key] = {"name": apple.name, "artists": apple.artists,
                              "at": datetime.now().isoformat(timespec="seconds")}
            row["action"] = "unmatched"
            stats["items"].append(row)
            _checkpoint(state, dry_run)
            continue
        stats["matched"] += 1
        row["netease"] = m
        method = m.get("match_method", "rules")
        log(f"  → {m['artists']} - {m['name']} id={m['id']} score={m['score']:.0f} [{method}]")
        nid = int(m["id"])
        if nid in liked_ncm:
            stats["already_liked"] += 1
            row["action"] = "already_liked"
            _remember(state, "liked_apple_ids", key)
            _remember(state, "liked_netease_ids", nid)
            liked_apple.add(key)
            unmatched.pop(key, None)
            pending.pop(key, None)
        elif nid in uncertain_ncm:
            # Hold aliases of an uncertain write until next run's fresh likelist.
            pending[key] = {"apple": asdict(apple), "netease": m, "status": "unknown"}
            stats["deferred"] += 1
            row["action"] = "deferred"
            log("  同曲红心结果待核对，保留到下次；继续处理其他歌曲")
        elif dry_run:
            row["action"] = "dry_run_like"
            stats["liked"] += 1
            actions += 1
        else:
            actions += 1
            pending[key] = {"apple": asdict(apple), "netease": m, "status": "unknown"}
            # If interrupted during a write, the next run must reconcile first.
            _checkpoint(state, dry_run)
            stop = False
            try:
                response = ncm_like(cookie, nid, True)
            except dr.AuthenticationError:
                pending[key]["status"] = "pending"
                _checkpoint(state, dry_run)
                raise
            except Exception as exc:
                ambiguous = isinstance(exc, (TimeoutError, dr.TransportError)) and getattr(exc, "ambiguous", True)
                pending[key]["status"] = "unknown" if ambiguous else "pending"
                if ambiguous:
                    uncertain_ncm.add(nid)
                    log("  红心写入结果未知，保留待核对；继续处理其他歌曲")
                stats["errors"] += 1
                row.update(action="unknown" if ambiguous else "error", error=_safe_error(exc))
            else:
                if isinstance(response, dict) and response.get("code") == 200:
                    row["action"] = "liked"
                    stats["liked"] += 1
                    _remember(state, "liked_apple_ids", key)
                    _remember(state, "liked_netease_ids", nid)
                    liked_apple.add(key)
                    liked_ncm.add(nid)
                    pending.pop(key)
                    unmatched.pop(key, None)
                else:
                    pending[key]["status"] = "pending"
                    stats["errors"] += 1
                    row.update(action="error", error=_response_error(response))
                    stop = _rate_limited(response)
            _checkpoint(state, dry_run)
            stats["items"].append(row)
            if stop:
                break
            time.sleep(FEEDBACK_LIKE_SLEEP)
            continue
        stats["items"].append(row)
        _checkpoint(state, dry_run)
    _checkpoint(state, dry_run)
    return stats


def sync_scrobble(
    cookie: str, am: dr.AppleMusicClient, state: dict[str, Any],
    dry_run: bool, limit: int, seed_only_first: bool = True,
) -> dict[str, Any]:
    _prepare_state(state, existing=True)
    stats = dict(source_total=0, new_plays=0, seed_only=False, matched=0,
                 unmatched=0, scrobbled=0, skipped_seen=0, unknown=0, errors=0, items=[])
    songs = fetch_recent_tracks(am, FEEDBACK_RECENT_LIMIT)
    stats["source_total"] = len(songs)
    if not state["recent_initialized"] and seed_only_first:
        stats["seed_only"] = True
        for song in songs:
            _remember(state, "seen_recent_apple_ids", song.apple_id, RECENT_HISTORY_LIMIT)
        state["recent_initialized"] = True
        log("首次运行：仅保存最近播放快照（空快照也完成初始化）")
        _checkpoint(state, dry_run)
        return stats
    state["recent_initialized"] = True
    seen = _as_id_set(state["seen_recent_apple_ids"]) | _as_id_set(state["scrobbled_apple_ids"])
    pending = state["pending_scrobbles"]
    for song in songs:
        if song.apple_id in seen:
            stats["skipped_seen"] += 1
        elif song.apple_id not in pending:
            pending[song.apple_id] = {"apple": asdict(song), "status": "pending"}
            stats["new_plays"] += 1
    # Capture the entire snapshot before attempting any writes or hitting a limit.
    _checkpoint(state, dry_run)
    actions = 0
    for key, record in list(pending.items()):
        apple = AppleSong(**record["apple"])
        row: dict[str, Any] = {"apple": asdict(apple), "action": None}
        if record["status"] == "unknown":
            row.update(action="unknown", error="打卡结果未知，保留记录待人工核实；不会自动重放")
            stats["unknown"] += 1
            stats["errors"] += 1
            stats["items"].append(row)
            log(f"打卡结果未知，暂不重放 Apple track {key}")
            continue
        if actions >= limit or (record["status"] == "unmatched" and _cooling_down(record)):
            continue
        log(f"[scrobble] {apple.artists} - {apple.name}")
        actions += 1
        try:
            m = record.get("netease") or match_netease(cookie, apple)
        except dr.AuthenticationError:
            _checkpoint(state, dry_run)
            raise
        except Exception as exc:
            stats["errors"] += 1
            row.update(action="error", error=_safe_error(exc))
            stats["items"].append(row)
            continue
        if not m:
            record.update(status="unmatched", at=datetime.now().isoformat(timespec="seconds"))
            row["action"] = "unmatched"
            stats["unmatched"] += 1
            stats["items"].append(row)
            _checkpoint(state, dry_run)
            continue
        record["netease"] = m
        row["netease"] = m
        stats["matched"] += 1
        log(f"  → {m['artists']} - {m['name']} id={m['id']} score={m['score']:.0f}")
        play_sec = FEEDBACK_SCROBBLE_TIME
        if apple.duration_ms:
            play_sec = max(30, min(play_sec, apple.duration_ms // 1000 - 1))
        if dry_run:
            row["action"] = "dry_run_scrobble"
            stats["scrobbled"] += 1
        else:
            record["status"] = "unknown"
            _checkpoint(state, dry_run)
            stop = False
            try:
                response = ncm_scrobble(cookie, int(m["id"]), int(m.get("album_id") or 0), play_sec)
            except dr.AuthenticationError:
                record["status"] = "pending"
                _checkpoint(state, dry_run)
                raise
            except Exception as exc:
                ambiguous = isinstance(exc, (TimeoutError, dr.TransportError)) and getattr(exc, "ambiguous", True)
                record["status"] = "unknown" if ambiguous else "pending"
                row.update(action="unknown" if ambiguous else "error", error=_safe_error(exc))
                stats["errors"] += 1
                stats["unknown"] += int(ambiguous)
            else:
                if isinstance(response, dict) and response.get("code") == 200:
                    row["action"] = "scrobbled"
                    stats["scrobbled"] += 1
                    _remember(state, "scrobbled_apple_ids", key, SCROBBLE_HISTORY_LIMIT)
                    _remember(state, "seen_recent_apple_ids", key, RECENT_HISTORY_LIMIT)
                    pending.pop(key)
                else:
                    record["status"] = "pending"
                    row.update(action="error", error=_response_error(response))
                    stats["errors"] += 1
                    stop = _rate_limited(response)
            _checkpoint(state, dry_run)
            stats["items"].append(row)
            if stop:
                break
            time.sleep(FEEDBACK_REQUEST_SLEEP)
            continue
        stats["items"].append(row)
    _checkpoint(state, dry_run)
    return stats


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Apple Music 喜爱/最近播放 → 网易云红心/听歌打卡"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只匹配不写网易云（也可用 FEEDBACK_DRY_RUN=1）",
    )
    p.add_argument(
        "--likes-only",
        action="store_true",
        help="只同步喜爱→红心",
    )
    p.add_argument(
        "--scrobble-only",
        action="store_true",
        help="只同步最近播放→scrobble",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="覆盖每类本轮最大写入条数（0=用配置）",
    )
    p.add_argument(
        "--force-scrobble-seed",
        action="store_true",
        help="首次也 scrobble（不推荐；默认首次只建快照）",
    )
    args = p.parse_args(argv)
    if args.limit < 0:
        p.error("--limit 必须是非负整数（0=用配置）")
    return args


def _run_main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    dry_run = bool(args.dry_run or FEEDBACK_DRY_RUN)
    do_likes = FEEDBACK_SYNC_LIKES and not args.scrobble_only
    do_scrobble = FEEDBACK_SYNC_SCROBBLE and not args.likes_only
    seed_only_first = FEEDBACK_SCROBBLE_SEED_ONLY_FIRST and not args.force_scrobble_seed

    like_limit = args.limit or FEEDBACK_MAX_LIKES_PER_RUN
    scrobble_limit = args.limit or FEEDBACK_MAX_SCROBBLE_PER_RUN

    dr.LOG_DIR.mkdir(parents=True, exist_ok=True)
    dr.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_file = dr.LOG_DIR / f"feedback-{dr.TODAY}.log"
    log_fp = open(log_file, "a", encoding="utf-8")
    sys.stdout = dr.Tee(sys.__stdout__, log_fp)  # type: ignore
    sys.stderr = dr.Tee(sys.__stderr__, log_fp)  # type: ignore

    log(
        f"=== Apple → 网易云反馈 {dr.TODAY} ({dr.RUN_TS}) "
        f"dry_run={dry_run} likes={do_likes} scrobble={do_scrobble} ==="
    )
    # Reject a corrupt state before making any remote requests.
    state = load_state()
    dr.ensure_ncm_api()
    cookie = dr.load_cookie()
    st = dr.ncm_get("/login/status", cookie)
    profile = (st.get("data") or {}).get("profile") or {}
    nickname = str(profile.get("nickname") or "?")
    uid = int(profile.get("userId") or 0)
    if not uid:
        raise dr.AuthenticationError("网易云登录失效，请更新 cookie")
    log(f"网易云账号: {nickname} (uid={uid})")
    already_liked: set[int] = set()
    if do_likes:
        already_liked = ncm_likelist_ids(cookie, uid)
        log(f"网易云现有红心: {len(already_liked)} 首")

    user_token = dr.get_music_user_token()
    dev_token = dr.fetch_web_developer_token()
    am = dr.AppleMusicClient(dev_token, user_token, dr.STOREFRONT)
    sf = am.storefront_info()
    sf_id = (sf.get("data") or [{}])[0].get("id")
    log(f"Apple Music storefront: {sf_id}")

    like_stats: Optional[dict[str, Any]] = None
    scrobble_stats: Optional[dict[str, Any]] = None

    if do_likes:
        log("--- 同步喜爱 → 红心 ---")
        like_stats = sync_likes(
            cookie, am, state, already_liked, dry_run, like_limit
        )
        log(
            "喜爱统计: "
            + json.dumps(
                {k: v for k, v in like_stats.items() if k != "items"},
                ensure_ascii=False,
            )
        )

    if do_scrobble:
        log("--- 同步最近播放 → scrobble ---")
        scrobble_stats = sync_scrobble(
            cookie,
            am,
            state,
            dry_run,
            scrobble_limit,
            seed_only_first=seed_only_first,
        )
        log(
            "scrobble 统计: "
            + json.dumps(
                {k: v for k, v in scrobble_stats.items() if k != "items"},
                ensure_ascii=False,
            )
        )

    man = {
        "date": dr.TODAY,
        "run_ts": dr.RUN_TS,
        "dry_run": dry_run,
        "account": {"nickname": nickname, "uid": uid},
        "likes": (
            {k: v for k, v in (like_stats or {}).items() if k != "items"}
            if like_stats
            else None
        ),
        "scrobble": (
            {k: v for k, v in (scrobble_stats or {}).items() if k != "items"}
            if scrobble_stats
            else None
        ),
        # 明细可能很长，只保留本轮 action 条目
        "like_items": (like_stats or {}).get("items") if like_stats else [],
        "scrobble_items": (scrobble_stats or {}).get("items")
        if scrobble_stats
        else [],
    }
    man_path = dr.DATA_DIR / f"{MANIFEST_PREFIX}-{dr.TODAY}.json"
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"清单: {man_path}")

    if dry_run:
        log("dry-run：未写入 feedback-state.json，也未调用写接口（或未真正红心/打卡）")
    else:
        save_state(state)
        log(f"状态: {STATE_FILE}")

    log("=== 完成 ===")
    like_err = int((like_stats or {}).get("errors") or 0)
    scrob_err = int((scrobble_stats or {}).get("errors") or 0)
    return 1 if like_err or scrob_err else 0


def main(argv: Optional[list[str]] = None) -> int:
    stdout, stderr = sys.stdout, sys.stderr
    try:
        return _run_main(argv)
    except Exception as exc:
        if isinstance(exc, dr.AuthenticationError):
            log(_safe_error(exc))
        else:
            log(f"反馈停止: {_safe_error(exc)}；请检查配置、连接和状态文件")
        return 1
    finally:
        # Tee owns only the log file; never close the caller's output streams.
        if sys.stdout is not stdout and hasattr(sys.stdout, "files"):
            for fp in sys.stdout.files:
                if fp is not sys.__stdout__:
                    fp.close()
        sys.stdout, sys.stderr = stdout, stderr


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)

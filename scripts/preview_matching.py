#!/usr/bin/env python3
"""Re-match source songs in a saved daily manifest using catalog reads only."""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daily_recommend as dr


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="已保存的每日同步 manifest")
    parser.add_argument("--unmatched-only", action="store_true", help="只重查原来未匹配的歌曲")
    parser.add_argument("--limit", type=int, default=0, help="最多检查数量（0=全部）")
    parser.add_argument("--output", type=Path, help="结果 JSON；默认输出到 stdout，不覆盖原清单")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit 必须非负")
    if args.output and args.output.resolve() == args.manifest.resolve():
        parser.error("--output 不能覆盖输入清单")
    return args


class ReadOnlyCatalog:
    """Expose only catalog searching, with an HTTP boundary against mutations."""

    def __init__(self, client):
        self.client = client

    @property
    def _consecutive_429(self):
        return self.client._consecutive_429

    def search_songs(self, term, limit=10):
        return self.client.search_songs(term, limit=limit)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        rows = manifest["tracks"]
        if not isinstance(rows, list):
            raise ValueError
        rows = [r for r in rows if not args.unmatched_only or r.get("status") == "unmatched"]
        if args.limit:
            rows = rows[:args.limit]
        sources = []
        for row in rows:
            source = dr.NeteaseSong(**row["netease"])
            if (type(source.id) is not int or source.id <= 0
                    or not all(isinstance(s, str) for s in (source.name, source.artists, source.album))
                    or not source.name.strip() or not source.artists.strip()
                    or type(source.duration_ms) is not int or not 0 <= source.duration_ms <= 86400000):
                raise ValueError
            sources.append((row, source))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("无法读取有效的日推清单", file=sys.stderr)
        return 1
    try:
        matcher = dr.AIMatcher.from_env(dr.ROOT)
        # Keep stdout valid JSON while existing matching/token helpers log progress.
        with contextlib.redirect_stdout(sys.stderr):
            client = dr.AppleMusicClient(dr.fetch_web_developer_token(), dr.get_music_user_token(), dr.STOREFRONT)
            original_request = client.request

            def catalog_request(path, method="GET", body=None, timeout=45):
                validated = dr.apple_page_path(path, client.base)
                if method != "GET" or body is not None or not validated.startswith(f"/v1/catalog/{dr.STOREFRONT}/"):
                    raise dr.TransportError("预览只允许当前地区的曲库读取")
                return original_request(validated, method=method, timeout=timeout)

            client.request = catalog_request
            catalog = ReadOnlyCatalog(client)
            results = []
            for row, source in sources:
                match = dr.match_catalog(catalog, source, matcher=matcher)
                results.append({"netease": row["netease"], "previous_match": row.get("match"),
                                "match": match, "status": "matched" if match else "unmatched"})
        payload = json.dumps({"preview": True, "storefront": dr.STOREFRONT,
                              "ai_enabled": matcher.enabled,
                              "stats": {"total": len(results), "matched": sum(r["match"] is not None for r in results)},
                              "tracks": results}, ensure_ascii=False, indent=2)
        if args.output:
            # Exclusive creation prevents overwriting state, secrets, or prior results.
            with args.output.open("x", encoding="utf-8") as fp:
                fp.write(payload + "\n")
        else:
            print(payload)
    except (dr.AIUnavailable, dr.TransportError, dr.AuthenticationError, dr.APIError, OSError):
        print("匹配预览失败，请检查 AI 配置、曲库连接和输出文件路径", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

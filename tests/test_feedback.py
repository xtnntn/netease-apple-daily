"""Feedback behavior tests: isolated state, fake clients, no configuration/network."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
# Import the real shared implementation without letting it read project config.
with patch.object(Path, "is_file", return_value=False), patch.dict(os.environ, {}, clear=True):
    spec = importlib.util.spec_from_file_location("feedback_test_daily", ROOT / "daily_recommend.py")
    dr = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dr
    spec.loader.exec_module(dr)
    spec = importlib.util.spec_from_file_location("feedback_under_test", ROOT / "apple_to_netease_feedback.py")
    fb = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = fb
    with patch.dict(sys.modules, {"daily_recommend": dr}):
        spec.loader.exec_module(fb)


def song(aid):
    return fb.AppleSong(str(aid), "Song " + str(aid), "Artist", "Album", 200000)


def match(cookie, apple):
    return dict(id=int(apple.apple_id), name=apple.name, artists=apple.artists,
                album=apple.album, album_id=123, score=110)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.tmp) / "state.json"
        self.stack.enter_context(patch.object(fb, "STATE_FILE", self.path))
        self.stack.enter_context(patch.object(fb, "log"))
        self.stack.enter_context(patch.object(fb.time, "sleep"))
        self.stack.enter_context(patch.object(fb, "match_netease", side_effect=match))
        self.recent = self.stack.enter_context(patch.object(fb, "fetch_recent_tracks", return_value=[]))
        self.favorites = self.stack.enter_context(patch.object(fb, "fetch_favorite_songs", return_value=({"id": "favorites"}, [])))
        self.scrobble = self.stack.enter_context(patch.object(fb, "ncm_scrobble", return_value={"code": 200}))
        self.like = self.stack.enter_context(patch.object(fb, "ncm_like", return_value={"code": 200}))
        self.state = fb.load_state()

    def run_scrobble(self, songs, limit=1, dry=False):
        self.recent.return_value = songs
        return fb.sync_scrobble("test", None, self.state, dry, limit)

    def initialize(self):
        self.state["recent_initialized"] = True

    def test_empty_initial_snapshot_initializes(self):
        stats = self.run_scrobble([])
        self.assertTrue(stats["seed_only"])
        self.state = fb.load_state()
        self.assertTrue(self.state["recent_initialized"])
        self.run_scrobble([song(1)])
        self.scrobble.assert_called_once()

    def test_first_seed_never_submits_history(self):
        self.run_scrobble([song(1), song(2)])
        self.scrobble.assert_not_called()
        self.run_scrobble([song(1), song(3)])
        self.scrobble.assert_called_once_with("test", 3, 123, 199)

    def test_legacy_empty_file_is_initialized(self):
        self.path.write_text(json.dumps({"seen_recent_apple_ids": [], "custom": "preserve"}))
        self.state = fb.load_state()
        self.assertTrue(self.state["recent_initialized"])
        self.run_scrobble([song(1)])
        self.assertEqual(self.state["custom"], "preserve")
        self.scrobble.assert_called_once()

    def test_legacy_seen_never_replayed(self):
        self.path.write_text(json.dumps({"seen_recent_apple_ids": ["1"], "scrobbled_apple_ids": ["2"]}))
        self.state = fb.load_state()
        self.run_scrobble([song(1), song(2), song(3)])
        self.scrobble.assert_called_once_with("test", 3, 123, 199)

    def test_over_limit_survives_window_and_restart(self):
        self.initialize()
        self.run_scrobble([song(1), song(2), song(2), song(3)])
        self.assertEqual(set(self.state["pending_scrobbles"]), {"2", "3"})
        self.state = fb.load_state()
        self.run_scrobble([], limit=2)
        self.assertEqual([c.args[1] for c in self.scrobble.call_args_list], [1, 2, 3])
        self.assertEqual(self.state["pending_scrobbles"], {})

    def test_explicit_failure_retries_after_window_leaves(self):
        self.initialize()
        self.scrobble.return_value = {"code": 500, "cookie": "SECRET"}
        stats = self.run_scrobble([song(1)])
        self.assertEqual(self.state["pending_scrobbles"]["1"]["status"], "pending")
        self.assertNotIn("SECRET", json.dumps(stats))
        self.assertNotIn("1", self.state["seen_recent_apple_ids"])
        self.state = fb.load_state()
        self.scrobble.return_value = {"code": 200}
        self.run_scrobble([])
        self.assertEqual(self.state["scrobbled_apple_ids"], ["1"])

    def test_timeout_is_held_and_reported_without_replay(self):
        self.initialize()
        self.scrobble.side_effect = dr.TransportError("secret-cookie", ambiguous=True)
        stats = self.run_scrobble([song(1)])
        self.assertEqual(stats["unknown"], 1)
        self.assertNotIn("secret-cookie", json.dumps(stats))
        self.state = fb.load_state()
        stats = self.run_scrobble([])
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(stats["errors"], 1)
        self.scrobble.assert_called_once()
        self.assertEqual(self.state["pending_scrobbles"]["1"]["apple"], fb.asdict(song(1)))

    def test_prewrite_checkpoint_protects_interruption(self):
        self.initialize()
        def interrupted(*args):
            on_disk = fb.load_state()
            self.assertEqual(on_disk["pending_scrobbles"]["1"]["status"], "unknown")
            raise KeyboardInterrupt()
        self.scrobble.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.run_scrobble([song(1)])
        self.state = fb.load_state()
        self.run_scrobble([])
        self.scrobble.assert_called_once()

    def test_failed_prewrite_checkpoint_prevents_scrobble(self):
        self.initialize()
        actual_save = fb.save_state

        def fail_unknown(state):
            if state["pending_scrobbles"]["1"]["status"] == "unknown":
                raise OSError("disk full")
            actual_save(state)

        with patch.object(fb, "save_state", side_effect=fail_unknown):
            with self.assertRaises(OSError):
                self.run_scrobble([song(1)])
        self.scrobble.assert_not_called()
        self.state = fb.load_state()
        self.assertEqual(self.state["pending_scrobbles"]["1"]["status"], "pending")
        self.run_scrobble([])
        self.scrobble.assert_called_once()

    def test_failed_result_checkpoint_holds_success_without_replay(self):
        self.initialize()
        actual_save = fb.save_state

        def fail_completed(state):
            if state["scrobbled_apple_ids"]:
                raise OSError("disk full")
            actual_save(state)

        with patch.object(fb, "save_state", side_effect=fail_completed):
            with self.assertRaises(OSError):
                self.run_scrobble([song(1)])
        self.state = fb.load_state()
        self.assertEqual(self.state["pending_scrobbles"]["1"]["status"], "unknown")
        stats = self.run_scrobble([])
        self.scrobble.assert_called_once()
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(self.state["scrobbled_apple_ids"], [])

    def test_missing_success_code_remains_pending(self):
        self.initialize()
        self.scrobble.return_value = {}
        stats = self.run_scrobble([song(1)])
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.state["scrobbled_apple_ids"], [])
        self.assertIn("1", self.state["pending_scrobbles"])

    def test_auth_aborts_remaining_mutations_with_checkpoint(self):
        self.initialize()
        self.scrobble.side_effect = [{"code": 200}, dr.AuthenticationError("secret"), {"code": 200}]
        with self.assertRaises(dr.AuthenticationError):
            self.run_scrobble([song(1), song(2), song(3)], limit=3)
        self.assertEqual(self.scrobble.call_count, 2)
        saved = fb.load_state()
        self.assertEqual(saved["scrobbled_apple_ids"], ["1"])
        self.assertEqual(set(saved["pending_scrobbles"]), {"2", "3"})

    def test_completed_history_uses_processing_order(self):
        self.initialize()
        self.state["scrobbled_apple_ids"] = [str(x) for x in range(1000, 1799)]
        self.run_scrobble([song(9), song(10)], limit=2)
        self.assertEqual(len(self.state["scrobbled_apple_ids"]), 800)
        self.assertEqual(self.state["scrobbled_apple_ids"][-2:], ["9", "10"])
        self.assertNotIn("1000", self.state["scrobbled_apple_ids"])

    def test_pending_not_evicted_by_history_bound(self):
        self.initialize()
        self.run_scrobble([song(x) for x in range(900)], limit=0)
        self.assertEqual(len(fb.load_state()["pending_scrobbles"]), 900)

    def test_unmatched_scrobble_retains_metadata_and_cools_down(self):
        self.initialize()
        with patch.object(fb, "match_netease", return_value=None) as search:
            self.run_scrobble([song(1)])
            self.run_scrobble([])
            search.assert_called_once()
        self.state["pending_scrobbles"]["1"]["at"] = "2000-01-01T00:00:00"
        self.run_scrobble([])
        self.scrobble.assert_called_once()

    def test_likes_cooldown_skips_before_scan_budget(self):
        self.favorites.return_value = ({"id": "f"}, [song(1), song(2)])
        self.state["unmatched_apple_ids"]["1"] = {"at": datetime.now().isoformat()}
        with patch.object(fb, "FEEDBACK_MAX_LIKE_SCAN_PER_RUN", 1), patch.object(fb, "match_netease", side_effect=match) as search:
            stats = fb.sync_likes("test", None, self.state, set(), False, 1)
        self.assertEqual(stats["skipped_cooldown"], 1)
        self.assertEqual(search.call_args.args[1].apple_id, "2")
        self.like.assert_called_once_with("test", 2, True)

    def test_likes_old_unmatched_is_rechecked(self):
        self.favorites.return_value = ({"id": "f"}, [song(1)])
        self.state["unmatched_apple_ids"]["1"] = {"at": "2000-01-01T00:00:00"}
        fb.sync_likes("test", None, self.state, set(), False, 1)
        self.like.assert_called_once()
        self.assertNotIn("1", self.state["unmatched_apple_ids"])

    def test_search_error_does_not_create_unmatched(self):
        self.favorites.return_value = ({"id": "f"}, [song(1)])
        with patch.object(fb, "match_netease", side_effect=RuntimeError("credential")):
            stats = fb.sync_likes("test", None, self.state, set(), False, 1)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.state["unmatched_apple_ids"], {})
        self.assertNotIn("credential", json.dumps(stats))

    def test_unknown_like_reconciles_even_outside_favorites(self):
        self.favorites.return_value = ({"id": "f"}, [song(1)])
        self.like.side_effect = dr.TransportError("secret", ambiguous=True)
        fb.sync_likes("test", None, self.state, set(), False, 1)
        self.state = fb.load_state()
        self.favorites.return_value = ({"id": "f"}, [])
        fb.sync_likes("test", None, self.state, {1}, False, 1)
        self.like.assert_called_once()
        self.assertEqual(self.state["liked_apple_ids"], ["1"])
        self.assertEqual(self.state["pending_likes"], {})

    def test_interrupted_like_reconciles_checkpoint_after_restart(self):
        self.favorites.return_value = ({"id": "f"}, [song(1)])

        def interrupted(*args):
            saved = fb.load_state()["pending_likes"]["1"]
            self.assertEqual(saved["status"], "unknown")
            self.assertEqual(saved["apple"], fb.asdict(song(1)))
            raise KeyboardInterrupt()

        self.like.side_effect = interrupted
        with self.assertRaises(KeyboardInterrupt):
            fb.sync_likes("test", None, self.state, set(), False, 1)
        self.state = fb.load_state()
        self.favorites.return_value = ({"id": "f"}, [])
        fb.sync_likes("test", None, self.state, {1}, False, 1)
        self.like.assert_called_once()
        self.assertEqual(self.state["liked_apple_ids"], ["1"])
        self.assertEqual(self.state["pending_likes"], {})

    def test_failed_like_retries_metadata_outside_favorites(self):
        self.favorites.return_value = ({"id": "f"}, [song(1)])
        self.like.return_value = {"code": 500}
        fb.sync_likes("test", None, self.state, set(), False, 1)
        self.state = fb.load_state()
        self.favorites.return_value = ({"id": "f"}, [])
        self.like.return_value = {"code": 200}
        fb.sync_likes("test", None, self.state, set(), False, 1)
        self.assertEqual(self.like.call_count, 2)
        self.assertEqual(self.state["liked_apple_ids"], ["1"])
        self.assertEqual(self.state["pending_likes"], {})

    def test_unknown_like_absent_in_fresh_list_can_retry(self):
        self.favorites.return_value = ({"id": "f"}, [song(1)])
        self.like.side_effect = [dr.TransportError("secret", ambiguous=True), {"code": 200}]
        fb.sync_likes("test", None, self.state, set(), False, 1)
        fb.sync_likes("test", None, self.state, set(), False, 1)
        self.assertEqual(self.like.call_count, 2)
        self.assertEqual(self.state["liked_apple_ids"], ["1"])

    def test_unknown_like_stops_alias_write_until_fresh_reconciliation(self):
        self.favorites.return_value = ({"id": "f"}, [song(1), song(2)])
        same_match = match("test", song(1))
        with patch.object(fb, "match_netease", return_value=same_match):
            self.like.side_effect = dr.TransportError("timeout", ambiguous=True)
            stats = fb.sync_likes("test", None, self.state, set(), False, 2)
            self.like.assert_called_once()
            self.assertEqual(stats["errors"], 1)
            self.state = fb.load_state()
            fb.sync_likes("test", None, self.state, {1}, False, 2)
        self.like.assert_called_once()
        self.assertEqual(self.state["liked_apple_ids"], ["1", "2"])

    def test_likes_missing_code_and_auth(self):
        self.favorites.return_value = ({"id": "f"}, [song(1), song(2)])
        self.like.return_value = {"message": "secret"}
        stats = fb.sync_likes("test", None, self.state, set(), False, 1)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.state["liked_apple_ids"], [])
        self.like.side_effect = dr.AuthenticationError("secret")
        with self.assertRaises(dr.AuthenticationError):
            fb.sync_likes("test", None, self.state, set(), False, 2)
        self.assertEqual(self.like.call_count, 2)

    def test_rate_limit_stops_queue_without_loss(self):
        self.initialize()
        self.scrobble.return_value = {"code": 405}
        self.run_scrobble([song(1), song(2)], limit=2)
        self.scrobble.assert_called_once()
        self.assertEqual(set(fb.load_state()["pending_scrobbles"]), {"1", "2"})

    def test_atomic_replace_failure_keeps_original_and_cleans_temp(self):
        fb.save_state(self.state)
        original = self.path.read_bytes()
        with patch.object(fb.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fb.save_state({**self.state, "test": True})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_interrupted_temp_write_preserves_original(self):
        fb.save_state(self.state)
        original = self.path.read_bytes()
        with patch.object(fb.os, "fsync", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                fb.save_state({**self.state, "test": True})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_state_permissions_and_same_directory(self):
        fb.save_state(self.state)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        actual_replace = os.replace
        with patch.object(fb.os, "replace", wraps=actual_replace) as replace:
            fb.save_state(self.state)
        self.assertEqual(Path(replace.call_args.args[0]).parent, self.path.parent)

    def test_corrupt_state_fails_without_overwrite(self):
        for raw in ("{broken", "[]", '{"pending_scrobbles": []}', '{"pending_scrobbles":{"1":{"status":"unknown"}}}'):
            self.path.write_text(raw)
            with self.assertRaisesRegex(RuntimeError, "状态损坏"):
                fb.load_state()
            self.assertEqual(self.path.read_text(), raw)

    def test_malformed_pending_song_rejected_before_writes(self):
        record = {"apple": fb.asdict(song(1)), "status": "pending"}
        for field, value in (("name", None), ("duration_ms", "invalid"), ("apple_id", 1)):
            bad = json.loads(json.dumps(record))
            bad["apple"][field] = value
            self.path.write_text(json.dumps({"pending_scrobbles": {"1": bad}}))
            with self.assertRaises(fb.FeedbackStateError):
                fb.load_state()
        self.scrobble.assert_not_called()

    def test_cli_zero_default_and_negative_rejected(self):
        self.assertEqual(fb.parse_args(["--limit", "0"]).limit, 0)
        with patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit) as exit_info:
            fb.parse_args(["--limit", "-1"])
        self.assertEqual(exit_info.exception.code, 2)

    def test_dry_run_preserves_disk_bytes_and_mtime(self):
        self.initialize()
        fb.save_state(self.state)
        original, stamp = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.favorites.return_value = ({"id": "f"}, [song(2)])
        fb.sync_likes("test", None, self.state, set(), True, 2)
        self.run_scrobble([song(1)], dry=True)
        self.like.assert_not_called()
        self.scrobble.assert_not_called()
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), (original, stamp))

    def test_likelist_failure_is_not_empty_success(self):
        with patch.object(dr, "ncm_get", return_value={"code": 500, "cookie": "secret"}):
            with self.assertRaisesRegex(RuntimeError, "likelist"):
                fb.ncm_likelist_ids("test", 1)

    def test_real_write_helper_requires_explicit_success(self):
        with patch.object(dr, "ncm_get", return_value={}) as request:
            self.assertEqual(REAL_LIKE("test", 1), {})
        request.assert_called_once()

    def test_like_rate_limit_retries_are_bounded(self):
        with patch.object(dr, "ncm_get", return_value={"code": 405}) as request, patch.object(fb.time, "sleep") as pause:
            self.assertEqual(REAL_LIKE("test", 1, retries=2), {"code": 405})
        self.assertEqual(request.call_count, 3)
        self.assertEqual(pause.call_count, 2)

    def prepare_main(self):
        self.stack.enter_context(patch.object(dr, "DATA_DIR", Path(self.tmp)))
        self.stack.enter_context(patch.object(dr, "LOG_DIR", Path(self.tmp) / "logs"))
        self.stack.enter_context(patch.object(dr, "ensure_ncm_api"))
        self.stack.enter_context(patch.object(dr, "load_cookie", return_value="test"))
        self.stack.enter_context(patch.object(dr, "get_music_user_token", return_value="fake"))
        self.stack.enter_context(patch.object(dr, "fetch_web_developer_token", return_value="fake"))
        self.stack.enter_context(patch.object(dr, "AppleMusicClient", return_value=types.SimpleNamespace(storefront_info=lambda: {"data": [{"id": "cn"}]})))
        self.stack.enter_context(patch.object(sys, "__stdout__", io.StringIO()))
        self.stack.enter_context(patch.object(sys, "__stderr__", io.StringIO()))
        return self.stack.enter_context(patch.object(dr, "ncm_get", return_value={"data": {"profile": {"userId": 1}}}))

    def test_main_partial_failure_returns_nonzero_and_sanitized_manifest(self):
        self.prepare_main()
        self.initialize()
        fb.save_state(self.state)
        self.recent.return_value = [song(1), song(2)]
        self.scrobble.side_effect = [{"code": 200}, {"code": 500, "cookie": "SECRET"}]
        self.assertEqual(fb.main(["--scrobble-only", "--limit", "2"]), 1)
        manifest = (Path(self.tmp) / f"feedback-manifest-{dr.TODAY}.json").read_text()
        self.assertNotIn("SECRET", manifest)
        self.assertEqual(fb.load_state()["scrobbled_apple_ids"], ["1"])

    def test_main_likelist_error_prevents_all_mutations(self):
        request = self.prepare_main()
        request.side_effect = [{"data": {"profile": {"userId": 1}}}, {"code": 500}]
        self.assertEqual(fb.main([]), 1)
        self.like.assert_not_called()
        self.scrobble.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_main_corrupt_state_reports_and_makes_no_requests(self):
        request = self.prepare_main()
        self.path.write_text("broken")
        self.assertEqual(fb.main([]), 1)
        request.assert_not_called()
        self.assertEqual(self.path.read_text(), "broken")
        self.assertIn("状态损坏", str(fb.log.call_args_list))

    def test_main_invalid_match_metadata_prevents_startup_and_requests(self):
        request = self.prepare_main()
        record = {"apple": fb.asdict(song(1)), "status": "unknown", "netease": match("test", song(1))}
        for field, value in (("id", "1"), ("artists", None), ("score", "110"), ("album_id", [])):
            with self.subTest(field=field):
                bad = json.loads(json.dumps(record))
                bad["netease"][field] = value
                raw = json.dumps({"pending_likes": {"1": bad}})
                self.path.write_text(raw)
                self.assertEqual(fb.main([]), 1)
                self.assertEqual(self.path.read_text(), raw)
        dr.ensure_ncm_api.assert_not_called()
        request.assert_not_called()
        self.like.assert_not_called()
        self.scrobble.assert_not_called()

    def test_main_existing_legacy_state_dry_run_preserves_file(self):
        self.prepare_main()
        raw = json.dumps({"seen_recent_apple_ids": ["1"], "custom": "legacy"})
        self.path.write_text(raw)
        stamp = self.path.stat().st_mtime_ns
        self.favorites.return_value = ({"id": "f"}, [song(2)])
        self.recent.return_value = [song(3)]
        with patch.object(fb, "ncm_likelist_ids", return_value=set()):
            self.assertEqual(fb.main(["--dry-run", "--limit", "1"]), 0)
        self.assertEqual((self.path.read_text(), self.path.stat().st_mtime_ns), (raw, stamp))
        self.like.assert_not_called()
        self.scrobble.assert_not_called()

    def test_main_auth_expiry_aborts_second_phase(self):
        self.prepare_main()
        self.favorites.return_value = ({"id": "f"}, [song(1)])
        with patch.object(fb, "ncm_likelist_ids", return_value=set()):
            self.like.side_effect = dr.AuthenticationError("SECRET")
            self.assertEqual(fb.main([]), 1)
        self.recent.assert_not_called()
        self.assertNotIn("SECRET", str(fb.log.call_args_list))

    def test_main_dry_run_creates_no_state(self):
        self.prepare_main()
        self.recent.return_value = [song(1)]
        self.assertEqual(fb.main(["--dry-run", "--scrobble-only"]), 0)
        self.assertFalse(self.path.exists())
        self.scrobble.assert_not_called()

    def test_matching_regression(self):
        apple = fb.AppleSong("1", "Example", "Artist", "Album", 200000)
        good = dict(id=1, name="Example", ar=[{"name": "Artist"}], al={"name": "Album"}, dt=200000)
        bad = dict(id=2, name="Unrelated", ar=[{"name": "Other"}], al={"name": "Different"})
        self.assertGreater(fb.score_apple_vs_ncm(apple, good), fb.FEEDBACK_MIN_SCORE)
        self.assertLess(fb.score_apple_vs_ncm(apple, bad), fb.FEEDBACK_MIN_SCORE)

    def test_feedback_cannot_like_an_incompatible_recording(self):
        apple = fb.AppleSong("1", "Example", "Artist", "Album", 200000)
        candidate = dict(id=1, name="Example (Karaoke)", ar=[{"name": "Artist"}], al={"name": "Album"}, dt=200000)
        self.assertEqual(fb.score_apple_vs_ncm(apple, candidate), 0)
        apple.name = "Example (Live)"
        candidate["name"] = "Example"
        self.assertEqual(fb.score_apple_vs_ncm(apple, candidate), 0)

    def test_pagination_uses_shared_origin_validator(self):
        am = types.SimpleNamespace(base="https://api.music.apple.com", request=Mock(return_value=(200, {"data": [], "next": "https://evil.invalid/v1/me"})))
        with self.assertRaises(dr.TransportError):
            fb.am_paginate(am, "/v1/me/library/playlists")
        am.request.assert_called_once()


REAL_LIKE = fb.ncm_like


if __name__ == "__main__":
    unittest.main()

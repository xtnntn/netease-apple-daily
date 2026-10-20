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


def match(cookie, apple, **kwargs):
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

    def test_unknown_like_does_not_block_distinct_songs_across_runs(self):
        self.favorites.return_value = ({"id": "f"}, [song(1), song(2), song(3)])

        def write(cookie, nid, like):
            if nid == 1:
                raise dr.TransportError("secret", ambiguous=True)
            return {"code": 200}

        self.like.side_effect = write
        stats = fb.sync_likes("test", None, self.state, set(), False, 2)
        self.assertEqual([c.args[1] for c in self.like.call_args_list], [1, 2])
        self.assertEqual((stats["liked"], stats["errors"]), (1, 1))
        self.state = fb.load_state()
        self.assertEqual(self.state["pending_likes"]["1"]["status"], "unknown")
        self.assertEqual(self.state["liked_apple_ids"], ["2"])
        self.assertNotIn("secret", json.dumps(stats))

        stats = fb.sync_likes("test", None, self.state, {2}, False, 2)
        self.assertEqual([c.args[1] for c in self.like.call_args_list], [1, 2, 1, 3])
        self.assertEqual((stats["liked"], stats["errors"]), (1, 1))
        self.assertEqual(fb.load_state()["liked_apple_ids"], ["2", "3"])

    def test_unknown_like_defers_alias_but_processes_distinct_song(self):
        self.favorites.return_value = ({"id": "f"}, [song(1), song(2), song(3)])

        def candidate(cookie, apple, **kwargs):
            return match(cookie, song(1) if apple.apple_id == "2" else apple)

        self.like.side_effect = [dr.TransportError("timeout", ambiguous=True), {"code": 200}]
        with patch.object(fb, "match_netease", side_effect=candidate):
            stats = fb.sync_likes("test", None, self.state, set(), False, 3)
        self.assertEqual([c.args[1] for c in self.like.call_args_list], [1, 3])
        self.assertEqual((stats["liked"], stats["errors"], stats["deferred"]), (1, 1, 1))
        saved = fb.load_state()
        self.assertEqual(set(saved["pending_likes"]), {"1", "2"})
        self.assertEqual(saved["pending_likes"]["2"]["netease"]["id"], 1)
        self.assertEqual(saved["liked_apple_ids"], ["3"])
        self.state = saved
        self.favorites.return_value = ({"id": "f"}, [])
        fb.sync_likes("test", None, self.state, {1, 3}, False, 3)
        self.assertEqual(self.like.call_count, 2)
        self.assertEqual(set(fb.load_state()["liked_apple_ids"]), {"1", "2", "3"})
        self.assertEqual(fb.load_state()["pending_likes"], {})

    def test_like_rate_limit_still_stops_distinct_songs(self):
        self.favorites.return_value = ({"id": "f"}, [song(1), song(2)])
        for code in (405, 429):
            with self.subTest(code=code):
                self.like.reset_mock()
                self.like.return_value = {"code": code}
                stats = fb.sync_likes("test", None, self.state, set(), False, 2)
                self.like.assert_called_once_with("test", 1, True)
                self.assertEqual(stats["errors"], 1)
                self.assertEqual(self.state["liked_apple_ids"], [])

    def test_unknown_like_dry_run_preserves_pending_state(self):
        self.state["pending_likes"]["1"] = {
            "apple": fb.asdict(song(1)), "netease": match("test", song(1)), "status": "unknown",
        }
        fb.save_state(self.state)
        original = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.favorites.return_value = ({"id": "f"}, [song(2)])
        stats = fb.sync_likes("test", None, self.state, set(), True, 2)
        self.assertEqual(stats["liked"], 2)
        self.like.assert_not_called()
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), original)

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


def ncm(sid, name, artist, dt=200000, album="Album"):
    return {"id": sid, "name": name, "ar": [{"name": artist}], "al": {"name": album, "id": 9}, "dt": dt}


class FakeMatcher:
    """Records model inputs and replays scripted decisions; never touches the network."""

    def __init__(self, *decisions):
        self.enabled = True
        self.decisions = list(decisions)
        self.calls = []

    def select(self, source, candidates, *, allow_search=True):
        self.calls.append((source, candidates, allow_search))
        decision = self.decisions.pop(0)
        if isinstance(decision, Exception):
            raise decision
        candidate_id, terms = decision if isinstance(decision, tuple) else (decision, [])
        return types.SimpleNamespace(candidate_id=candidate_id, search_terms=terms, reason="model note")


class LikeAIMatchingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(fb.time, "sleep"))
        self.stack.enter_context(patch.object(fb, "log"))
        self.results = {}
        self.search = self.stack.enter_context(patch.object(
            fb, "ncm_cloudsearch", side_effect=lambda cookie, term, limit=8: list(self.results.get(term, self.default))))
        self.default = []

    def match(self, apple, matcher, review=None):
        return fb.match_netease("test", apple, ai_review=True, matcher=matcher, review=review)

    def test_strong_rule_match_skips_ai(self):
        apple = fb.AppleSong("1", "裸の勇者", "Vaundy", "Album", 200000)
        self.default = [ncm(11, "裸の勇者", "Vaundy", 200500)]
        matcher = FakeMatcher()
        result = self.match(apple, matcher)
        self.assertEqual((result["id"], result["match_method"]), (11, "rules"))
        self.assertEqual(matcher.calls, [])

    def test_weak_rule_match_is_reviewed_and_approved(self):
        apple = fb.AppleSong("1", "Someday, Somewhere", "JOLIN蔡依林", "Album", 200000)
        self.default = [ncm(21, "Someday, Somewhere", "蔡依林", 200000)]
        review = {}
        result = self.match(apple, FakeMatcher("21"), review)
        self.assertEqual((result["id"], result["match_method"]), (21, "ai_review"))
        self.assertEqual(review["ai"], "approved")
        self.assertEqual(review["rule_candidate"]["id"], 21)

    def test_review_can_reject_a_rule_match(self):
        # A passing rule score with a different artist identity must not be liked.
        apple = fb.AppleSong("1", "Scatterbrain", "nihmune", "Album", 200000)
        self.default = [ncm(31, "Scatterbrain", "nihmune band", 200000)]
        review = {}
        self.assertIsNone(self.match(apple, FakeMatcher(None), review))
        self.assertEqual(review["ai"], "rejected")

    def test_ai_recovers_cross_language_match(self):
        apple = fb.AppleSong("1", "Monstruo", "Ado", "Album", 200000)
        # Different album keeps the rule score under the minimum, so only AI can match.
        self.default = [ncm(41, "モンストロ", "Ado", 201000, "残夢"), ncm(42, "MONSTRUO", "Tainy", 230000)]
        self.assertLess(fb.score_apple_vs_ncm(apple, self.default[0]), fb.FEEDBACK_MIN_SCORE)
        matcher = FakeMatcher("41")
        result = self.match(apple, matcher)
        self.assertEqual((result["id"], result["match_method"]), (41, "ai"))
        supplied = {c["id"] for c in matcher.calls[0][1]}
        # Duration filter removes the 30-second-longer song before the model sees it.
        self.assertEqual(supplied, {"41"})

    def test_incompatible_versions_never_reach_model(self):
        apple = fb.AppleSong("1", "Kiss & Cry", "Utada", "Album", 200000)
        self.default = [ncm(51, "Kiss & Cry (Karaoke)", "宇多田ヒカル"), ncm(52, "Kiss & Cry (Live Version)", "宇多田ヒカル"),
                        ncm(53, "Kiss & Cry", "宇多田ヒカル")]
        matcher = FakeMatcher("53")
        self.assertEqual(self.match(apple, matcher)["id"], 53)
        self.assertEqual({c["id"] for c in matcher.calls[0][1]}, {"53"})

    def test_unknown_candidate_and_ai_failure_defer(self):
        apple = fb.AppleSong("1", "Monstruo", "Ado", "Album", 200000)
        self.default = [ncm(41, "モンストロ", "Ado")]
        for decision in ("999", dr.AIUnavailable("down")):
            with self.subTest(decision=decision), self.assertRaises(fb.MatchDeferred):
                self.match(apple, FakeMatcher(decision))

    def test_missing_matcher_defers_reviews_but_keeps_strong_matches(self):
        strong = fb.AppleSong("1", "Sober", "CREAM", "Album", 200000)
        self.default = [ncm(61, "Sober", "CREAM")]
        self.assertEqual(self.match(strong, None)["match_method"], "rules")
        weak = fb.AppleSong("2", "Monstruo", "Ado", "Album", 200000)
        self.default = [ncm(41, "モンストロ", "Ado")]
        with self.assertRaises(fb.MatchDeferred):
            self.match(weak, None)

    def test_one_supplement_search_then_final_decision(self):
        apple = fb.AppleSong("1", "Monstruo", "Ado", "Album", 200000)
        self.results["モンストロ Ado"] = [ncm(41, "モンストロ", "Ado")]
        matcher = FakeMatcher((None, ["モンストロ Ado"]), "41")
        self.assertEqual(self.match(apple, matcher)["id"], 41)
        self.assertEqual([call[2] for call in matcher.calls], [True, False])
        self.assertEqual(self.search.call_args_list[-1].args[1], "モンストロ Ado")

    def test_rules_only_mode_never_calls_model(self):
        apple = fb.AppleSong("1", "Someday, Somewhere", "JOLIN蔡依林", "Album", 200000)
        self.default = [ncm(21, "Someday, Somewhere", "蔡依林")]
        result = fb.match_netease("test", apple)
        self.assertEqual((result["id"], result["match_method"]), (21, "rules"))

    def test_sync_likes_defers_without_cooldown_and_stops_calling_model(self):
        songs = [fb.AppleSong(str(i), "Monstruo " + str(i), "Ado", "Album", 200000) for i in (1, 2)]
        strong = fb.AppleSong("3", "Sober", "CREAM", "Album", 200000)
        self.results = {t: [ncm(41, "モンストロ", "Ado")] for s in songs for t in fb._search_terms(s)}
        self.results.update({t: [ncm(61, "Sober", "CREAM")] for t in fb._search_terms(strong)})
        matcher = FakeMatcher(dr.AIUnavailable("down"))
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(fb, "STATE_FILE", Path(tmp) / "state.json"))
            stack.enter_context(patch.object(fb, "FEEDBACK_AI_MATCHING", True))
            stack.enter_context(patch.object(dr.AIMatcher, "from_env", return_value=matcher))
            stack.enter_context(patch.object(fb, "fetch_favorite_songs", return_value=({"id": "f"}, songs + [strong])))
            like = stack.enter_context(patch.object(fb, "ncm_like", return_value={"code": 200}))
            state = fb.load_state()
            stats = fb.sync_likes("test", None, state, set(), False, 10)
        self.assertEqual(len(matcher.calls), 1)
        self.assertEqual((stats["deferred"], stats["liked"]), (2, 1))
        like.assert_called_once_with("test", 61, True)
        self.assertEqual(state["unmatched_apple_ids"], {})

    def test_sync_likes_rejection_cools_down_and_records_review(self):
        song_ = fb.AppleSong("1", "Monstruo", "Ado", "Album", 200000)
        self.default = [ncm(41, "モンストロ", "Ado")]
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(fb, "STATE_FILE", Path(tmp) / "state.json"))
            stack.enter_context(patch.object(fb, "FEEDBACK_AI_MATCHING", True))
            stack.enter_context(patch.object(dr.AIMatcher, "from_env", return_value=FakeMatcher(None)))
            stack.enter_context(patch.object(fb, "fetch_favorite_songs", return_value=({"id": "f"}, [song_])))
            like = stack.enter_context(patch.object(fb, "ncm_like"))
            state = fb.load_state()
            stats = fb.sync_likes("test", None, state, set(), True, 10)
        like.assert_not_called()
        self.assertEqual((stats["ai_rejected"], stats["unmatched"]), (1, 1))
        self.assertIn("1", state["unmatched_apple_ids"])
        self.assertEqual(stats["items"][0]["ai"], "rejected")
        # Model explanations stay out of logs.
        self.assertNotIn("model note", str(fb.log.call_args_list))

    def test_invalid_ai_config_still_likes_strong_matches(self):
        strong = fb.AppleSong("3", "Sober", "CREAM", "Album", 200000)
        self.default = [ncm(61, "Sober", "CREAM")]
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(fb, "STATE_FILE", Path(tmp) / "state.json"))
            stack.enter_context(patch.object(fb, "FEEDBACK_AI_MATCHING", True))
            stack.enter_context(patch.object(dr.AIMatcher, "from_env", side_effect=dr.AIUnavailable("bad")))
            stack.enter_context(patch.object(fb, "fetch_favorite_songs", return_value=({"id": "f"}, [strong])))
            stack.enter_context(patch.object(fb, "ncm_like", return_value={"code": 200}))
            stats = fb.sync_likes("test", None, fb.load_state(), set(), False, 10)
        self.assertEqual(stats["liked"], 1)


REAL_LIKE = fb.ncm_like


if __name__ == "__main__":
    unittest.main()

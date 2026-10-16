"""Deterministic matching regressions with no config files or network."""
import importlib.util
import types
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_spec = importlib.util.spec_from_file_location("daily_matching_under_test", Path(__file__).resolve().parents[1] / "daily_recommend.py")
dr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dr
with patch.dict(os.environ, {}, clear=True), patch.object(Path, "is_file", return_value=False):
    _spec.loader.exec_module(dr)


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.source = dr.NeteaseSong(1, "Example Song", "Example Artist", "Example Album", 180000)
        self.song = {"id": "123", "attributes": {"name": "Example Song", "artistName": "Example Artist", "albumName": "Example Album", "durationInMillis": 181000}}

    def test_exact_match_score_and_early_stop(self):
        self.assertEqual(dr.score_match(self.source, self.song), 108)
        client = Mock()
        client.search_songs.return_value = [self.song]
        result = dr.match_catalog(client, self.source)
        self.assertEqual(result["id"], "123")
        self.assertEqual(result["score"], 108)
        self.assertEqual(client.search_songs.call_count, 1)

    def test_obviously_unrelated_song_is_unmatched(self):
        client = Mock()
        client._consecutive_429 = 0
        client.search_songs.return_value = [{"id": "999", "attributes": {"name": "独立作品", "artistName": "另一歌手", "albumName": "不同专辑", "durationInMillis": 350000}}]
        self.assertIsNone(dr.match_catalog(client, self.source))
        self.assertEqual(client.search_songs.call_count, 3)

    def test_normalization_and_empty_title(self):
        self.assertEqual(dr.normalize_title("  Example Song (Live) "), "example song")
        self.assertEqual(dr.score_match(self.source, {"attributes": {"name": ""}}), 0)

    def test_search_auth_failure_propagates(self):
        client = Mock()
        client.search_songs.side_effect = dr.AppleAuthenticationError("update credentials")
        with self.assertRaises(dr.AppleAuthenticationError):
            dr.match_catalog(client, self.source)
        self.assertEqual(client.search_songs.call_count, 1)


class AIMatchingFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(_consecutive_429=0)
        self.matcher = Mock(enabled=True)
        self.source = dr.NeteaseSong(3447749509, "雲を抜け風下へ私だけ", "ヨルシカ", "雲を抜け風下へ私だけ", 238398)
        self.song = {"id": "6817316271", "attributes": {
            "name": "Beyond the Clouds, Only Me Downwind", "artistName": "Yorushika",
            "albumName": "Beyond the Clouds, Only Me Downwind - Single", "durationInMillis": 238398}}
        self.client.search_songs.return_value = [self.song]

    def decision(self, sid=None, terms=None):
        return types.SimpleNamespace(candidate_id=sid, search_terms=terms or [], reason="metadata agrees")

    def test_localized_title_and_artist_can_match_below_rule_threshold(self):
        self.matcher.select.return_value = self.decision("6817316271")
        result = dr.match_catalog(self.client, self.source, matcher=self.matcher)
        self.assertEqual(result["id"], "6817316271")
        self.assertEqual(result["match_method"], "ai")
        self.assertLess(result["score"], 55)
        self.assertEqual(self.matcher.select.call_args.args[1][0]["artists"], "Yorushika")

    def test_transliterated_duet_and_localized_artist_cases(self):
        cases = [
            ("ジュビリー (M@STER VERSION)", "早見沙織 / 星街すいせい", "Jubilee (M@ster Version)", "Hoshimachi Suisei & Kaede Takagaki (CV: Saori Hayami)", 240892),
            ("ホウキボシ", "ロクデナシ", "Shooting Star", "Rokudenashi", 181069),
            ("悩みたいだけ", "家入レオ", "Nayamitaidake", "家入莉奥", 222679),
            ("旅路", "藤井風", "Tabiji", "藤井风", 277332),
            ("みかんハート", "C&K", "Mikan Heart", "C&K", 336875),
        ]
        for title, artist, translated, alias, duration in cases:
            with self.subTest(title=title):
                source = dr.NeteaseSong(1, title, artist, "Album", duration)
                song = {"id": "42", "attributes": {"name": translated, "artistName": alias, "albumName": "Album", "durationInMillis": duration}}
                self.client.search_songs.return_value = [song]
                self.matcher.select.return_value = self.decision("42")
                self.assertEqual(dr.match_catalog(self.client, source, matcher=self.matcher)["id"], "42")

    def test_strong_rules_match_spends_no_ai_call(self):
        source = dr.NeteaseSong(1, "Example", "Artist", "Album", 180000)
        self.client.search_songs.return_value = [{"id": "42", "attributes": {"name": "Example", "artistName": "Artist", "albumName": "Album", "durationInMillis": 180000}}]
        self.assertEqual(dr.match_catalog(self.client, source, matcher=self.matcher)["match_method"], "rules")
        self.matcher.select.assert_not_called()
        self.assertEqual(self.client.search_songs.call_count, 1)

    def test_alternative_query_then_one_final_decision(self):
        self.client.search_songs.side_effect = [[], [], [], [self.song]]
        self.matcher.select.side_effect = [self.decision(terms=["Yorushika Beyond the Clouds"]), self.decision("6817316271")]
        self.assertEqual(dr.match_catalog(self.client, self.source, matcher=self.matcher)["id"], "6817316271")
        self.assertEqual(self.client.search_songs.call_count, 4)
        self.assertFalse(self.matcher.select.call_args.kwargs["allow_search"])

    def test_foreign_region_or_missing_catalog_never_invented(self):
        self.client.search_songs.return_value = []
        self.matcher.select.return_value = self.decision()
        self.assertIsNone(dr.match_catalog(self.client, self.source, matcher=self.matcher))
        self.matcher.select.return_value = self.decision("1747230784")
        self.assertIsNone(dr.match_catalog(self.client, self.source, matcher=self.matcher))

    def test_unavailable_ai_does_not_use_weak_fallback(self):
        source = dr.NeteaseSong(1, "Kiss & Cry", "宇多田ヒカル", "EP", 307666)
        self.client.search_songs.return_value = [{"id": "42", "attributes": {"name": "Kiss & Cry", "artistName": "Utada", "albumName": "EP", "durationInMillis": 305893}}]
        self.matcher.select.side_effect = dr.AIUnavailable("failure")
        self.assertIsNone(dr.match_catalog(self.client, source, matcher=self.matcher))

    def test_auth_failure_during_extra_search_propagates(self):
        self.matcher.select.return_value = self.decision(terms=["alternate"])
        self.client.search_songs.side_effect = [[], [], [], dr.AppleAuthenticationError("expired")]
        with self.assertRaises(dr.AppleAuthenticationError):
            dr.match_catalog(self.client, self.source, matcher=self.matcher)

    def test_malformed_candidates_are_skipped_without_aborting_batch(self):
        for bad in (None, {"id": "1", "attributes": []},
                    {"id": "2", "attributes": {**self.song["attributes"], "durationInMillis": "238398"}},
                    {"id": "3", "attributes": {**self.song["attributes"], "name": None}}):
            with self.subTest(bad=bad):
                self.client.search_songs.return_value = [bad, self.song]
                self.matcher.select.return_value = self.decision("6817316271")
                self.assertEqual(dr.match_catalog(self.client, self.source, matcher=self.matcher)["id"], "6817316271")

    def test_rule_fast_path_cannot_bypass_duration_filter(self):
        source = dr.NeteaseSong(1, "Example", "Artist", "Album", 180000)
        self.client.search_songs.return_value = [{"id": "1", "attributes": {"name": "Example", "artistName": "Artist", "albumName": "Album", "durationInMillis": 90000}}]
        self.matcher.select.return_value = self.decision()
        self.assertIsNone(dr.match_catalog(self.client, source, matcher=self.matcher))
        self.assertEqual(self.matcher.select.call_args.args[1], [])

    def test_large_duration_mismatch_is_not_offered_to_ai(self):
        self.song["attributes"]["durationInMillis"] = 100000
        self.matcher.select.return_value = self.decision("6817316271")
        self.assertIsNone(dr.match_catalog(self.client, self.source, matcher=self.matcher))
        self.assertEqual(self.matcher.select.call_args.args[1], [])


class VersionTests(unittest.TestCase):
    def test_kiss_cry_karaoke_first_is_never_selected(self):
        source = dr.NeteaseSong(666446, "Kiss & Cry", "宇多田ヒカル", "Beautiful World / Kiss & Cry", 307666)
        base = {"artistName": "Utada", "albumName": "Beautiful World / Kiss & Cry - EP", "durationInMillis": 306667}
        karaoke = {"id": "1445033680", "attributes": {**base, "name": "Kiss & Cry (Karaoke)"}}
        original = {"id": "1445033660", "attributes": {**base, "name": "Kiss & Cry"}}
        for enabled in (False, True):
            matcher = Mock(enabled=enabled)
            matcher.select.return_value = types.SimpleNamespace(candidate_id="1445033660", search_terms=[])
            client = Mock(_consecutive_429=0)
            client.search_songs.return_value = [karaoke, original]
            result = dr.match_catalog(client, source, matcher=matcher)
            self.assertEqual(result["id"], "1445033660")
            self.assertEqual(dr.score_match(source, karaoke), 0)
            if enabled:
                self.assertEqual([s["id"] for s in matcher.select.call_args.args[1]], ["1445033660"])

    def test_incompatible_versions_cannot_trigger_early_stop(self):
        source = dr.NeteaseSong(1, "Example", "Artist", "Album", 180000)
        bad = {"id": "1", "attributes": {"name": "Example (Karaoke)", "artistName": "Artist", "albumName": "Album", "durationInMillis": 180000}}
        good = {"id": "2", "attributes": {**bad["attributes"], "name": "Example"}}
        client = Mock(_consecutive_429=0)
        client.search_songs.side_effect = [[bad], [bad], [good]]
        self.assertEqual(dr.match_catalog(client, source, matcher=Mock(enabled=False))["id"], "2")
        client.search_songs.side_effect = None
        client.search_songs.return_value = [bad]
        self.assertIsNone(dr.match_catalog(client, source, matcher=Mock(enabled=False)))

    def test_multilingual_qualifiers_and_normal_words(self):
        for suffix in ("(Karaoke)", "【伴奏】", "（カラオケ）", "(Instrumental)", "(Off Vocal)", "(Live Version)", "(Game Version)", "(Solo Remix)", "(Acoustic)"):
            self.assertFalse(dr.versions_compatible("Example", "Example " + suffix), suffix)
        self.assertTrue(dr.versions_compatible("Example (Karaoke)", "Example (Instrumental)"))
        self.assertTrue(dr.versions_compatible("Example", "Example (Remastered 2018)"))
        self.assertEqual(dr.version_tags("Alive"), set())
        self.assertEqual(dr.version_tags("Delivery"), set())
        self.assertEqual(dr.version_tags("Live"), set())
        self.assertTrue(dr.versions_compatible("為你而活", "I Live For You"))
        self.assertTrue(dr.versions_compatible("万岁", "Long Live"))
        self.assertFalse(dr.versions_compatible("Example", "Example 伴奏"))
        self.assertFalse(dr.versions_compatible("Example", "Example カラオケ"))
        self.assertFalse(dr.versions_compatible("Example", "Example Live Version"))
        self.assertTrue(dr.versions_compatible("ショートケーキ", "Shortcake"))
        self.assertFalse(dr.versions_compatible("Example (Live)", "Example"))


if __name__ == "__main__":
    unittest.main()

"""Historical matching previews never invoke playlist or feedback writes."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


name = "preview_matching_under_test"
spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scripts/preview_matching.py")
preview = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {}, clear=True), patch.object(Path, "is_file", return_value=False):
    spec.loader.exec_module(preview)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manifest = Path(self.temp.name) / "manifest.json"
        self.row = {"netease": {"id": 1, "name": "旅路", "artists": "藤井風", "album": "旅路", "duration_ms": 277332}, "match": None, "status": "unmatched"}
        self.manifest.write_text(json.dumps({"tracks": [self.row]}))
        self.original = self.manifest.read_bytes()

    def test_preview_search_only_and_json_output(self):
        client = Mock(_consecutive_429=0, base="https://api.music.apple.com")
        client.request.return_value = (200, {"data": []})
        output = io.StringIO()
        def matching(catalog, source, *, matcher):
            catalog.search_songs("Tabiji", limit=25)
            self.assertFalse(hasattr(catalog, "create_playlist"))
            return {"id": "42", "match_method": "ai"}
        with patch.object(preview.dr, "AppleMusicClient", return_value=client), patch.object(preview.dr, "fetch_web_developer_token", return_value="dev"), patch.object(preview.dr, "get_music_user_token", return_value="user"), patch.object(preview.dr.AIMatcher, "from_env", return_value=Mock(enabled=True)), patch.object(preview.dr, "match_catalog", side_effect=matching), patch.object(preview.dr, "ensure_ncm_api") as startup, patch.object(preview.dr, "ncm_get") as ncm, contextlib.redirect_stdout(output):
            self.assertEqual(preview.main([str(self.manifest)]), 0)
            startup.assert_not_called()
            ncm.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["preview"])
        self.assertEqual(self.manifest.read_bytes(), self.original)
        client.search_songs.assert_called_once_with("Tabiji", limit=25)
        client.create_playlist.assert_not_called()
        with self.assertRaises(preview.dr.TransportError):
            client.request("/v1/me/library/playlists", method="POST", body={})
        with self.assertRaises(preview.dr.TransportError):
            client.request("/v1/catalog/jp/search?term=Tabiji")

    def test_filters_and_output_exclusive_creation(self):
        self.manifest.write_text(json.dumps({"tracks": [self.row, {**self.row, "status": "matched"}]}))
        output = Path(self.temp.name) / "preview.json"
        output.write_text("keep-existing")
        with patch.object(preview.dr, "AppleMusicClient"), patch.object(preview.dr, "fetch_web_developer_token", return_value="dev"), patch.object(preview.dr, "get_music_user_token", return_value="user"), patch.object(preview.dr.AIMatcher, "from_env", return_value=Mock(enabled=False)), patch.object(preview.dr, "match_catalog", return_value=None) as match, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(preview.main([str(self.manifest), "--unmatched-only", "--limit", "1", "--output", str(output)]), 1)
            self.assertEqual(match.call_count, 1)
        self.assertEqual(output.read_text(), "keep-existing")

    def test_bad_manifest_or_overwrite_input_stops_before_network(self):
        with patch.object(preview.dr, "AppleMusicClient") as client, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                preview.main([str(self.manifest), "--output", str(self.manifest)])
            for field, value in (("name", None), ("name", ""), ("duration_ms", "unknown")):
                self.manifest.write_text(json.dumps({"tracks": [{**self.row, "netease": {**self.row["netease"], field: value}}]}))
                self.assertEqual(preview.main([str(self.manifest)]), 1)
                client.assert_not_called()
            self.manifest.write_text("broken")
            self.assertEqual(preview.main([str(self.manifest)]), 1)
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()

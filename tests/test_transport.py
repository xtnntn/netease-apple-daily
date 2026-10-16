"""Offline transport regression tests; never load project config or credentials."""
import base64
import contextlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse


def load_daily():
    name = "daily_transport_under_test"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "daily_recommend.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # Import has intentional environment side effects: isolate both config and proxy edits.
    with patch.dict(os.environ, {}, clear=True), patch.object(Path, "is_file", return_value=False):
        spec.loader.exec_module(module)
    return module


dr = load_daily()


class Response:
    def __init__(self, body=b"{}", status=200):
        self.body, self.status = body, status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def jwt(exp=None, **claims):
    payload = {"iss": "AMPWebPlay", "exp": time.time() + 3600 if exp is None else exp, **claims}
    enc = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return enc({"alg": "ES256"}) + "." + enc(payload) + ".signature"


class TransportTests(unittest.TestCase):
    def test_cookie_post_body_and_timestamp_business_query(self):
        cookie = "MUSIC_U=private+&值; __csrf=raw=="
        with patch.object(dr, "_urlopen", return_value=Response(b'{"code":200,"ids":[]}')) as opened:
            dr.ncm_get("/likelist", cookie, {"uid": "42", "timestamp": "old"})
        request = opened.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"cookie": cookie})
        self.assertEqual(query["uid"], ["42"])
        self.assertNotEqual(query["timestamp"], ["old"])
        self.assertNotIn("cookie", query)
        self.assertNotIn(cookie, request.full_url)

    def test_network_errors_sanitized_and_endpoint_ambiguity(self):
        for endpoint, ambiguous in (("/login/status", False), ("/likelist", False), ("/cloudsearch", False), ("/like", True), ("/scrobble", True)):
            with self.subTest(endpoint=endpoint), patch.object(dr, "_urlopen", side_effect=urllib.error.URLError("cookie=PRIVATE")):
                with self.assertRaises(dr.TransportError) as caught:
                    dr.ncm_get(endpoint, "PRIVATE")
                self.assertEqual(caught.exception.ambiguous, ambiguous)
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_server_failure_ambiguity_depends_on_endpoint(self):
        for status in (500, 502, 503, 504):
            for endpoint, ambiguous in (("/likelist", False), ("/cloudsearch", False), ("/like", True), ("/scrobble", True)):
                with self.subTest(status=status, endpoint=endpoint), patch.object(dr, "http_json", return_value=(status, {"code": status})):
                    expected = dr.TransportError if ambiguous else dr.APIError
                    with self.assertRaises(expected) as caught:
                        dr.ncm_get(endpoint, "private")
                    if ambiguous:
                        self.assertTrue(caught.exception.ambiguous)
                    else:
                        self.assertEqual(caught.exception.status, status)

    def test_rate_limit_error_body_is_sanitized(self):
        for status in (405, 429):
            with self.subTest(status=status), patch.object(dr, "http_json", return_value=(status, {"code": status, "message": "PRIVATE", "cookie": "PRIVATE"})):
                self.assertEqual(dr.ncm_get("/like", "PRIVATE"), {"code": status})

    def test_http_error_body_is_not_returned_or_logged(self):
        error = urllib.error.HTTPError("https://invalid/?cookie=PRIVATE", 500, "PRIVATE", {}, io.BytesIO(b'{"code":500,"message":"PRIVATE","cookie":"PRIVATE"}'))
        with patch.object(dr, "_urlopen", side_effect=error):
            self.assertEqual(dr.http_json("https://invalid/"), (500, {"code": 500}))
        error = urllib.error.HTTPError("http://invalid/", 500, "PRIVATE", {}, io.BytesIO(b"PRIVATE"))
        with patch.object(dr, "_urlopen", side_effect=error), self.assertRaises(dr.APIError) as caught:
            dr.ncm_get("/likelist", "PRIVATE")
        self.assertEqual(caught.exception.status, 500)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_ncm_auth_and_login_profile_fail_closed(self):
        cases = [(401, {}), (200, {"code": 301}), (200, {"data": {"code": 302}}), (200, {"code": 200, "data": {"profile": None}})]
        for response in cases:
            with self.subTest(response=response), patch.object(dr, "http_json", return_value=response):
                with self.assertRaises(dr.NCMAuthenticationError):
                    dr.ncm_get("/login/status", "private")
        with patch.object(dr, "http_json", return_value=(200, {"data": {"profile": {"userId": 42}}})):
            self.assertEqual(dr.ncm_get("/login/status", "private")["data"]["profile"]["userId"], 42)

    def test_business_failure_and_missing_code_preserved(self):
        for data in ({"code": 405}, {}, {"code": 500}):
            with patch.object(dr, "http_json", return_value=(200, data)):
                self.assertEqual(dr.ncm_get("/like", "private"), data)
        with patch.object(dr, "http_json", return_value=(429, {})):
            self.assertEqual(dr.ncm_get("/like", "private")["code"], 429)

    def test_api_business_error_body_is_sanitized(self):
        with patch.object(dr, "http_json", return_value=(200, {"code": 405, "message": "PRIVATE", "cookie": "PRIVATE"})):
            self.assertEqual(dr.ncm_get("/like", "PRIVATE"), {"code": 405})

    def test_invalid_json_network_and_write_timeout(self):
        for value in (b"<html>PRIVATE</html>", b"\xff"):
            with patch.object(dr, "_urlopen", return_value=Response(value)), self.assertRaises(dr.TransportError) as caught:
                dr.http_json("https://invalid/", method="POST", body={})
            self.assertTrue(caught.exception.ambiguous)
            self.assertNotIn("PRIVATE", str(caught.exception))
        with patch.object(dr, "_urlopen", side_effect=socket.timeout("PRIVATE")), self.assertRaises(dr.TransportError) as caught:
            dr.http_json("https://invalid/", method="POST")
        self.assertTrue(caught.exception.ambiguous)

    def test_redirect_handler_never_creates_redirect_request(self):
        self.assertIsNone(dr._NoRedirect().redirect_request(Mock(), None, 302, "", {}, "https://evil.invalid/"))
        opener = Mock()
        with patch.object(dr.urllib.request, "build_opener", return_value=opener) as build:
            dr._urlopen(Mock(), 7)
        self.assertIsInstance(build.call_args.args[0], dr._NoRedirect)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)

    def test_same_origin_pagination(self):
        self.assertEqual(dr.apple_page_path("https://api.music.apple.com/v1/me/library/songs?offset=10"), "/v1/me/library/songs?offset=10")
        self.assertEqual(dr.apple_page_path("/v1/me/library/songs"), "/v1/me/library/songs")
        for value in ("https://evil.invalid/v1/x", "http://api.music.apple.com/v1/x", "//api.music.apple.com/v1/x", "https://api.music.apple.com.evil/v1/x", "https://u@api.music.apple.com/v1/x", "/v1/../x", "/v1/%2e%2e/x", "/v1/x#fragment", "/v1/x\n", "?offset=3"):
            with self.subTest(value=value), self.assertRaises(dr.TransportError):
                dr.apple_page_path(value)


class TokenTests(unittest.TestCase):
    def test_public_page_redirect_and_token_discovery(self):
        token = jwt()
        redirect = urllib.error.HTTPError(
            "https://music.apple.com/cn", 301, "Moved Permanently",
            {"Location": "https://music.apple.com/cn/new"}, io.BytesIO(b"PRIVATE"),
        )
        html = b'<script src="/assets/main.hash.js"></script>'
        with patch.object(dr, "DEVELOPER_TOKEN", ""), patch.object(dr, "_urlopen", side_effect=[redirect, Response(html), Response(token.encode())]) as opened:
            self.assertEqual(dr.fetch_web_developer_token(), token)
        requests = [call.args[0] for call in opened.call_args_list]
        self.assertEqual([req.full_url for req in requests], [
            "https://music.apple.com/cn", "https://music.apple.com/cn/new",
            "https://music.apple.com/assets/main.hash.js",
        ])
        for request in requests:
            self.assertEqual({key.lower() for key in request.headers}, {"user-agent"})
            self.assertIsNone(request.data)

    def test_public_redirect_origin_and_count_bounded(self):
        targets = ["https://evil.invalid/PRIVATE", "http://music.apple.com/PRIVATE",
                   "https://user@music.apple.com/PRIVATE", "https://music.apple.com:444/PRIVATE"]
        for target in targets:
            redirect = urllib.error.HTTPError("https://music.apple.com/cn", 302, "PRIVATE", {"Location": target}, io.BytesIO(b"PRIVATE"))
            with self.subTest(target=target), patch.object(dr, "_urlopen", side_effect=redirect) as opened:
                with self.assertRaises(dr.TransportError) as caught:
                    dr._apple_web_text("https://music.apple.com/cn")
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(opened.call_count, 1)
        redirects = [urllib.error.HTTPError("https://music.apple.com/cn", 301, "PRIVATE", {"Location": "/cn/new"}, io.BytesIO()) for _ in range(4)]
        with patch.object(dr, "_urlopen", side_effect=redirects) as opened, self.assertRaisesRegex(dr.TransportError, "上限"):
            dr._apple_web_text("https://music.apple.com/cn")
        self.assertEqual(opened.call_count, 4)

    def test_changed_bundle_paths_jwt_candidates_and_expiry(self):
        html = '<script src="assets/main.hash.js?v=2"></script><link rel="modulepreload" href="/assets/extra.js">'
        bundle = " ".join([jwt(exp=0), jwt(exp="bad"), jwt(exp=math.inf), jwt(iss="other"), jwt()])
        with patch.object(dr, "DEVELOPER_TOKEN", ""), patch.object(dr, "_apple_web_text", side_effect=[html, bundle]) as get:
            token = dr.fetch_web_developer_token()
        self.assertEqual(dr._jwt_payload(token)["iss"], "AMPWebPlay")
        self.assertTrue(get.call_args_list[1].args[0].endswith("/assets/main.hash.js?v=2"))

    def test_configured_token_precedence_and_bad_expiry(self):
        configured = jwt(iss="configured-owner")
        with patch.object(dr, "DEVELOPER_TOKEN", configured), patch.object(dr, "_apple_web_text") as get:
            self.assertEqual(dr.fetch_web_developer_token(), configured)
            get.assert_not_called()
        for token in ("invalid", jwt(exp=0), jwt(exp="bad"), jwt(exp=True), jwt(exp=math.nan)):
            with patch.object(dr, "DEVELOPER_TOKEN", token), self.assertRaises(dr.AppleAuthenticationError):
                dr.fetch_web_developer_token()

    def test_discovery_error_categories(self):
        with patch.object(dr, "DEVELOPER_TOKEN", ""):
            with patch.object(dr, "_apple_web_text", return_value="no assets"), self.assertRaisesRegex(dr.AppleAuthenticationError, "JS"):
                dr.fetch_web_developer_token()
            with patch.object(dr, "_apple_web_text", side_effect=['<script src="/assets/x.js">', dr.TransportError("offline")]), self.assertRaises(dr.TransportError):
                dr.fetch_web_developer_token()
            with patch.object(dr, "_apple_web_text", side_effect=['<script src="/assets/x.js">', jwt(exp=0)]), self.assertRaisesRegex(dr.AppleAuthenticationError, "developer token"):
                dr.fetch_web_developer_token()


class AppleTests(unittest.TestCase):
    def client(self):
        with patch.object(dr, "DEVELOPER_TOKEN", ""):
            client = dr.AppleMusicClient("old", "private")
        client._pace = Mock()
        return client

    def test_read_refresh_once_then_auth_abort(self):
        client = self.client()
        with patch.object(dr, "fetch_web_developer_token", return_value="new") as refresh, patch.object(dr, "http_json", side_effect=[(401, {}), (403, {})]) as request:
            with self.assertRaises(dr.AppleAuthenticationError):
                client.request("/v1/me/storefront")
            self.assertEqual(refresh.call_count, 1)
            self.assertEqual(request.call_count, 2)
        self.assertEqual(client.dev, "new")

    def test_read_refresh_can_recover(self):
        client = self.client()
        with patch.object(dr, "fetch_web_developer_token", return_value="new"), patch.object(dr, "http_json", side_effect=[(401, {}), (200, {"data": []})]):
            self.assertEqual(client.request("/v1/me/storefront"), (200, {"data": []}))

    def test_configured_token_and_mutation_auth_never_refresh(self):
        for configured, method in (("configured", "GET"), ("", "POST")):
            with patch.object(dr, "DEVELOPER_TOKEN", configured):
                client = dr.AppleMusicClient("old", "private")
            client._pace = Mock()
            with patch.object(dr, "http_json", return_value=(401, {})) as request, patch.object(dr, "fetch_web_developer_token") as refresh:
                with self.assertRaises(dr.AppleAuthenticationError):
                    client.request("/v1/me/library/playlists", method=method)
                self.assertEqual(request.call_count, 1)
                refresh.assert_not_called()

    def test_write_timeout_or_server_error_no_create_replay(self):
        for outcome in (dr.TransportError("unknown", ambiguous=True), (500, {})):
            client = self.client()
            with patch.object(dr, "http_json", side_effect=[outcome]) as request:
                with self.assertRaises(RuntimeError):
                    client.create_playlist("test", "", ["1"], "folder")
                self.assertEqual(request.call_count, 1)

    def test_redirect_and_foreign_path_rejected(self):
        client = self.client()
        with patch.object(dr, "http_json", return_value=(302, {})) as request:
            with self.assertRaises(dr.TransportError):
                client.request("/v1/me/storefront")
            self.assertEqual(request.call_count, 1)
        with patch.object(dr, "http_json") as request, self.assertRaises(dr.TransportError):
            client.request("https://evil.invalid/v1/x")
        request.assert_not_called()

    def test_search_failure_is_not_unmatched(self):
        client = self.client()
        for code in (429, 500):
            with patch.object(client, "request", return_value=(code, {})), self.assertRaises(dr.APIError):
                client.search_songs("title")

    def test_apple_error_helpers_never_echo_response(self):
        client = self.client()
        actions = [client.storefront_info, client.list_library_playlists, client.list_playlist_folders,
                   lambda: client.create_playlist_folder("test"),
                   lambda: client.create_playlist("test", "", ["1"]),
                   lambda: client.add_tracks("test", ["1"])]
        for action in actions:
            with patch.object(client, "request", return_value=(500, {"message": "PRIVATE"})), self.assertRaises(dr.APIError) as caught:
                action()
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_retry_after_finite_nonnegative_bounded(self):
        for value in ("nan", "inf", "-inf", "-4", "garbage", "9999999999", "0", "1.5", "Wed, 21 Oct 2037 07:28:00 GMT"):
            with self.subTest(value=value):
                wait = dr.AppleMusicClient._retry_after_seconds({"_retry_after": value}, 0)
                self.assertTrue(math.isfinite(wait))
                self.assertGreaterEqual(wait, 0)
                self.assertLessEqual(wait, 60)
        client = self.client()
        client.max_429_retries = 2
        with patch.object(dr, "http_json", return_value=(429, {"_retry_after": "0"})) as request, patch.object(dr.time, "sleep"):
            self.assertEqual(client.request("/v1/me/storefront")[0], 429)
            self.assertEqual(request.call_count, 3)


class DailySkipTests(unittest.TestCase):
    def test_existing_folder_playlist_skips_without_apple_writes(self):
        playlist_name = f"{dr.PLAYLIST_PREFIX}-{dr.TODAY}"
        replies = {
            ("127.0.0.1", "/login/status"): {"data": {"profile": {"userId": 42}}},
            ("127.0.0.1", "/recommend/songs"): {"code": 200, "data": {"dailySongs": [{"id": 1, "name": "Song"}]}},
            ("api.music.apple.com", "/v1/me/storefront"): {"data": [{"id": "cn"}]},
            ("api.music.apple.com", "/v1/me/library/playlist-folders/folder"): {"data": [{"id": "folder", "attributes": {"name": "Existing folder"}}]},
            ("api.music.apple.com", "/v1/me/library/playlist-folders/folder/children"): {"data": [{"id": "existing", "type": "library-playlists", "attributes": {"name": playlist_name}}]},
        }

        def offline_api(request, timeout):
            url = urllib.parse.urlsplit(request.full_url)
            if url.hostname == "api.music.apple.com":
                self.assertEqual(request.method, "GET", "Apple write attempted before daily skip")
            else:
                self.assertEqual(request.method, "POST")
                self.assertNotIn("cookie", urllib.parse.parse_qs(url.query))
            key = (url.hostname, url.path)
            self.assertIn(key, replies, "Unexpected request before daily skip")
            return Response(json.dumps(replies[key]).encode())

        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            folder = Path(directory)
            for name, value in (("LOG_DIR", folder / "logs"), ("DATA_DIR", folder / "data"),
                                ("NCM_API_BASE", "http://127.0.0.1:3000"), ("PLAYLIST_FOLDER_ID", "folder"),
                                ("SKIP_IF_EXISTS", True), ("DEVELOPER_TOKEN", "")):
                stack.enter_context(patch.object(dr, name, value))
            stack.enter_context(patch.object(dr, "ensure_ncm_api"))
            stack.enter_context(patch.object(dr, "load_cookie", return_value="private"))
            stack.enter_context(patch.object(dr, "get_music_user_token", return_value="private"))
            stack.enter_context(patch.object(dr, "fetch_web_developer_token", return_value="developer"))
            stack.enter_context(patch.object(dr.AppleMusicClient, "_pace"))
            opened = stack.enter_context(patch.object(dr, "_urlopen", side_effect=offline_api))
            matched = stack.enter_context(patch.object(dr, "match_catalog"))
            stack.enter_context(patch.object(dr, "open", return_value=io.StringIO(), create=True))
            for stream in ("stdout", "stderr", "__stdout__", "__stderr__"):
                stack.enter_context(patch.object(dr.sys, stream, io.StringIO()))
            self.assertEqual(dr.main(), 0)
            matched.assert_not_called()
            self.assertEqual(opened.call_count, len(replies))
            manifest = json.loads((folder / "data" / f"manifest-{dr.TODAY}.json").read_text())
            self.assertTrue(manifest["skipped"])
            self.assertEqual(manifest["playlist"]["id"], "existing")
            self.assertEqual(manifest["folder"]["id"], "folder")
            self.assertNotIn("private", json.dumps(manifest))


class StartupTests(unittest.TestCase):
    def test_healthy_api_never_starts(self):
        with patch.object(dr, "ncm_alive", return_value=True), patch.object(dr.subprocess, "run") as run:
            dr.ensure_ncm_api()
            run.assert_not_called()

    def test_custom_command_bounded_sanitized_env_and_logs(self):
        env = {"NCM_API_START_CMD": "start private-secret", "MUSIC_USER_TOKEN": "private", "DEVELOPER_TOKEN": "private", "APPLE_PRIVATE_KEY": "private", "AM_CONFIG": "private", "PATH": "/bin"}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(dr, "ncm_alive", side_effect=[False, True]), patch.object(dr.subprocess, "run", return_value=Mock(returncode=0)) as run, contextlib.redirect_stdout(output):
            dr.ensure_ncm_api()
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual(run.call_args.kwargs["cwd"], str(dr.ROOT))
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], "/bin")
        self.assertNotIn("MUSIC_USER_TOKEN", run.call_args.kwargs["env"])
        self.assertNotIn("DEVELOPER_TOKEN", run.call_args.kwargs["env"])
        self.assertNotIn("APPLE_PRIVATE_KEY", run.call_args.kwargs["env"])
        self.assertNotIn("AM_CONFIG", run.call_args.kwargs["env"])
        self.assertNotIn("private-secret", output.getvalue())

    def test_missing_docker_actionable(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(dr, "ncm_alive", return_value=False), patch.object(dr.shutil, "which", return_value=None), self.assertRaisesRegex(SystemExit, "NCM_API_START_CMD"):
            dr.ensure_ncm_api()

    def test_start_failure_timeout_and_readiness_timeout(self):
        with patch.dict(os.environ, {"NCM_API_START_CMD": "start"}, clear=True), patch.object(dr, "ncm_alive", return_value=False):
            with patch.object(dr.subprocess, "run", return_value=Mock(returncode=7)), self.assertRaisesRegex(SystemExit, "7"):
                dr.ensure_ncm_api()
            with patch.object(dr.subprocess, "run", side_effect=subprocess.TimeoutExpired("PRIVATE", 30)), self.assertRaisesRegex(SystemExit, "30") as caught:
                dr.ensure_ncm_api()
            self.assertNotIn("PRIVATE", str(caught.exception))
            with patch.object(dr.subprocess, "run", return_value=Mock(returncode=0)), patch.object(dr.time, "monotonic", side_effect=[0, 41]), self.assertRaisesRegex(SystemExit, "40"):
                dr.ensure_ncm_api()


if __name__ == "__main__":
    unittest.main()

"""Shell entry point regressions using isolated files and fake services only."""
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_COOKIE = "MUSIC_U=fake-cookie-only; __csrf=sample&value+space"
STUB = r'''
import json, os, pathlib, stat, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
event = {"tool": name, "args": args, "cwd": os.getcwd()}
if name == "python3":
    event["env"] = {k: os.environ.get(k) for k in (
        "COOKIE_FILE", "LOG_DIR", "DATA_DIR", "AM_CONFIG", "FEEDBACK_STATE_FILE",
        "NCM_API_START_CMD", "PLAYLIST_DESCRIPTION", "PATH")}
if name == "curl":
    for index, arg in enumerate(args):
        if arg.startswith("cookie@"):
            path = pathlib.Path(arg.split("@", 1)[1])
            event["body"] = path.read_text()
            event["body_mode"] = stat.S_IMODE(path.stat().st_mode)
        if arg == "-D":
            path = pathlib.Path(args[index + 1])
            event["header_mode"] = stat.S_IMODE(path.stat().st_mode)
            path.write_text(os.environ.get("STUB_HEADERS", ""))
if name == "chmod":
    path = pathlib.Path(args[-1])
    event["previous"] = path.read_text() if path.exists() else None
with open(os.environ["STUB_EVENTS"], "a") as stream:
    stream.write(json.dumps(event) + "\n")
if name == "python3":
    sys.exit(int(os.environ.get("STUB_PYTHON_RC", "0")))
if name == "curl":
    if any("/login/status" in a for a in args):
        print(os.environ.get("STUB_STATUS", '{"data":{"code":200,"profile":{"nickname":"tester","userId":42}}}'))
    elif any("/qr/key" in a for a in args):
        print('{"data":{"unikey":"test-key"}}')
    elif any("/qr/create" in a for a in args):
        print('{"data":{"qrurl":"https://example.invalid/qr","qrimg":"data:image/png;base64,aW1hZ2U="}}')
    elif any("/qr/check" in a for a in args):
        state = pathlib.Path(os.environ["STUB_POLL_COUNT"])
        index = int(state.read_text()) if state.exists() else 0
        state.write_text(str(index + 1))
        sequence = json.loads(os.environ.get("STUB_POLL", "[]"))
        result = sequence[min(index, len(sequence)-1)]
        if isinstance(result, int):
            print("sensitive response must stay hidden", file=sys.stderr)
            sys.exit(result)
        print(result if isinstance(result, str) else json.dumps(result))
    else:
        sys.exit("unexpected network endpoint")
elif name == "jq":
    try:
        data = json.load(sys.stdin)
        expression = args[-1]
        if '.data.unikey' in expression:
            value = data.get("data", {}).get("unikey")
        elif '.data.qrurl' in expression:
            value = data.get("data", {}).get("qrurl")
        elif '.data.qrimg' in expression:
            value = data.get("data", {}).get("qrimg", "")
        elif expression.startswith('.code'):
            value = data.get("code")
            if not isinstance(value, int): sys.exit(1)
        elif expression.startswith('.cookie'):
            value = data.get("cookie", "")
        elif 'and (' in expression:
            d = data.get("data", {})
            profile = d.get("profile")
            uid = float(profile.get("userId", 0)) if isinstance(profile, dict) else 0
            sys.exit(0 if d.get("code") == 200 and uid > 0 and uid.is_integer() else 1)
        else:
            d = data.get("data", {})
            value = {"code": d.get("code"), "profile": (d.get("profile") or {}).get("nickname")}
        if value is None: sys.exit(1)
        print(value if isinstance(value, (str, int)) else json.dumps(value))
    except (ValueError, TypeError, AttributeError):
        sys.exit(1)
elif name == "chmod":
    sys.exit(subprocess.call([os.environ["STUB_REAL_CHMOD"], *args]))
elif name == "flock":
    sys.exit(int(os.environ.get("STUB_FLOCK_RC", "0")))
'''


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ncm shell tests ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        self.foreign = self.base / "foreign cwd"
        self.foreign.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.scratch = self.base / "private temporary files"
        self.scratch.mkdir()
        self.events_file = self.base / "events.jsonl"
        for name in ("run.sh", "run_feedback.sh", "login.sh", "config.env.example"):
            shutil.copy2(ROOT / name, self.project / name)
        for name in ("python3", "curl", "jq", "flock", "sleep", "qrencode", "chmod"):
            stub = self.bin / name
            stub.write_text(f"#!{sys.executable}\n" + textwrap.dedent(STUB))
            stub.chmod(0o755)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.base),
            "TMPDIR": str(self.scratch),
            "STUB_EVENTS": str(self.events_file),
            "STUB_POLL_COUNT": str(self.base / "poll-count"),
            "STUB_REAL_CHMOD": shutil.which("chmod"),
        }
        self.config = (
            'COOKIE_FILE="./cookie folder/cookie.txt"\n'
            'DATA_DIR="./data folder"\nLOG_DIR="./log folder"\n'
            'AM_CONFIG="./secrets folder/apple.yaml"\n'
            'FEEDBACK_STATE_FILE="./state folder/state.json"\n'
            'NCM_API_START_CMD="fake-service-start"\n'
            'PLAYLIST_DESCRIPTION="description with spaces"\n'
        )
        self.write_config()

    def write_config(self, extra=""):
        (self.project / "config.env").write_text(self.config + extra)

    def run_script(self, name, *args, stdin=None):
        return subprocess.run(
            [str(self.project / name), *args], cwd=self.foreign, env=self.env,
            input=stdin, text=True, capture_output=True, timeout=15,
        )

    def events(self, tool=None):
        rows = [json.loads(line) for line in self.events_file.read_text().splitlines()]
        return [row for row in rows if tool is None or row["tool"] == tool]

    def cookie_file(self):
        return self.project / "cookie folder/cookie.txt"

    def seed_key(self):
        path = self.project / "data folder/login-key.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text("test-key\n")
        return path

    def assert_private_cleanup(self):
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse((self.project / "data folder/login-headers.txt").exists())

    def test_wrappers_preserve_exit_code_paths_and_startup_export(self):
        self.env["STUB_PYTHON_RC"] = "23"
        for script in ("run.sh", "run_feedback.sh"):
            with self.subTest(script=script):
                result = self.run_script(script, "--limit", "3")
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertIn("finished rc=23", (self.project / "log folder/cron.log").read_text())
        calls = self.events("python3")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["args"][1:], ["--limit", "3"])
        for call in calls:
            self.assertEqual(Path(call["cwd"]).resolve(), self.project.resolve())
            for variable, relative in (("COOKIE_FILE", "cookie folder/cookie.txt"), ("DATA_DIR", "data folder"), ("LOG_DIR", "log folder"), ("AM_CONFIG", "secrets folder/apple.yaml")):
                self.assertEqual(call["env"][variable], str(self.project / relative))
            self.assertEqual(call["env"]["NCM_API_START_CMD"], "fake-service-start")
            self.assertIn("/usr/local/bin", call["env"]["PATH"])
            self.assertIn("/opt/homebrew/bin", call["env"]["PATH"])
        self.assertEqual(calls[1]["env"]["FEEDBACK_STATE_FILE"], str(self.project / "state folder/state.json"))
        self.assertFalse(self.events("curl"))

    def test_wrappers_success_and_busy_lock(self):
        for script in ("run.sh", "run_feedback.sh"):
            self.assertEqual(self.run_script(script).returncode, 0)
        self.assertIn("finished rc=0", (self.project / "log folder/cron.log").read_text())
        self.env["STUB_FLOCK_RC"] = "1"
        self.assertEqual(self.run_script("run.sh").returncode, 0)
        self.assertEqual(len(self.events("python3")), 2)

    def test_missing_flock_warns(self):
        self.write_config('command() { if [[ "$1" == -v && "$2" == flock ]]; then return 1; fi; builtin command "$@"; }\n')
        for script in ("run.sh", "run_feedback.sh"):
            result = self.run_script(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("flock unavailable", result.stderr)
        self.assertIn("flock unavailable", (self.project / "log folder/cron.log").read_text())

    def test_example_config_is_sourceable(self):
        shutil.copy2(self.project / "config.env.example", self.project / "config.env")
        result = self.run_script("run.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("python3")[0]["env"]["PLAYLIST_DESCRIPTION"], "Auto-synced from NetEase Cloud Music daily recommend")

    def test_cookie_stdin_uses_private_post_body_and_shared_startup(self):
        cookie_path = self.cookie_file()
        cookie_path.parent.mkdir()
        cookie_path.write_text("old-cookie")
        cookie_path.chmod(0o644)
        result = self.run_script("login.sh", "cookie", "-", stdin=FAKE_COOKIE + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(cookie_path.read_text(), FAKE_COOKIE + "\n")
        self.assertEqual(stat.S_IMODE(cookie_path.stat().st_mode), 0o600)
        tightening = [e for e in self.events("chmod") if e["args"][-1] == str(cookie_path)]
        self.assertEqual(tightening[0]["previous"], "old-cookie")
        call = self.events("curl")[0]
        self.assertEqual(call["body"], FAKE_COOKIE)
        self.assertEqual(call["body_mode"], 0o600)
        self.assertIn("POST", call["args"])
        self.assertTrue(any("/login/status?timestamp=" in a for a in call["args"]))
        for event in self.events():
            self.assertNotIn(FAKE_COOKIE, json.dumps(event["args"]))
        self.assertNotIn(FAKE_COOKIE, result.stdout + result.stderr)
        startup = self.events("python3")[0]
        self.assertEqual(Path(startup["cwd"]).resolve(), self.project.resolve())
        self.assertIn("ensure_ncm_api()", startup["args"][1])
        self.assert_private_cleanup()

    def test_cookie_preserves_raw_semicolons_and_attributes(self):
        raw_cookie = FAKE_COOKIE + ";; Path=/;"
        result = self.run_script("login.sh", "cookie", "-", stdin=raw_cookie + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cookie_file().read_text(), raw_cookie + "\n")
        self.assertEqual(self.events("curl")[0]["body"], raw_cookie)
        self.assertNotIn(raw_cookie, result.stdout + result.stderr)
        self.assert_private_cleanup()

    def test_status_requires_usable_profile_id_with_real_jq(self):
        real_jq = shutil.which("jq")
        if real_jq is None:
            self.skipTest("jq is required to verify actual login response parsing")
        (self.bin / "jq").unlink()
        (self.bin / "jq").symlink_to(real_jq)
        self.cookie_file().parent.mkdir()
        self.cookie_file().write_text(FAKE_COOKIE + "\n")
        cases = (
            ({"code": 200, "profile": {}}, False),
            ({"code": 200, "account": {}}, False),
            ({"code": 200, "profile": {"nickname": "tester"}}, False),
            ({"code": 200, "profile": {"userId": 0}}, False),
            ({"code": 200, "profile": {"userId": -1}}, False),
            ({"code": 200, "profile": {"userId": "invalid"}}, False),
            ({"code": 301, "profile": {"userId": 42}}, False),
            ({"code": 200, "profile": {"userId": 42}}, True),
            ({"code": 200, "profile": {"userId": "42"}}, True),
        )
        for data, expected in cases:
            with self.subTest(data=data):
                self.env["STUB_STATUS"] = json.dumps({"data": data, "cookie": FAKE_COOKIE})
                result = self.run_script("login.sh", "status")
                self.assertEqual(result.returncode == 0, expected, result.stdout + result.stderr)
                self.assertNotIn(FAKE_COOKIE, result.stdout + result.stderr)
                self.assert_private_cleanup()

    def test_legacy_cookie_input_still_works_and_creates_parent(self):
        result = self.run_script("login.sh", "cookie", FAKE_COOKIE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cookie_file().read_text(), FAKE_COOKIE + "\n")
        self.assertNotIn(FAKE_COOKIE, json.dumps(self.events("curl")[0]["args"]))
        self.assert_private_cleanup()

    def test_missing_jq_stops_before_startup(self):
        self.write_config('command() { if [[ "$1" == -v && "$2" == jq ]]; then return 1; fi; builtin command "$@"; }\n')
        result = self.run_script("login.sh", "qr-init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("jq", result.stderr)
        self.assertFalse(self.events_file.exists())

    def test_startup_failure_stops_login_requests(self):
        self.env["STUB_PYTHON_RC"] = "17"
        result = self.run_script("login.sh", "qr-init")
        self.assertEqual(result.returncode, 17)
        self.assertFalse(self.events("curl"))
        self.assert_private_cleanup()

    def test_qr_init_private_key_and_readable_image(self):
        key = self.seed_key()
        key.chmod(0o644)
        result = self.run_script("login.sh", "qr-init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((key.parent / "login-qr.png").stat().st_mode), 0o644)
        self.assert_private_cleanup()

    def test_qr_transient_and_invalid_responses_recover(self):
        key = self.seed_key()
        self.env["STUB_POLL"] = json.dumps([7, "not json", {"code": 801}, {"code": 802}, {"code": 803, "cookie": FAKE_COOKIE}])
        result = self.run_script("login.sh", "qr-poll", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LOGIN_OK", result.stdout)
        self.assertIn("waiting_scan", result.stdout)
        self.assertIn("scanned_confirm_on_phone", result.stdout)
        self.assertNotIn(FAKE_COOKIE, result.stdout + result.stderr)
        self.assertNotIn("sensitive response", result.stdout + result.stderr)
        self.assertFalse(key.exists())
        checks = [e for e in self.events("curl") if "header_mode" in e]
        self.assertEqual(len(checks), 5)
        self.assertTrue(all(e["header_mode"] == 0o600 for e in checks))
        self.assert_private_cleanup()

    def test_qr_invalid_login_status_never_emits_success(self):
        self.seed_key()
        self.env["STUB_POLL"] = json.dumps([{"code": 803, "cookie": FAKE_COOKIE}])
        self.env["STUB_STATUS"] = json.dumps({"data": {"code": 301}, "cookie": FAKE_COOKIE})
        result = self.run_script("login.sh", "qr-poll", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("LOGIN_OK", result.stdout)
        self.assertNotIn(FAKE_COOKIE, result.stdout + result.stderr)
        self.assert_private_cleanup()

    def test_qr_headers_fallback_cookie_is_private_and_cleaned(self):
        key = self.seed_key()
        self.env["STUB_POLL"] = json.dumps([{"code": 803}])
        self.env["STUB_HEADERS"] = "HTTP/1.1 200 OK\r\nSet-Cookie: MUSIC_U=fake; Path=/\r\nsEt-CoOkIe: __csrf=example; HttpOnly\r\n"
        result = self.run_script("login.sh", "qr-poll", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cookie_file().read_text(), "MUSIC_U=fake;__csrf=example\n")
        self.assertFalse(key.exists())
        self.assert_private_cleanup()

    def test_qr_failure_is_bounded_and_keeps_key_for_retry(self):
        key = self.seed_key()
        self.env["STUB_POLL"] = json.dumps([7])
        result = self.run_script("login.sh", "qr-poll", "2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("QR_TIMEOUT", result.stderr)
        self.assertTrue(key.exists())
        self.assertEqual(len(self.events("curl")), 2)
        self.assert_private_cleanup()

    def test_expired_qr_cleans_key(self):
        key = self.seed_key()
        self.env["STUB_POLL"] = json.dumps([{"code": 800}])
        result = self.run_script("login.sh", "qr-poll", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("QR_EXPIRED", result.stderr)
        self.assertFalse(key.exists())
        self.assert_private_cleanup()

    def test_invalid_cookie_status_does_not_print_raw_response(self):
        self.env["STUB_STATUS"] = json.dumps({"data": {"code": 301}, "cookie": FAKE_COOKIE})
        result = self.run_script("login.sh", "cookie", "-", stdin=FAKE_COOKIE)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cookie", result.stderr)
        self.assertNotIn(FAKE_COOKIE, result.stdout + result.stderr)
        self.assert_private_cleanup()


if __name__ == "__main__":
    unittest.main()

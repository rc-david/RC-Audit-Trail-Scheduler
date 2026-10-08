"""
Error-handling tests. A tiny local stub imitates RingCentral's responses (including its failures) so every
troubleshooting message can be exercised without credentials or internet.
Run from the repository root:   python -m unittest discover -s tests -v
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import rc_audit_trail as rc  # noqa: E402


def make_jwt(exp_offset=3600, iss="http://127.0.0.1"):
    """A correctly *shaped* JWT (not signed; the stub does not verify it)."""
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'none'})}.{b({'exp': int(time.time()) + exp_offset, 'iss': iss})}.sig"


class Stub:
    """Scripted RingCentral stand-in. `script` maps a path to a list of (status, body, headers) replies,
    consumed in order; the last reply repeats."""

    def __init__(self, script):
        self.script, self.calls = script, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                path = self.path.split("?")[0]
                outer.calls.append(path)
                replies = outer.script[path]
                status, body, headers = replies[min(outer.calls.count(path) - 1, len(replies) - 1)]
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


TOKEN_OK = (200, {"access_token": "tok"}, None)
TOKEN = "/restapi/oauth/token"
SEARCH = "/restapi/v1.0/account/~/audit-trail/search"


def ev(i):
    return {"id": f"e{i}", "eventTime": "2026-01-01T00:00:00.000Z", "actionId": "DELETE_USER:USER"}


class Base(unittest.TestCase):
    def start(self, script):
        self.stub = Stub(script)
        self.addCleanup(self.stub.close)
        env = {"RC_SERVER_URL": self.stub.url, "RC_CLIENT_ID": "client-id-1234", "RC_CLIENT_SECRET": "client-secret-1234",
               "RC_JWT": make_jwt(), "RC_PER_PAGE": "2"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(rc.time, "sleep", lambda *_: None)   # never really wait during retries
        s.start()
        self.addCleanup(s.stop)

    def error_from(self, fn, *a):
        with self.assertRaises(rc.CollectorError) as cm:
            fn(*a)
        return cm.exception


class TokenErrors(Base):
    def test_unparseable_jwt_explains_how_to_paste_it(self):
        self.start({TOKEN: [(400, {"error": "invalid_request", "error_description": "Unparseable assertion",
                                   "errors": [{"errorCode": "OAU-331", "message": "Unparseable assertion"}]}, None)]})
        e = self.error_from(rc.get_token)
        self.assertIn("could not read the JWT", e.what)
        self.assertIn("OAU-331", e.detail)
        self.assertTrue(any("ONLY the long token" in f for f in e.fix))

    def test_invalid_grant_lists_expired_revoked_and_app_restrictions(self):
        self.start({TOKEN: [(400, {"error": "invalid_grant", "error_description": "JWT is invalid"}, None)]})
        e = self.error_from(rc.get_token)
        self.assertIn("rejected the JWT", e.what)
        self.assertTrue(any("expired" in f for f in e.fix))

    def test_audience_claim_error_seen_with_a_non_genuine_jwt(self):
        self.start({TOKEN: [(400, {"error": "invalid_request", "errors": [{"errorCode": "OAU-336",
                    "message": "The [Audience] claim value must contain a token endpoint URL"}]}, None)]})
        e = self.error_from(rc.get_token)
        self.assertIn("not issued for this server", e.what)
        self.assertIn("OAU-336", e.detail)

    def test_bad_client_credentials(self):
        # shape of the real response: HTTP 401, error=invalid_client, errorCode OAU-153
        self.start({TOKEN: [(401, {"error": "invalid_client", "errors": [{"errorCode": "OAU-153",
                    "message": "Invalid client application"}]}, None)]})
        self.assertIn("Client ID and Client Secret", self.error_from(rc.get_token).what)

    def test_login_without_token_in_reply(self):
        self.start({TOKEN: [(200, {"foo": 1}, None)]})
        self.assertIn("no access token", self.error_from(rc.get_token).what)

    def test_secrets_never_appear_in_messages(self):
        self.start({TOKEN: [(401, {"error": "invalid_client", "error_description": "client-secret-1234 is wrong"}, None)]})
        self.assertNotIn("client-secret-1234", rc.redact(self.error_from(rc.get_token).report()))


class SearchErrors(Base):
    def test_403_names_the_permission_and_the_user_role(self):
        self.start({TOKEN: [TOKEN_OK], SEARCH: [(403, {"errorCode": "CMN-401", "message": "Insufficient permissions"}, None)]})
        e = self.error_from(rc.fetch, 7)
        self.assertIn("not allowed to read the Audit Trail", e.what)
        text = " ".join(e.fix)
        self.assertIn("Read Audit Trail", text)
        self.assertIn("role", text)

    def test_404_400_and_500(self):
        for code, needle in [(404, "not found"), (400, "rejected the Audit Trail request"), (500, "server problem")]:
            with self.subTest(code=code):
                self.start({TOKEN: [TOKEN_OK], SEARCH: [(code, {"message": "x"}, None)]})
                self.assertIn(needle, self.error_from(rc.fetch, 7).what)

    def test_500_is_retried_then_succeeds(self):
        self.start({TOKEN: [TOKEN_OK], SEARCH: [(503, {"message": "busy"}, None), (200, {"records": [ev(1)]}, None)]})
        self.assertEqual(len(rc.fetch(7)), 1)
        self.assertEqual(self.stub.calls.count(SEARCH), 2)

    def test_429_waits_then_succeeds(self):
        self.start({TOKEN: [TOKEN_OK], SEARCH: [(429, {"message": "slow down"}, {"Retry-After": "0"}),
                                                (200, {"records": []}, None)]})
        self.assertEqual(rc.fetch(7), [])

    def test_paging_follows_pages_until_short_page(self):
        self.start({TOKEN: [TOKEN_OK], SEARCH: [(200, {"records": [ev(1), ev(2)]}, None), (200, {"records": [ev(3)]}, None)]})
        self.assertEqual([r["id"] for r in rc.fetch(7)], ["e1", "e2", "e3"])

    def test_non_json_reply_points_at_proxy(self):
        self.start({TOKEN: [TOKEN_OK], SEARCH: [(200, b"<html>Please log in to the proxy</html>", None)]})
        self.assertIn("not JSON", self.error_from(rc.fetch, 7).what)

    def test_missing_records_field_lists_what_arrived(self):
        self.start({TOKEN: [TOKEN_OK], SEARCH: [(200, {"results": [], "total": 0}, None)]})
        e = self.error_from(rc.fetch, 7)
        self.assertIn("expected format", e.what)
        self.assertIn("results", e.detail)

    def test_window_out_of_range(self):
        self.start({TOKEN: [TOKEN_OK]})
        self.assertIn("not allowed", self.error_from(rc.fetch, 500).what)


class NetworkErrors(Base):
    def test_connection_refused_is_explained(self):
        self.start({TOKEN: [TOKEN_OK]})
        self.stub.close()                           # nothing is listening any more
        e = self.error_from(rc.get_token)
        self.assertIn("Could not connect", e.what)
        self.assertTrue(any("HTTPS_PROXY" in f for f in e.fix))

    def test_dns_failure_is_explained(self):
        with mock.patch.dict(os.environ, {"RC_SERVER_URL": "https://no-such-host.invalid", "RC_CLIENT_ID": "client-id-1234",
                                          "RC_CLIENT_SECRET": "client-secret-1234", "RC_JWT": make_jwt(iss="")}), \
                mock.patch.object(rc.time, "sleep", lambda *_: None):
            e = self.error_from(rc.get_token)
        self.assertIn("could not be found (DNS)", e.what)


class SettingsChecks(unittest.TestCase):
    def run_check(self, **env):
        base = {k: "" for k in rc.REQUIRED}
        with mock.patch.dict(os.environ, {**base, **env}):
            return rc.check_settings()

    def test_pasted_json_in_jwt_is_caught_before_any_network_call(self):
        problems, _ = self.run_check(RC_SERVER_URL="https://platform.ringcentral.com", RC_CLIENT_ID="abcdefghij1",
                                     RC_CLIENT_SECRET="abcdefghij2", RC_JWT="{")
        self.assertTrue(any("ONLY the long token" in p for p in problems))

    def test_expired_jwt_says_when(self):
        problems, _ = self.run_check(RC_SERVER_URL="https://platform.ringcentral.com", RC_CLIENT_ID="abcdefghij1",
                                     RC_CLIENT_SECRET="abcdefghij2", RC_JWT=make_jwt(exp_offset=-86400 * 3))
        self.assertTrue(any("expired on" in p for p in problems))

    def test_empty_values_are_each_named(self):
        with mock.patch.object(rc, "HERE", Path(tempfile.mkdtemp())):    # a folder with no .env
            problems, _ = self.run_check()
        self.assertTrue(any(".env file" in p for p in problems))

    def test_sandbox_url_warns(self):
        _, warnings = self.run_check(RC_SERVER_URL="https://platform.devtest.ringcentral.com", RC_CLIENT_ID="abcdefghij1",
                                     RC_CLIENT_SECRET="abcdefghij2", RC_JWT=make_jwt(iss=""))
        self.assertTrue(any("SANDBOX" in w for w in warnings))

    def test_http_server_url_rejected(self):
        problems, _ = self.run_check(RC_SERVER_URL="http://platform.ringcentral.com", RC_CLIENT_ID="abcdefghij1",
                                     RC_CLIENT_SECRET="abcdefghij2", RC_JWT=make_jwt(iss=""))
        self.assertTrue(any("https://" in p for p in problems))

    def test_jwt_environment_mismatch_is_flagged(self):
        _, warnings = self.run_check(RC_SERVER_URL="https://platform.ringcentral.com", RC_CLIENT_ID="abcdefghij1",
                                     RC_CLIENT_SECRET="abcdefghij2", RC_JWT=make_jwt(iss="https://platform.devtest.ringcentral.com"))
        self.assertTrue(any("do not work against each other" in w for w in warnings))


class FilesAndInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_corrupt_master_is_set_aside_not_lost_and_run_continues(self):
        (self.tmp / "audit_master.json").write_text('{"a":', encoding="utf-8")
        allrows, new = rc.save_json(self.tmp, {"x": {"id": "x"}})
        self.assertEqual((len(allrows), new), (1, 1))
        self.assertEqual(len(list(self.tmp.glob("audit_master.corrupt-*.json"))), 1)

    def test_from_json_problems_are_explained(self):
        bad = self.tmp / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        wrong = self.tmp / "wrong.json"
        wrong.write_text('{"foo": 1}', encoding="utf-8")
        for path, needle in [(self.tmp / "missing.json", "was not found"), (bad, "not valid JSON"), (wrong, "list of audit events")]:
            with self.subTest(path=path.name), self.assertRaises(rc.CollectorError) as cm:
                rc.load_from_json(path)
            self.assertIn(needle, cm.exception.what)

    def test_unwritable_output_folder_is_explained(self):
        blocker = self.tmp / "afile"
        blocker.write_text("x")
        with self.assertRaises(rc.CollectorError) as cm:
            rc.ensure_dir(blocker / "sub")           # a folder cannot be created under a file
        self.assertIn("cannot be used", cm.exception.what)

    def test_permission_error_mentions_excel(self):
        e = rc.io_error(PermissionError("denied"), self.tmp / "x.csv", "the CSV file")
        self.assertTrue(any("Excel" in f for f in e.fix))

    def test_corrupt_schedule_json_warns_once_and_uses_defaults(self):
        (self.tmp / "schedule.json").write_text("{bad", encoding="utf-8")
        rc._warned_schedule = False
        with mock.patch.object(rc, "SCHEDULE_FILE", self.tmp / "schedule.json"), mock.patch.object(rc, "say") as say:
            cfg = rc.load_schedule()
            rc.load_schedule()
        self.assertEqual(cfg, rc.DEFAULT_SCHEDULE)
        self.assertEqual(say.call_count, 1)

    def test_redact_hides_secrets(self):
        with mock.patch.dict(os.environ, {"RC_CLIENT_SECRET": "supersecretvalue"}):
            self.assertEqual(rc.redact("x supersecretvalue y"), "x *** y")


class CommandLine(unittest.TestCase):
    """Run the real script as a subprocess, the way a user or Task Scheduler would."""

    def run_cli(self, *args, **env):
        out = tempfile.mkdtemp()
        e = {k: v for k, v in os.environ.items() if not k.startswith("RC_")}
        e.update(env)
        e["PYTHONDONTWRITEBYTECODE"] = "1"
        r = subprocess.run([sys.executable, str(ROOT / "rc_audit_trail.py"), "--out", out, *args],
                           capture_output=True, text=True, env=e, timeout=120)
        return r, Path(out)

    def test_bad_setup_prints_a_readable_error_and_records_the_failure(self):
        r, out = self.run_cli(RC_SERVER_URL="https://platform.ringcentral.com", RC_CLIENT_ID="abcdefghij1",
                              RC_CLIENT_SECRET="abcdefghij2", RC_JWT="{")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR: Setup needs attention", r.stderr)
        self.assertIn("What to do:", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        last = json.loads((out / "last_run.json").read_text(encoding="utf-8"))
        self.assertFalse(last["ok"])
        self.assertIn("run_log.txt", [p.name for p in out.iterdir()])

    def test_check_reports_fail_lines_and_nonzero_exit(self):
        r, _ = self.run_cli("--check", RC_SERVER_URL="https://platform.ringcentral.com", RC_CLIENT_ID="abcdefghij1",
                            RC_CLIENT_SECRET="abcdefghij2", RC_JWT="{")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[FAIL]", r.stdout)
        self.assertIn("problem(s) found", r.stdout)

    def test_check_against_stub_passes_end_to_end(self):
        stub = Stub({TOKEN: [TOKEN_OK], SEARCH: [(200, {"records": []}, None)]})
        self.addCleanup(stub.close)
        r, _ = self.run_cli("--check", RC_SERVER_URL=stub.url, RC_CLIENT_ID="abcdefghij1", RC_CLIENT_SECRET="abcdefghij2",
                            RC_JWT=make_jwt())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Audit Trail access works", r.stdout)
        self.assertIn("All checks passed", r.stdout)

    def test_successful_collection_end_to_end_against_stub(self):
        stub = Stub({TOKEN: [TOKEN_OK], SEARCH: [(200, {"records": [ev(1), ev(2)]}, None), (200, {"records": []}, None)]})
        self.addCleanup(stub.close)
        r, out = self.run_cli("--save", "json", RC_SERVER_URL=stub.url, RC_CLIENT_ID="abcdefghij1",
                              RC_CLIENT_SECRET="abcdefghij2", RC_JWT=make_jwt(), RC_PER_PAGE="2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(json.loads((out / "last_run.json").read_text())["ok"])
        self.assertTrue(list(out.glob("audit_viewer_*.html")))

    def test_every_chosen_store_failing_exits_2_but_still_writes_outputs(self):
        r, out = self.run_cli("--from-json", str(ROOT / "docs" / "sample_events_FICTIONAL.json"), "--save", "postgres")
        self.assertEqual(r.returncode, 2)
        self.assertIn("RC_PG_DSN is not set", r.stderr)
        self.assertTrue(list(out.glob("audit_viewer_*.html")) and list(out.glob("audit_trail_*.csv")))


if __name__ == "__main__":
    unittest.main()

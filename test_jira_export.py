"""Checks for jira_export.py and jira_attachments_upload.py without a Jira site or an Orbit server."""
import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

import jira_attachments_upload
import jira_export


class FakeJira(jira_export.Jira):
    """Answers the calls jira_export.main makes; records what it was asked.
    Only the network calls are replaced; paging is the real code."""

    def __init__(self, site, email, token):
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "/rest/api/3/field":
            return [{"id": "customfield_1", "name": "Team"}]
        if path == "/rest/api/3/search/jql":
            return {"isLast": True, "issues": [
                {"id": "2", "key": "ACME-2", "fields": {"issuetype": {"subtask": True}, "comment": {"total": 0, "comments": []},
                                                        "attachment": [{"id": "701", "filename": "../notes.txt", "size": 5}]}},
                {"id": "1", "key": "ACME-1", "fields": {"issuetype": {"hierarchyLevel": 1}, "comment": {"total": 2, "comments": [{"id": "c1"}]}}},
            ]}
        if path.startswith("/rest/api/3/issue/1/comment"):
            return {"comments": [{"id": "c1"}, {"id": "c2"}], "total": 2}
        raise AssertionError(f"unexpected Jira call {method} {path}")

    def download(self, path, target, limit):
        self.calls.append(("DOWNLOAD", path))
        with open(target, "wb") as handle:
            handle.write(b"hello")
        return ""


class JiraExportTests(unittest.TestCase):
    def test_main_writes_the_export_parents_first_and_downloads_attachments(self):
        with tempfile.TemporaryDirectory() as folder:
            files = os.path.join(folder, "files")
            argv = ["jira_export.py", "--site", "https://acme.atlassian.net", "--email", "a@example.com", "--project", "ACME",
                    "--out", os.path.join(folder, "jira-ACME"), "--attachments", files]
            with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"JIRA_API_TOKEN": "not-a-token"}), \
                    mock.patch.object(jira_export, "Jira", FakeJira), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(jira_export.main(), 0)
            with open(os.path.join(folder, "jira-ACME.json"), encoding="utf-8") as handle:
                document = json.load(handle)
            self.assertEqual([issue["key"] for issue in document["issues"]], ["ACME-1", "ACME-2"])
            self.assertEqual(len(document["issues"][0]["fields"]["comment"]["comments"]), 2, "the comments a search page cut short are filled in")
            self.assertEqual(document["names"], {"customfield_1": "Team"})
            self.assertEqual(os.listdir(os.path.join(files, "701")), ["notes.txt"])

    def test_safe_name_is_safe_on_every_system(self):
        for given, want in [("../.secret.txt", "file"), ("C:evil.txt", "C_evil.txt"), ("a.txt:ads", "a.txt_ads"), ("CON.txt", "_CON.txt"),
                            ("dir\\name.png", "name.png"), ("tab\there.txt", "tabhere.txt"), ("trailing. ", "trailing"), ("", "file")]:
            self.assertEqual(jira_export.safe_name(given), want, given)


class Orbit(http.server.BaseHTTPRequestHandler):
    answers = []
    seen = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Orbit.seen.append(self.path)
        status, body = Orbit.answers.pop(0)
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class JiraAttachmentsUploadTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Orbit)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.folder = tempfile.TemporaryDirectory()
        for attachment_id in ("701", "702", "703"):
            os.makedirs(os.path.join(self.folder.name, attachment_id))
            with open(os.path.join(self.folder.name, attachment_id, f"{attachment_id}.txt"), "wb") as handle:
                handle.write(b"hello")
        with open(os.path.join(self.folder.name, "701", ".partial"), "wb") as handle:
            handle.write(b"half")
        Orbit.seen = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def run_upload(self):
        argv = ["jira_attachments_upload.py", "--api", f"http://127.0.0.1:{self.server.server_port}", "--import", "imp-1", "--dir", self.folder.name]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"ORBIT_API_TOKEN": "not-a-token"}), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            return jira_attachments_upload.main(), out.getvalue()

    def test_one_refused_file_does_not_stop_the_others(self):
        Orbit.answers = [(201, {"created": True, "issueKey": "ACME-1"}), (415, {"code": "attachment_type_refused"}), (200, {"created": False, "issueKey": "ACME-3"})]
        code, out = self.run_upload()
        self.assertEqual(code, 1)
        self.assertEqual(Orbit.seen, ["/api/v1/imports/imp-1/attachments/701", "/api/v1/imports/imp-1/attachments/702", "/api/v1/imports/imp-1/attachments/703"])
        self.assertIn("1 copied, 1 already in Orbit, 1 refused or skipped", out)

    def test_an_answer_about_the_sender_stops_the_run(self):
        Orbit.answers = [(403, {"code": "project_administrators_import_from_jira"})]
        with self.assertRaises(SystemExit):
            self.run_upload()
        self.assertEqual(len(Orbit.seen), 1)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Export a Jira Cloud project for the Orbit Jira import.

Reads work items through the Jira Cloud REST API v3 with every field, the
field names, the full changelog and every comment and work log, and writes
one or more JSON files in the orbit-jira-export/1 format. Upload each file
in the Jira import of your Orbit project in the order written: epics come
first, then standard work, then subtasks, so parents exist before their
children. See https://orbit.inortix.com/docs/orbit/import-from-jira/.

Usage:
  JIRA_API_TOKEN=... python3 jira_export.py --site https://example.atlassian.net \\
      --email you@example.com --project ACME [--jql '...'] [--out jira-ACME] \\
      [--attachments jira-ACME-files]

The API token comes from https://id.atlassian.com/manage-profile/security/api-tokens.
Nothing is sent anywhere except the Jira site you name. Attachments are listed
in the export with their Jira links; with --attachments DIR their files are
also downloaded, each to DIR/<attachment id>/<file name> (files over 10 MiB,
which Orbit does not take, are skipped and named). After the import has run,
jira_attachments_upload.py sends them to Orbit. Downloading again
skips the files already there.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FORMAT = "orbit-jira-export/1"
MAX_ISSUES = 2000
MAX_BYTES = 9 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Jira credentials to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Jira:
    def __init__(self, site: str, email: str, token: str):
        self.site = site.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        opener = urllib.request.build_opener(NoRedirect)
        for attempt in range(6):
            request = urllib.request.Request(self.site + path, data=data, method=method, headers={
                "Authorization": self.auth, "Accept": "application/json", "Content-Type": "application/json"})
            try:
                with opener.open(request, timeout=60) as response:
                    return json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as error:
                if error.code in (429, 502, 503, 504) and attempt < 5:
                    wait = float(error.headers.get("Retry-After") or 2 ** attempt)
                    time.sleep(min(wait, 60))
                    continue
                detail = error.read().decode(errors="replace")[:500]
                raise SystemExit(f"Jira {method} {path}: {error.code} {detail}")
            except urllib.error.URLError as error:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"Jira {method} {path}: {error.reason}")
        raise SystemExit(f"Jira {method} {path}: gave up")

    def download(self, path: str, target: str, limit: int) -> str:
        """Writes one file to target through a temporary name beside it.

        Answers "" when the file is there, else why it was skipped; only a
        refused sign-in (401) stops the export."""
        opener = urllib.request.build_opener(NoRedirect)
        partial = os.path.join(os.path.dirname(target), ".partial")
        for attempt in range(6):
            request = urllib.request.Request(self.site + path, headers={"Authorization": self.auth, "Accept": "*/*"})
            try:
                with opener.open(request, timeout=120) as response:
                    size = 0
                    with open(partial, "wb") as handle:
                        while chunk := response.read(1 << 16):
                            size += len(chunk)
                            if size > limit:
                                break
                            handle.write(chunk)
                    if size > limit:
                        os.remove(partial)
                        return f"larger than {limit} bytes"
                    os.replace(partial, target)
                    return ""
            except urllib.error.HTTPError as error:
                if error.code == 401:
                    raise SystemExit(f"Jira GET {path}: 401; check the email and the API token")
                if error.code in (429, 502, 503, 504) and attempt < 5:
                    time.sleep(retry_after(error, attempt))
                    continue
                return f"Jira answered {error.code}" + (" (redirects are not followed)" if 300 <= error.code < 400 else "")
            except (urllib.error.URLError, OSError) as error:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                return f"no answer: {getattr(error, 'reason', error)}"
        return "gave up"

    def paged(self, path: str, key: str) -> list:
        """Every entry of an offset-paged list such as comments or work logs."""
        out, start = [], 0
        while True:
            sep = "&" if "?" in path else "?"
            page = self.call("GET", f"{path}{sep}startAt={start}&maxResults=100")
            values = page.get(key) or []
            out.extend(values)
            start += len(values)
            if not values or start >= int(page.get("total", start)):
                return out


def retry_after(error: urllib.error.HTTPError, attempt: int) -> float:
    try:
        return min(float(error.headers.get("Retry-After") or 2 ** attempt), 60)
    except ValueError:
        return min(2 ** attempt, 60)


def level(issue: dict) -> int:
    issuetype = (issue.get("fields") or {}).get("issuetype") or {}
    if isinstance(issuetype.get("hierarchyLevel"), int):
        return issuetype["hierarchyLevel"]
    return -1 if issuetype.get("subtask") else 0


def complete(jira: Jira, issue: dict) -> dict:
    """Fill in the comments, work logs and history a search page cuts short."""
    fields = issue.setdefault("fields", {})
    key = urllib.parse.quote(issue["id"])
    comment = fields.get("comment") or {}
    if int(comment.get("total", 0)) > len(comment.get("comments") or []):
        comments = jira.paged(f"/rest/api/3/issue/{key}/comment", "comments")
        fields["comment"] = {"comments": comments, "total": len(comments)}
    worklog = fields.get("worklog") or {}
    if int(worklog.get("total", 0)) > len(worklog.get("worklogs") or []):
        worklogs = jira.paged(f"/rest/api/3/issue/{key}/worklog", "worklogs")
        fields["worklog"] = {"worklogs": worklogs, "total": len(worklogs)}
    changelog = issue.get("changelog") or {}
    if int(changelog.get("total", 0)) > len(changelog.get("histories") or []):
        histories = jira.paged(f"/rest/api/3/issue/{key}/changelog", "values")
        issue["changelog"] = {"histories": histories, "total": len(histories)}
    return issue


WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str) -> str:
    """A local file name on any system: the last path part, without control
    characters, drive or stream colons, reserved names or a leading dot."""
    name = os.path.basename(str(name).replace("\\", "/")).strip()
    name = "".join("_" if ch in '<>:"|?*' else ch for ch in name if ch >= " " and ch != "\x7f")
    name = name.rstrip(". ")
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    if not name or name.startswith(".") or len(name.encode()) > 200:
        return "file"
    return name


def download_attachments(jira: Jira, issues: list, folder: str) -> None:
    """Writes every listed attachment to folder/<id>/<name>, skipping what is there."""
    got = skipped = present = 0
    for issue in issues:
        for item in (issue.get("fields") or {}).get("attachment") or []:
            attachment_id = str(item.get("id") or "")
            if not ATTACHMENT_ID.match(attachment_id):
                print(f"{issue.get('key')}: attachment id {attachment_id!r} skipped", file=sys.stderr)
                skipped += 1
                continue
            size = item.get("size")
            if isinstance(size, int) and size > MAX_ATTACHMENT_BYTES:
                print(f"{issue.get('key')}: {item.get('filename')} ({size} bytes) is over 10 MiB; Orbit does not take it", file=sys.stderr)
                skipped += 1
                continue
            directory = os.path.join(folder, attachment_id)
            os.makedirs(directory, mode=0o700, exist_ok=True)
            existing = [name for name in os.listdir(directory) if not name.startswith(".")]
            if existing and (not isinstance(size, int) or os.path.getsize(os.path.join(directory, existing[0])) == size):
                present += 1
                continue
            target = os.path.join(directory, safe_name(item.get("filename") or "file"))
            if os.path.commonpath([os.path.realpath(directory), os.path.realpath(target)]) != os.path.realpath(directory):
                print(f"{issue.get('key')}: {item.get('filename')!r} would leave {directory}; skipped", file=sys.stderr)
                skipped += 1
                continue
            why = jira.download(f"/rest/api/3/attachment/content/{urllib.parse.quote(attachment_id)}?redirect=false", target, MAX_ATTACHMENT_BYTES)
            if why:
                print(f"\n{issue.get('key')}: {item.get('filename')} skipped: {why}", file=sys.stderr)
                skipped += 1
                continue
            got += 1
            print(f"\r{got} files downloaded", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    print(f"{folder}: {got} files downloaded, {present} already there, {skipped} skipped")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", required=True, help="Jira Cloud site, such as https://example.atlassian.net")
    parser.add_argument("--email", required=True, help="the Atlassian account email the API token belongs to")
    parser.add_argument("--project", help="Jira project key; exports the whole project")
    parser.add_argument("--jql", help="JQL instead of a whole project")
    parser.add_argument("--out", help="file name prefix (default jira-<project>)")
    parser.add_argument("--per-file", type=int, default=1000, help="work items per file, at most 2000")
    parser.add_argument("--attachments", metavar="DIR", help="also download the attachment files into DIR")
    args = parser.parse_args()
    if not args.project and not args.jql:
        parser.error("name --project or --jql")
    if not 1 <= args.per_file <= MAX_ISSUES:
        parser.error("--per-file is 1 to 2000")
    site = args.site.rstrip("/")
    if not site.startswith("https://"):
        parser.error("--site must be an https:// address")
    token = os.environ.get("JIRA_API_TOKEN") or getpass.getpass("Jira API token: ")
    jira = Jira(site, args.email, token)
    jql = args.jql or f'project = "{args.project}" ORDER BY key ASC'
    prefix = args.out or f"jira-{args.project or 'export'}"

    names = {field["id"]: field.get("name", field["id"]) for field in jira.call("GET", "/rest/api/3/field") if isinstance(field, dict) and field.get("id")}
    issues, page_token = [], None
    while True:
        body = {"jql": jql, "fields": ["*all"], "expand": "changelog", "maxResults": 100}
        if page_token:
            body["nextPageToken"] = page_token
        page = jira.call("POST", "/rest/api/3/search/jql", body)
        for issue in page.get("issues") or []:
            issues.append(complete(jira, issue))
        print(f"\r{len(issues)} work items", end="", file=sys.stderr, flush=True)
        page_token = page.get("nextPageToken")
        if not page_token or page.get("isLast"):
            break
    print(file=sys.stderr)
    if not issues:
        print("Nothing matched the query.", file=sys.stderr)
        return 1

    # Parents first: higher hierarchy levels, then key order.
    issues.sort(key=lambda issue: (-level(issue), int(issue["id"]) if str(issue["id"]).isdigit() else 0))
    exported = datetime.datetime.now(datetime.timezone.utc).isoformat()
    chunks, chunk = [], []
    size = 0
    for issue in issues:
        encoded = len(json.dumps(issue, ensure_ascii=False).encode())
        if chunk and (len(chunk) >= args.per_file or size + encoded > MAX_BYTES):
            chunks.append(chunk)
            chunk, size = [], 0
        if encoded > MAX_BYTES:
            print(f"{issue.get('key')} alone is larger than {MAX_BYTES} bytes; its comments or history are too long to import", file=sys.stderr)
            continue
        chunk.append(issue)
        size += encoded
    if chunk:
        chunks.append(chunk)
    for number, part in enumerate(chunks, 1):
        name = f"{prefix}-{number}.json" if len(chunks) > 1 else f"{prefix}.json"
        document = {"format": FORMAT, "source": site, "exportedAt": exported, "jql": jql, "names": names, "issues": part}
        with open(name, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)
        print(f"{name}: {len(part)} work items")
    print("Upload the files in the Jira import of your Orbit project, in this order.")
    if args.attachments:
        download_attachments(jira, issues, args.attachments)
        print("After the import has run, send the files with jira_attachments_upload.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

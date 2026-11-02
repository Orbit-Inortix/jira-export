#!/usr/bin/env python3
"""Send the Jira attachment files of a Jira import to Orbit.

jira_export.py --attachments DIR downloads each listed attachment to
DIR/<attachment id>/<file name>. Once the import has run in Orbit, this
script sends every file to POST /api/v1/imports/<import id>/attachments/<id>,
which attaches it to the work item whose origin lists that id, under its Jira
file name. A file Orbit already has is reported, not copied again, so the
script can be run again after a stop.

Usage:
  ORBIT_API_TOKEN=opt_... python3 jira_attachments_upload.py \\
      --api https://acme.orbit.inortix.com --import <import id> --dir jira-ACME-files

The token is a personal API token with write scope (profile → API tokens; a
write token is never limited to projects) of an administrator of the
import's project. A file Orbit refuses (too large, an executable, a work item
the sender may not change) is reported and the others go on; an answer about
the token or the import stops the run. Only the Orbit address you name is
contacted; it must be https, except on localhost.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BYTES = 10 * 1024 * 1024
# Answers about the import or the sender (not an administrator of the
# import's project), not about one file: stop at once.
STOP_CODES = {"import_not_found", "import_not_jira", "import_not_run", "project_administrators_import_from_jira", "session_required"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the token to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def multipart(name: str, data: bytes) -> tuple[bytes, str]:
    boundary = "orbit-" + uuid.uuid4().hex
    quoted = name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{quoted}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + data + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", required=True, help="your Orbit address, such as https://acme.orbit.inortix.com")
    parser.add_argument("--import", dest="import_id", required=True, help="the id of the Jira import in Orbit")
    parser.add_argument("--dir", required=True, help="the folder jira_export.py --attachments wrote")
    args = parser.parse_args()
    api = args.api.rstrip("/")
    parsed = urllib.parse.urlparse(api)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        parser.error("--api must be an https:// address (http only on localhost)")
    if not os.path.isdir(args.dir):
        parser.error(f"{args.dir} is not a folder")
    token = os.environ.get("ORBIT_API_TOKEN") or getpass.getpass("Orbit API token: ")
    opener = urllib.request.build_opener(NoRedirect)
    base = f"{api}/api/v1/imports/{urllib.parse.quote(args.import_id, safe='')}/attachments/"

    copied = present = refused = 0
    for attachment_id in sorted(os.listdir(args.dir)):
        folder = os.path.join(args.dir, attachment_id)
        if not ATTACHMENT_ID.match(attachment_id) or not os.path.isdir(folder):
            continue
        names = sorted(name for name in os.listdir(folder) if not name.startswith(".") and os.path.isfile(os.path.join(folder, name)))
        if not names:
            print(f"{attachment_id}: no downloaded file in {folder}, skipped", file=sys.stderr)
            refused += 1
            continue
        path = os.path.join(folder, names[0])
        if os.path.getsize(path) > MAX_BYTES:
            print(f"{attachment_id}: {names[0]} is over 10 MiB, skipped", file=sys.stderr)
            refused += 1
            continue
        with open(path, "rb") as handle:
            body, content_type = multipart(names[0], handle.read())
        for attempt in range(6):
            request = urllib.request.Request(base + attachment_id, data=body, method="POST", headers={
                "Authorization": f"Bearer {token}", "Content-Type": content_type, "Accept": "application/json"})
            try:
                with opener.open(request, timeout=120) as response:
                    answer = json.loads(response.read() or b"{}")
                if answer.get("created"):
                    copied += 1
                    print(f"{attachment_id}: copied to {answer.get('issueKey')}")
                else:
                    present += 1
                    print(f"{attachment_id}: {answer.get('issueKey')} has it already")
                break
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")[:300]
                try:
                    code = json.loads(detail).get("code", "")
                except ValueError:
                    code = ""
                if error.code in (429, 502, 503, 504) and attempt < 5:
                    try:
                        wait = float(error.headers.get("Retry-After") or 2 ** attempt)
                    except ValueError:
                        wait = 2 ** attempt
                    time.sleep(min(wait, 60))
                    continue
                if error.code == 401 or 300 <= error.code < 400 or code.startswith("token_") or code in STOP_CODES:
                    raise SystemExit(f"Orbit answered {error.code} {detail}; check the token, the address and the import id")
                refused += 1
                print(f"{attachment_id}: {error.code} {detail}", file=sys.stderr)
                break
            except (urllib.error.URLError, OSError) as error:
                if attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"Orbit: {getattr(error, 'reason', error)}")
    print(f"{copied} copied, {present} already in Orbit, {refused} refused or skipped")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())

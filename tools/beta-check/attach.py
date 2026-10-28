#!/usr/bin/env python3
"""Attach the beta check scripts to a beta's checklist issue.

    python3 tools/beta-check/attach.py ISSUE VERSION

Zips pull-beta-check.sh, beta-check-device.sh and README.md from this folder
into ``beta-check-VERSION.zip`` (the forge doesn't accept ``.sh`` attachments)
and uploads it to issue ISSUE. The forge and repository come from the
``origin`` remote, so nothing about them is written here. The API token comes
from ``FORGEJO_TOKEN`` if set, else from the ``tea`` login for that forge; it
is only ever sent in the request header, never printed or put on a command
line. Prints the download URL.
"""

import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ("pull-beta-check.sh", "beta-check-device.sh", "README.md")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?")


def origin():
    """``(base_url, owner/repo)`` from the origin remote (https or ssh form)."""
    url = subprocess.run(["git", "-C", HERE, "remote", "get-url", "origin"],
                         capture_output=True, text=True, check=True).stdout.strip()
    m = re.match(r"https?://([^/]+)/(.+?)(\.git)?/?$", url) \
        or re.match(r"(?:ssh://)?[^@]+@([^:/]+)(?::\d+)?[:/](.+?)(\.git)?/?$", url)
    if not m:
        sys.exit("can't read the forge and repo from the origin remote")
    return "https://" + m.group(1), m.group(2)


def token_for(base):
    if os.environ.get("FORGEJO_TOKEN"):
        return os.environ["FORGEJO_TOKEN"]
    path = os.path.expanduser("~/.config/tea/config.yml")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        sys.exit("no FORGEJO_TOKEN and no tea config")
    for block in re.split(r"\n\s*-\s+name:", text):
        if base in block:
            m = re.search(r"^\s*token:\s*(\S+)", block, re.M)
            if m:
                return m.group(1).strip("'\"")
    sys.exit("no tea login for %s" % base)


def build_zip(version):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in FILES:
            info = zipfile.ZipInfo("beta-check-%s/%s" % (version, name),
                                   date_time=(2020, 1, 1, 0, 0, 0))
            mode = 0o755 if name.endswith(".sh") else 0o644
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(HERE, name), "rb") as handle:
                zf.writestr(info, handle.read())
    return buf.getvalue()


def main(argv):
    if len(argv) != 3 or not argv[1].isdigit() or not VERSION_RE.fullmatch(argv[2]):
        sys.exit("usage: attach.py ISSUE X.Y.Z[-beta.N]")
    issue, version = argv[1], argv[2]
    base, repo = origin()
    name = "beta-check-%s.zip" % version
    data = build_zip(version)
    boundary = uuid.uuid4().hex
    body = (("--%s\r\nContent-Disposition: form-data; name=\"attachment\"; "
             "filename=\"%s\"\r\nContent-Type: application/zip\r\n\r\n")
            % (boundary, name)).encode() + data + ("\r\n--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        "%s/api/v1/repos/%s/issues/%s/assets" % (base, repo, issue),
        data=body, method="POST")
    req.add_header("Authorization", "token " + token_for(base))
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    with urllib.request.urlopen(req, timeout=60) as resp:
        reply = json.load(resp)
    print(reply.get("browser_download_url", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

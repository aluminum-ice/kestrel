#!/usr/bin/env python3
"""Fail if the repo holds something real (CLAUDE.md, "Nothing real in the repo").

Scans every tracked file, and with --include-untracked every untracked file git
would let you add, for:

  - MAC addresses outside the placeholders (RFC 7042 and the conventional
    AA:BB:CC / 11:22:33 / 00:11:22 / repeated-octet ones the tests use)
  - email addresses outside reserved, local and noreply domains
  - private keys, token-shaped strings and password hashes other than the
    documented default
  - hashcat 22000 lines whose AP or client MAC isn't a placeholder
  - capture, GPS-track, model, key and diagnostics-bundle files, by name

It can't recognise a real SSID, a real coordinate or a screenshot of a real
device; those stay the reviewer's job. Findings print the file, line and kind
and only the start of the matched value, so the log doesn't repeat the leak.

    python3 tools/real-data-check/real_data_check.py [--include-untracked]

Exit status: 0 clean, 1 findings, 2 not run inside a git work tree.
"""

import argparse
import os
import re
import subprocess
import sys

# -- what counts as a placeholder ------------------------------------------------

PLACEHOLDER_MAC_PREFIXES = (
    "00:00:5E:00:53:",   # RFC 7042 documentation range
    "AA:BB:CC:",         # conventional placeholders in the tests and docs
    "11:22:33:",
    "00:11:22:",
)

# Email domains that can't belong to a person: RFC 2606/6761 reserved names,
# mDNS hosts (pi@kestrel.local), and systemd template instances
# (getty@tty1.service), which only look like addresses.
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_SUFFIXES = (".example", ".invalid", ".test", ".localhost", ".local",
                      ".service", ".socket", ".timer", ".target", ".mount",
                      ".path", ".device")
# Local parts that name a service, not a person (the ssh clone user, noreply).
_SERVICE_LOCAL_PARTS = ("git", "noreply")
# Real addresses the project publishes on purpose: the role-based security
# reporting alias in SECURITY.md and the patches alias in CONTRIBUTING.md.
# Never a personal address.
ALLOWED_EMAILS = ("security@3ntropy.io", "patches@3ntropy.io")

# The documented default `pi` / `raspberry` hash in builder/config.yml.
ALLOWED_HASHES = (
    "$6$rpiospublic$IdjDhs9BWgsMS9GaagrukxeyvrJyqKSRV7/tXs8iSGrRgJCK2HMgSWUSsEqiF5hIpkgC2ESP4gL7G8a1AjOqr/",
)

# -- patterns ---------------------------------------------------------------------

_HEX2 = r"[0-9A-Fa-f]{2}"
MAC_RE = re.compile(r"(?<![0-9A-Fa-f:-])(%s([:-])%s(?:\2%s){4})(?![0-9A-Fa-f:-])"
                    % (_HEX2, _HEX2, _HEX2))
# The domain must end in a letters-only TLD, so ssh targets like pi@192.168.1.50
# don't count as addresses.
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@"
                      r"((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![A-Za-z0-9-])")
# hashcat 22000: WPA*01|02*<pmkid or mic>*<ap mac>*<client mac>*<essid hex>*...
HASHLINE_RE = re.compile(r"WPA\*0[12]\*[0-9A-Fa-f]*\*([0-9A-Fa-f]{12})\*([0-9A-Fa-f]{12})\*")
CRYPT_RE = re.compile(r"\$(?:1|5|6|7|y|gy|2[aby])\$[^\s'\"$]+(?:\$[^\s'\"$]+)*"
                      r"\$[./A-Za-z0-9]{20,}")
SECRET_RES = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,})")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("PyPI token", re.compile(r"\bpypi-AgE[A-Za-z0-9_-]{50,}")),
    ("Ghost Admin API key", re.compile(r"\b[0-9a-f]{24}:[0-9a-f]{64}\b")),
)

# Files that are real device data, secrets or bundles by their nature.
FORBIDDEN_SUFFIXES = (
    ".pcap", ".pcapng", ".cap", ".hc22000", ".22000", ".hccapx",   # captures
    ".potfile",                                                    # cracked PSKs
    ".gpx", ".nmea", ".kismet", ".netxml",                         # GPS tracks, survey logs
    ".pt", ".pth",                                                 # model weights
    ".env", ".key", ".pem", ".p12", ".pfx",                        # secrets
)
FORBIDDEN_NAMES = ("capture-seen.json", "capture-status.json",
                   "id_rsa", "id_ecdsa", "id_ed25519", "id_dsa")
FORBIDDEN_NAME_RES = (re.compile(r"^model-.*\.zip$"),
                      re.compile(r"^kestrel-beta-check-\d"),   # beta check reports
                      re.compile(r"^kestrel-health-\d{8}T\d{6}Z\.txt$"),  # pull-health.sh
                      # a scripts/pull-*.sh report or bundle saved as one file
                      re.compile(r"^[a-z0-9-]+-diagnostics-\d{8}T\d{6}Z(\.txt|\.tar\.gz)?$"))
# Bundles the scripts/pull-*.sh tools write into the current directory.
BUNDLE_DIR_RE = re.compile(r"(?:-diagnostics-|^capture-health-|^kestrel-beta-check-)\d")


# -- checks ---------------------------------------------------------------------

def mask(value):
    """Enough of ``value`` to find it, not enough to repeat it."""
    return value[:5] + "..." if len(value) > 5 else value


def normalize_mac(mac):
    mac = mac.upper().replace("-", ":")
    if ":" not in mac:
        mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
    return mac


def placeholder_mac(mac):
    mac = normalize_mac(mac)
    return mac.startswith(PLACEHOLDER_MAC_PREFIXES) or len(set(mac.split(":"))) == 1


def allowed_email(local, domain):
    domain = domain.lower()
    if "%s@%s" % (local.lower(), domain) in ALLOWED_EMAILS:
        return True
    if local.lower() in _SERVICE_LOCAL_PARTS or domain.startswith("noreply."):
        return True
    return (domain in _RESERVED_DOMAINS
            or domain.endswith(_RESERVED_SUFFIXES)
            or any(domain.endswith("." + d) for d in _RESERVED_DOMAINS))


def scan_text(text):
    """``[(line_no, kind, masked_value)]`` for everything real-looking in ``text``."""
    found = []
    for no, line in enumerate(text.splitlines(), 1):
        for match in MAC_RE.finditer(line):
            if not placeholder_mac(match.group(1)):
                found.append((no, "MAC address", mask(match.group(1))))
        for match in HASHLINE_RE.finditer(line):
            for mac in match.groups():
                if not placeholder_mac(mac):
                    found.append((no, "hashcat line with a real MAC", mask(mac)))
        for match in EMAIL_RE.finditer(line):
            if not allowed_email(match.group(1), match.group(2)):
                found.append((no, "email address", mask(match.group(0))))
        for match in CRYPT_RE.finditer(line):
            if match.group(0) not in ALLOWED_HASHES:
                found.append((no, "password hash", mask(match.group(0))))
        for kind, pattern in SECRET_RES:
            for match in pattern.finditer(line):
                found.append((no, kind, mask(match.group(0))))
    return found


def check_name(path):
    """Why ``path`` may not be committed at all, or None."""
    parts = path.replace(os.sep, "/").split("/")
    name = parts[-1]
    if name.lower().endswith(FORBIDDEN_SUFFIXES):
        return "file type that holds real data or secrets"
    if name in FORBIDDEN_NAMES or any(r.match(name) for r in FORBIDDEN_NAME_RES):
        return "file that holds real device data or a key"
    if any(BUNDLE_DIR_RE.search(part) for part in parts[:-1]):
        return "inside a diagnostics bundle"
    return None


def check_file(path):
    """Findings for one file: its name, then its contents if it is text."""
    findings = []
    reason = check_name(path)
    if reason:
        findings.append((path, 0, reason, ""))
    if not os.path.isfile(path) or os.path.islink(path):
        return findings
    with open(path, "rb") as handle:
        data = handle.read()
    if b"\0" in data[:8192]:
        return findings
    for no, kind, value in scan_text(data.decode("utf-8", errors="replace")):
        findings.append((path, no, kind, value))
    return findings


def repo_files(include_untracked=False):
    args = ["git", "ls-files", "-z", "--cached"]
    if include_untracked:
        args += ["--others", "--exclude-standard"]
    out = subprocess.run(args, check=True, capture_output=True).stdout
    return sorted({p for p in out.decode("utf-8", errors="replace").split("\0") if p})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--include-untracked", action="store_true",
                        help="also scan untracked files git would let you add")
    args = parser.parse_args(argv)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                             capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        print("real-data-check: not inside a git work tree", file=sys.stderr)
        return 2
    os.chdir(top)
    findings = []
    for path in repo_files(args.include_untracked):
        findings += check_file(path)
    for path, no, kind, value in findings:
        where = "%s:%d" % (path, no) if no else path
        print("%s: %s%s" % (where, kind, " (%s)" % value if value else ""), file=sys.stderr)
    if findings:
        print("real-data-check: %d finding(s). Use a placeholder instead (CLAUDE.md, "
              "\"Nothing real in the repo\"); if one is a genuine placeholder, add it "
              "to the allowlist in tools/real-data-check/real_data_check.py and say "
              "why in the commit." % len(findings), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

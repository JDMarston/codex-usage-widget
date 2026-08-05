#!/usr/bin/env python3
'''Fail when tracked public files contain likely credentials or private data.

Output intentionally reports only rule, path, and line number. It never prints
matched values. Add narrowly scoped false-positive handling in code review rather
than disabling this guard.
'''
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

SELF = "scripts/public_privacy_guard.py"
SKIP_PARTS = {".git", ".venv", "venv", "node_modules", "dist", "build", "coverage", "__pycache__"}
MAX_BYTES = 2_000_000
SENSITIVE_NAMES = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\..*)?|.*\.(?:pem|key|p12|pfx|jks|keystore|sqlite|sqlite3|db)|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)|credentials(?:\..*)?|secrets?(?:\..*)?)$"
)
RULES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I),
    "credential-literal": re.compile(
        r"(?ix)\b(?:api[_-]?key|secret|client[_-]?secret|password|passwd|pwd|access[_-]?token|"
        r"auth[_-]?token|private[_-]?key)\b\s*[:=]\s*(?:[\"'][^\"'\n]{8,}[\"']|[A-Za-z0-9_+/=-]{16,})"
    ),
    "authorization-value": re.compile(
        r"(?i)\bAuthorization\b\s*[:=]\s*[\"']?(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"
    ),
    "credential-url": re.compile(r"(?i)https?://[^\s/:@]+:[^\s/@]+@"),
    "email": re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])"),
    "private-ip": re.compile(
        r"(?<!\d)(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?!\d)"
    ),
    "windows-user-path": re.compile(r"(?i)(?:[A-Z]:[\\/]|/)[Uu]sers[\\/][^\\/\s\"']+"),
    "ssn-like": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    "phone-like": re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)"),
}
SAFE_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "users.noreply.github.com"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout
    return [item.decode("utf-8", "surrogateescape") for item in out.split(b"\0") if item]


def main() -> int:
    findings: list[tuple[str, str, int]] = []
    for name in tracked_files():
        normalized = name.replace("\\", "/")
        if normalized == SELF or any(part in SKIP_PARTS for part in pathlib.PurePosixPath(normalized).parts):
            continue
        if SENSITIVE_NAMES.search(normalized):
            findings.append(("sensitive-filename", normalized, 0))
        path = pathlib.Path(name)
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > MAX_BYTES or b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", "replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule, pattern in RULES.items():
                for match in pattern.finditer(line):
                    if rule == "email":
                        domain = match.group(0).rsplit("@", 1)[-1].lower()
                        if domain in SAFE_EMAIL_DOMAINS:
                            continue
                    findings.append((rule, normalized, line_no))
    if findings:
        print("Public-data guard blocked this change. Matched values are intentionally hidden.", file=sys.stderr)
        for rule, path, line in sorted(set(findings)):
            where = f"{path}:{line}" if line else path
            print(f"- {rule}: {where}", file=sys.stderr)
        return 1
    print("Public-data guard: no credential literals or private-data patterns found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Reject accidental private state and obvious credentials in the tracked tree.

This lightweight guard complements Gitleaks and manual review; it is not a
claim that every possible secret encoding can be detected. Never print matches.
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DENIED_PARTS = {"credentials", "private", "evidence", "screenshots", "backups", ".venv", "node_modules", "__pycache__"}
DENIED_NAMES = {"host.json", "helper.json", "owner-login.json", "owner-session.json", "setup.token"}
DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".db", ".age", ".har", ".pcap", ".log"}
RULES = {
    "private-key-block": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "provider-live-key": re.compile(rb"\bsk-(?:api|cp)-[A-Za-z0-9_+./=-]{20,}"),
    "github-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "aws-access-key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private-workstation-path": re.compile(rb"(?i)[CDE]:[\\/](?:Users[\\/]|" + "服务器运维|梦bot".encode() + rb")"),
}


def main():
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    paths = [Path(p.decode("utf-8")) for p in result.stdout.split(b"\0") if p]
    if not paths:
        raise SystemExit("No tracked files; stage the intended public tree first")
    problems = []
    for relative in paths:
        path = ROOT / relative
        if (set(relative.parts) & DENIED_PARTS or path.name in DENIED_NAMES or
                path.suffix.lower() in DENIED_SUFFIXES or ".sqlite" in path.name or
                path.name.startswith(".env") and path.name != ".env.example"):
            problems.append((relative.as_posix(), "private-file-type"))
            continue
        if path.is_symlink() or not path.is_file():
            problems.append((relative.as_posix(), "unsupported-file-type"))
            continue
        content = path.read_bytes()
        for name, pattern in RULES.items():
            if pattern.search(content):
                problems.append((relative.as_posix(), name))
        if content.startswith((b"MZ", b"\x7fELF", b"PK\x03\x04")):
            problems.append((relative.as_posix(), "unexpected-binary-or-archive"))
    if problems:
        for path, rule in problems:
            print(f"REJECT {path}: {rule}")
        raise SystemExit(1)
    print(f"PASS: {len(paths)} tracked files; no prohibited file types or credential patterns")


if __name__ == "__main__":
    main()

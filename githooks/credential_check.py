#!/usr/bin/python3
"""Keep login credentials (keys, tokens, passwords) out of this repository.

Git runs this through the pre-commit and commit-msg hooks next to it, once
`git config core.hooksPath githooks` is set -- see README, "Commit hooks".

  credential_check.py --staged          lines added by the staged diff, and staged file names
  credential_check.py --message FILE    a commit message
  credential_check.py PATH...           whole files, e.g. `git ls-files -z | xargs -0 ...`

Exits 1 with a report on stderr when anything matches, 0 otherwise. Matches are
printed masked, so a real secret is not echoed into a terminal or log.

A line containing the allow marker (ALLOW_MARKER below) is skipped; use it only
for a false positive you have looked at. `git commit --no-verify` skips both
hooks entirely.
"""

import math
import os
import re
import subprocess
import sys
from collections import Counter

ALLOW_MARKER = "credential-check: allow"

# Values that start like these are references or placeholders, not secrets:
# ${VAR}, $VAR, <your-token>, {token}, (...).
_NOT_LITERAL = r"(?![${<(])"

PATTERNS = [
    ("private-key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----"),
    ("ssh-public-key",
     r"\b(?:ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp\d+|sk-(?:ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)"
     r" AAAA[0-9A-Za-z+/]{20,}"),
    ("ssh-key-fingerprint", r"\bSHA256:[A-Za-z0-9+/]{43}(?![A-Za-z0-9+/])"),
    ("aws-access-key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ("gitlab-token", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("ai-api-key", r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"),
    ("huggingface-token", r"\bhf_[A-Za-z0-9]{30,}"),
    ("stripe-key", r"\b[rs]k_live_[A-Za-z0-9]{20,}"),
    ("npm-token", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
    ("url-with-password", r"\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:" + _NOT_LITERAL + r"[^/\s@'\"]+@"),
    ("authorization-header",
     r"(?i)\bauthorization\b['\"]?\s*[:=]\s*['\"]?(?:bearer|basic|token)\s+"
     + _NOT_LITERAL + r"[A-Za-z0-9._~+/=-]{12,}"),
    ("bearer-token", r"(?i)\bbearer\s+(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{20,}"),
    # A secret-sounding key given a quoted literal value, in code, YAML or JSON.
    ("quoted-secret",
     r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|secret[_-]?key"
     r"|client[_-]?secret|auth[_-]?token|private[_-]?key)\b['\"]?\s*[:=]\s*['\"]"
     + _NOT_LITERAL + r"[^'\"\s]{6,}['\"]"),
    # An upper-case *_TOKEN / *_PASSWORD style variable given an unquoted value, as
    # in a shell or .env line. The value must contain a digit, which keeps a code
    # constant assigned another constant's name from matching.
    ("env-secret",
     r"\b(?:[A-Z0-9]+_)*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_KEY|PRIVATE_KEY)\b\s*=\s*"
     r"(?![$({<'\"])(?=[A-Za-z0-9+/=_.~-]*\d)[A-Za-z0-9+/=_.~-]{8,}(?![A-Za-z0-9+/=_.~(-])"),
    ("sshpass", r"\bsshpass\s+-p\s*['\"]?" + _NOT_LITERAL + r"[^\s'\"]+"),
    ("password-piped-to-sudo", r"\becho\s+['\"]?[^\s|'\"$]+['\"]?\s*\|\s*sudo\s+-S\b"),
    ("curl-basic-auth", r"\bcurl\b.*\s(?:-u|--user)\s+['\"]?[^\s:'\"]+:" + _NOT_LITERAL + r"[^\s'\"]+"),
]
PATTERNS = [(name, re.compile(rx)) for name, rx in PATTERNS]

# Long random-looking strings. '/' is left out of the token alphabet because
# almost every long run containing it in this repository is a file path.
_TOKEN = re.compile(r"[A-Za-z0-9+=_-]{32,}")
ENTROPY_BITS = 4.3

SENSITIVE_NAME = re.compile(
    r"^(?:id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?|authorized_keys|\.netrc|\.pgpass|\.htpasswd"
    r"|\.env(?:\..+)?|credentials(?:\.json)?|.*secrets?\.(?:json|ya?ml|toml|env)"
    r"|.*\.(?:pem|key|p12|pfx|jks|keystore|kdbx|ppk))$",
    re.IGNORECASE,
)

SCISSORS = "# ------------------------ >8 ------------------------"


def _entropy(s):
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _looks_random(token):
    return (any(c.isdigit() for c in token)
            and any(c.isupper() for c in token)
            and any(c.islower() for c in token)
            and _entropy(token) >= ENTROPY_BITS)


def scan_line(line):
    """Return [(rule, matched_text)] for one line of text."""
    if ALLOW_MARKER in line:
        return []
    hits = [(name, m.group(0)) for name, rx in PATTERNS for m in rx.finditer(line)]
    hits += [("high-entropy-string", t) for t in _TOKEN.findall(line) if _looks_random(t)]
    return hits


def scan_text(where, text):
    """Return [(location, rule, matched_text)] for a block of text."""
    return [(f"{where}:{n}", rule, hit)
            for n, line in enumerate(text.splitlines(), 1)
            for rule, hit in scan_line(line)]


def scan_name(path):
    if SENSITIVE_NAME.match(os.path.basename(path)):
        return [(path, "credential-file-name", os.path.basename(path))]
    return []


def scan_message(text):
    """Scan a commit message as git will store it: no comments, nothing below the scissors."""
    kept = []
    for line in text.splitlines():
        if line == SCISSORS:
            break
        kept.append("" if line.startswith("#") else line)
    return scan_text("commit message", "\n".join(kept))


def _git(args, cwd=None):
    return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd,
                          capture_output=True, check=True).stdout.decode("utf-8", "replace")


def _diff_path(header):
    path = header[4:].strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return path[2:] if path.startswith("b/") else None


def scan_staged(cwd=None):
    """Scan what the commit would add: new and changed lines, and new file names."""
    findings = []
    names = _git(["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"], cwd)
    for path in filter(None, names.split("\0")):
        findings += scan_name(path)

    # --text so binary files are scanned too; --unified=0 so only added lines appear.
    diff = _git(["diff", "--cached", "--no-color", "--no-ext-diff", "--text",
                 "--unified=0", "--diff-filter=ACMR"], cwd)
    path, lineno, in_header = None, 0, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path, in_header = None, True
        elif line.startswith("@@"):
            in_header = False
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif in_header:
            if line.startswith("+++ "):
                path = _diff_path(line)
        elif line.startswith("+") and path is not None:
            findings += [(f"{path}:{lineno}", rule, hit) for rule, hit in scan_line(line[1:])]
            lineno += 1
    return findings


def scan_files(paths):
    findings = []
    for path in paths:
        findings += scan_name(path)
        with open(path, "rb") as f:
            findings += scan_text(path, f.read().decode("utf-8", "replace"))
    return findings


def mask(hit):
    return f"{hit[:6]}... ({len(hit)} chars)" if len(hit) > 10 else hit[:3] + "..."


def report(findings, out=sys.stderr):
    if not findings:
        return 0
    print("credential-check: possible login credentials found -- commit blocked:", file=out)
    for where, rule, hit in findings:
        print(f"  {where}  {rule}  {mask(hit)}", file=out)
    print("Remove them, and rotate any secret that was real. For a false positive you have\n"
          f"checked, add '{ALLOW_MARKER}' to that line, or commit with --no-verify.", file=out)
    return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--staged"]:
        findings = scan_staged()
    elif argv[:1] == ["--message"] and len(argv) == 2:
        with open(argv[1], encoding="utf-8", errors="replace") as f:
            findings = scan_message(f.read())
    elif argv and not argv[0].startswith("-"):
        findings = scan_files(argv)
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return report(findings)


if __name__ == "__main__":
    sys.exit(main())

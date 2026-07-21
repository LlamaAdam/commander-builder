"""Pre-commit secret scanner (FP-011 piece).

Scans STAGED changes for credential-shaped strings and blocks the commit on a
hit, so an API key / token can't be committed by accident. The credential
*file* layer already lives in `_secrets.py` (keys stay in
~/.commander-builder/credentials, outside the repo); this is the belt to that
suspenders -- it catches a key that slips into tracked source/config/notebook.

Install (one of):
  git config core.hooksPath setup/hooks        # uses setup/hooks/pre-commit
  # or copy setup/hooks/pre-commit -> .git/hooks/pre-commit

Run manually:
  python scripts/scan_secrets.py               # scans `git diff --cached`
  python scripts/scan_secrets.py --all         # scans the whole tree (audit)

`scan_diff(text)` is the pure, unit-tested core.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Patterns for credential-shaped strings in ADDED lines. Conservative enough
# to avoid most false positives; each is a real secret prefix/shape.
_PATTERNS = [
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
]

# Lines that are obviously placeholders/examples -- don't flag these.
_ALLOW = re.compile(r"sk-ant-\.\.\.|sk-ant-xxx|<your[- ]key>|EXAMPLE|REDACTED|sk-ant-\$", re.IGNORECASE)

# The scanner and its tests intentionally contain credential-shaped patterns
# (regexes, fixtures). Don't scan them, or the scanner trips on itself.
_SKIP_FILES = {"scripts/scan_secrets.py", "tests/test_scan_secrets.py"}


def scan_diff(diff_text: str) -> list[tuple[str, str, str]]:
    """Scan unified-diff text. Returns (pattern_name, file, snippet) for each
    secret found in an ADDED line. Only added lines (`+`, not `+++`) count."""
    hits: list[tuple[str, str, str]] = []
    current_file = "?"
    skip_current = False
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            skip_current = current_file.replace("\\", "/") in _SKIP_FILES
            continue
        if skip_current:
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        if _ALLOW.search(added):
            continue
        for name, pat in _PATTERNS:
            m = pat.search(added)
            if m:
                snippet = m.group(0)
                redacted = snippet[:8] + "…" + snippet[-2:] if len(snippet) > 12 else snippet
                hits.append((name, current_file, redacted))
    return hits


def _git_staged_diff() -> str:
    return subprocess.run(
        ["git", "diff", "--cached", "--unified=0"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--all" in argv:
        diff = subprocess.run(
            ["git", "diff", "--unified=0", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
    else:
        diff = _git_staged_diff()
    hits = scan_diff(diff)
    if hits:
        print("BLOCKED: possible secret(s) in staged changes:", file=sys.stderr)
        for name, f, snip in hits:
            print(f"  {name}: {f}: {snip}", file=sys.stderr)
        print("Move the secret to ~/.commander-builder/credentials (see _secrets.py) "
              "and unstage it. Override with: git commit --no-verify (NOT recommended).",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

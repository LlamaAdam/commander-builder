"""Pre-commit secret scanner — pure stdlib, no external dependencies.

Invoked as a git ``pre-commit`` hook (see ``scripts/install_git_hooks.py``
or ``.pre-commit-config.yaml``). Receives one or more file paths as
positional arguments and exits non-zero if any file contains a string
that looks like a secret.

Usage
-----

    python scripts/pre_commit_secret_scan.py path1 path2 ...
    python scripts/pre_commit_secret_scan.py --baseline .secrets-baseline path1 ...

Acceptance
----------

- Catches Anthropic / OpenAI / GitHub / AWS API keys, generic Bearer
  tokens (>=32 chars), and private-key armor headers.
- Does NOT false-positive on the project's documentation placeholders
  (``sk-ant-...``, ``sk-ant-api03-...``, ``Bearer YOUR_TOKEN_HERE``).
- Supports a baseline file (one fingerprint per line, format
  ``<path>:<line>:<pattern>``) for known false positives.
- Supports inline opt-out via ``# pragma: secret-scan-allow`` on the
  offending line (for deliberate test fixtures).
- Skips binary files and a built-in skip list (``*.zip``, ``*.lock``,
  image formats, etc.).

Pattern updates land in ``_PATTERNS`` below; tests in
``tests/test_pre_commit_secret_scan.py`` pin the contract.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set


# ---------------------------------------------------------------------------
# Pattern table
# ---------------------------------------------------------------------------

# Each pattern is (label, compiled_regex). The label is used to:
# (1) name the finding in stderr output, and (2) form part of the
# baseline fingerprint so allowlisting is precise per-pattern.
#
# Patterns favor FALSE NEGATIVES over false positives — a secret scanner
# that screams on every base64 blob teaches developers to ignore it.
# Real-world rotation horror stories almost always come from explicit
# `KEY=value` shapes; that's the surface we hit hardest.

_PLACEHOLDER_MARKERS = (
    "...",          # sk-ant-api03-...
    "YOUR_",        # YOUR_TOKEN_HERE
    "REPLACE",      # REPLACE_ME
    "EXAMPLE",      # EXAMPLE_KEY
    "<token>",
    "<your",
)


# Anthropic: sk-ant-api03-<>= 90 base64-ish chars; we require >=60 to
# avoid the docs `sk-ant-...` placeholder while still catching real
# keys that come in around 95 chars.
_ANTHROPIC_RE = re.compile(r"sk-ant-(?:api03-)?[A-Za-z0-9_\-]{60,}")

# OpenAI: sk-<>=40 alphanum (legacy) or sk-proj-<>=  for new project keys.
_OPENAI_RE = re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9]{40,}")

# GitHub PATs: ghp_, gho_, ghs_, ghr_, github_pat_ followed by 36+ chars
_GITHUB_RE = re.compile(
    r"(?:ghp_|gho_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{36,}"
)

# AWS access key: AKIA followed by exactly 16 alphanum chars (the
# canonical format). Bounded so we don't fire on every "AKIA" string.
_AWS_ACCESS_KEY_RE = re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])")

# Bearer tokens: "Bearer <40+ char value>". 40+ avoids placeholders
# like "YOUR_TOKEN_HERE" (14 chars) but catches real JWTs / opaque
# session tokens.
_BEARER_RE = re.compile(r"Bearer\s+([A-Za-z0-9\-_.=+/]{40,})")

# Private-key armor: catches any PEM-format private key header.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |)PRIVATE KEY-----"
)


_PATTERNS = (
    ("anthropic_api_key", _ANTHROPIC_RE),
    ("openai_api_key", _OPENAI_RE),
    ("github_pat", _GITHUB_RE),
    ("aws_access_key", _AWS_ACCESS_KEY_RE),
    ("bearer_token", _BEARER_RE),
    ("private_key", _PRIVATE_KEY_RE),
)


# Built-in skip suffixes (binary blobs, generated lockfiles).
_SKIP_SUFFIXES = frozenset(
    {
        ".zip", ".gz", ".tar", ".whl", ".pyc", ".pyo", ".so", ".dll",
        ".exe", ".jar", ".class", ".png", ".jpg", ".jpeg", ".gif",
        ".webp", ".ico", ".pdf", ".lock", ".min.js", ".min.css",
        ".woff", ".woff2", ".ttf",
    }
)

# Inline opt-out marker (per-line). The wrapper has to also accept it
# without the leading "#" (e.g. for non-Python source).
_INLINE_ALLOW = "pragma: secret-scan-allow"


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    """One match in one file at one line."""

    path: str
    line: int
    pattern: str
    snippet: str

    def render(self) -> str:
        return f"  {self.path}:{self.line} [{self.pattern}] {self.snippet}"


def fingerprint(finding: Finding) -> str:
    """Stable identifier for the baseline file."""

    return f"{finding.path}:{finding.line}:{finding.pattern}"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _has_placeholder_marker(line: str) -> bool:
    """``True`` if the line looks like a documentation placeholder."""

    return any(marker in line for marker in _PLACEHOLDER_MARKERS)


def _is_inline_allowed(line: str) -> bool:
    return _INLINE_ALLOW in line


def _snippet_for(line: str, span: tuple[int, int]) -> str:
    """Return a short snippet that highlights the match for the dev."""

    start, end = span
    # Keep 40 chars before and after the match, total cap 200 chars.
    snippet_start = max(0, start - 40)
    snippet_end = min(len(line), end + 40)
    snippet = line[snippet_start:snippet_end].rstrip("\n")
    if snippet_start > 0:
        snippet = "..." + snippet
    if snippet_end < len(line.rstrip("\n")):
        snippet = snippet + "..."
    if len(snippet) > 200:
        snippet = snippet[:200] + "..."
    return snippet


def scan_text(
    name: str,
    text: str,
    allowlist: Set[str],
) -> List[Finding]:
    """Scan one piece of text for secret patterns.

    Returns one ``Finding`` per pattern hit. Returns an empty list if
    the text is clean or every hit is suppressed by the allowlist or
    an inline opt-out marker.
    """

    findings: List[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _is_inline_allowed(line):
            continue
        if _has_placeholder_marker(line):
            # Skip lines that are clearly documentation placeholders.
            # The placeholder check applies even when the regex would
            # otherwise fire — favors avoiding doc false positives.
            continue
        for label, regex in _PATTERNS:
            for match in regex.finditer(line):
                f = Finding(
                    path=name,
                    line=lineno,
                    pattern=label,
                    snippet=_snippet_for(line, match.span()),
                )
                if fingerprint(f) in allowlist:
                    continue
                findings.append(f)
    return findings


def _should_skip_path(path: Path) -> bool:
    """Skip files matching the built-in skip list."""

    # Treat compound suffixes (".min.js") as a single skip key.
    name = path.name.lower()
    for suffix in _SKIP_SUFFIXES:
        if name.endswith(suffix):
            return True
    return False


def _is_binary(data: bytes) -> bool:
    """Heuristic: a NUL byte in the first 8 KB → binary."""

    return b"\x00" in data[:8192]


def scan_path(path: Path, allowlist: Set[str]) -> List[Finding]:
    """Scan one file by path. Missing / binary / skip-listed files yield []."""

    if not path.exists():
        return []
    if _should_skip_path(path):
        return []
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if _is_binary(data):
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except UnicodeDecodeError:
            return []
    return scan_text(str(path), text, allowlist)


# ---------------------------------------------------------------------------
# Allowlist file
# ---------------------------------------------------------------------------

def load_allowlist(path: Path) -> Set[str]:
    """Read a baseline file. Missing file → empty set.

    Format: one fingerprint per line, ``<path>:<line>:<pattern>``.
    Comment lines starting with ``#`` and blank lines are ignored.
    """

    result: Set[str] = set()
    if not path.exists():
        return result
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            result.add(line)
    except OSError:
        return set()
    return result


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scan files for secrets (Anthropic / OpenAI / GitHub / AWS / "
            "Bearer / PEM private keys). Pure stdlib; no external deps."
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(".secrets-baseline"),
        help=(
            "Path to an allowlist file (one '<path>:<line>:<pattern>' "
            "fingerprint per line; '#' for comments). Defaults to "
            "'.secrets-baseline' in the repo root."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files to scan (typically supplied by the git pre-commit hook).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    allowlist = load_allowlist(args.baseline)

    all_findings: List[Finding] = []
    for p in args.paths:
        all_findings.extend(scan_path(p, allowlist))

    if not all_findings:
        return 0

    sys.stderr.write(
        "Pre-commit secret scan FAILED. "
        "If these are deliberate test fixtures, either:\n"
        "  1. Add '# pragma: secret-scan-allow' to the offending line, or\n"
        "  2. Append the fingerprint below to .secrets-baseline:\n\n"
    )
    for f in all_findings:
        sys.stderr.write(f.render() + "\n")
    sys.stderr.write("\nFingerprints (for .secrets-baseline):\n")
    for f in all_findings:
        sys.stderr.write(f"  {fingerprint(f)}\n")
    sys.stderr.write(
        "\nIf this is a real secret: rotate it immediately (see "
        "docs/SECRETS.md) — do NOT just delete the file and re-commit.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

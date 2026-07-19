"""Pre-commit secret scanner — pure stdlib, no external dependencies.

Invoked as a git ``pre-commit`` hook (see ``scripts/install_git_hooks.py``
or ``.pre-commit-config.yaml``). Receives one or more file paths as
positional arguments and exits non-zero if any file contains a string
that looks like a secret.

Usage
-----

    python scripts/pre_commit_secret_scan.py path1 path2 ...
    python scripts/pre_commit_secret_scan.py --baseline .secrets-baseline path1 ...
    python scripts/pre_commit_secret_scan.py --staged   # enumerate staged files itself
    python scripts/pre_commit_secret_scan.py --all      # every git-tracked file (CI)

``--staged`` / ``--all`` ask git for the file list with ``-z`` (NUL
separators), so filenames containing spaces, brackets, or even newlines
are scanned correctly. Shell hooks should prefer ``--staged`` over
piping ``git diff --name-only`` through ``xargs``: the unquoted pipe
word-splits names like ``[USER] My Deck [B3].dck`` into fragments that
then look like missing files and were silently skipped.

Acceptance
----------

- Catches Anthropic / OpenAI / GitHub / AWS API keys, generic Bearer
  tokens (>=32 chars), and private-key armor headers.
- Does NOT false-positive on the project's documentation placeholders
  (``sk-ant-...``, ``sk-ant-api03-...``, ``Bearer YOUR_TOKEN_HERE``).
  Placeholder detection is applied to the MATCHED TOKEN only — a real
  high-entropy key is flagged even when the surrounding line contains
  words like ``EXAMPLE`` or a trailing ``...`` in a comment.
- Supports a baseline file (one fingerprint per line, format
  ``<path>:<pattern>:<token-sha256-prefix>``) for known false positives.
- Supports inline opt-out via ``# pragma: secret-scan-allow`` on the
  offending line (for deliberate test fixtures).
- Skips binary files and a built-in skip list (``*.zip``, ``*.lock``,
  image formats, etc.).

Pattern updates land in ``_PATTERNS`` below; tests in
``tests/test_pre_commit_secret_scan.py`` pin the contract.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
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

# Markers that identify a matched TOKEN as a documentation placeholder.
#
# SECURITY: these are matched against the token the secret regex
# captured, NOT the whole line. An earlier version skipped any LINE
# containing a marker, which meant a real key committed as
#   ANTHROPIC_API_KEY=sk-ant-<90 real chars>  # replaces the EXAMPLE key
# sailed through — the attacker (or an unlucky comment) controls the
# rest of the line, so nothing outside the token itself can be trusted
# to suppress a finding. A token that is literally
# ``sk-ant-api03-YOUR_KEY_HERE_PADDED...`` is a placeholder; a real
# high-entropy token on a line that merely mentions EXAMPLE is not.
_PLACEHOLDER_MARKERS = (
    "...",          # Bearer abc...xyz  (only Bearer's charset allows '.')
    "YOUR_",        # sk-ant-api03-YOUR_KEY_HERE_...
    "REPLACE",      # ghp_REPLACE_ME_...
    "EXAMPLE",      # AKIAEXAMPLE... / sk-...EXAMPLE...
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
    # The exact token the regex captured. Carried so the baseline
    # fingerprint can be content-based (see ``fingerprint``) — findings
    # stay allowlisted when unrelated edits shift line numbers.
    token: str

    def render(self) -> str:
        return f"  {self.path}:{self.line} [{self.pattern}] {self.snippet}"


def fingerprint(finding: Finding) -> str:
    """Stable identifier for the baseline file.

    Content-based: ``<path>:<pattern>:<sha256(token)[:12]>``. The old
    format embedded the line number, which meant ANY edit above a
    baselined line silently invalidated the allowlist entry (and the
    next commit failed on a long-accepted false positive). Hashing the
    token instead of embedding it keeps the baseline free of secret-
    shaped strings while remaining stable across reflows. 12 hex chars
    (48 bits) is plenty to avoid collisions within one repo's baseline.
    """

    digest = hashlib.sha256(finding.token.encode("utf-8")).hexdigest()[:12]
    return f"{finding.path}:{finding.pattern}:{digest}"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _token_is_placeholder(token: str) -> bool:
    """``True`` if the matched token itself is a documentation placeholder.

    Deliberately inspects ONLY the token, not the surrounding line:
    everything else on the line (comments, prose) is attacker- or
    accident-controllable and must not be able to suppress a real key.
    """

    return any(marker in token for marker in _PLACEHOLDER_MARKERS)


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
            # The explicit pragma is a per-line contract, so it stays a
            # line-level check (unlike the placeholder heuristic below).
            continue
        for label, regex in _PATTERNS:
            for match in regex.finditer(line):
                # Run the secret regexes FIRST, then apply the
                # placeholder test to the matched token only. The old
                # order (whole-line placeholder check before any regex)
                # let a real key commit cleanly whenever the same line
                # happened to contain '...' or 'EXAMPLE' etc.
                #
                # For patterns with a capture group (bearer_token) the
                # token is the group, not the full match — "Bearer " is
                # scaffolding, not token content.
                token = (
                    match.group(1) if match.re.groups else match.group(0)
                )
                if _token_is_placeholder(token):
                    continue
                f = Finding(
                    path=name,
                    line=lineno,
                    pattern=label,
                    snippet=_snippet_for(line, match.span()),
                    token=token,
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
    """Scan one file by path. Missing / binary / skip-listed files yield [].

    A missing path prints a WARNING instead of failing: deleted files
    can legitimately reach us (git hooks invoked with a raw file list,
    ``git commit -a`` after ``git rm``), but in a hook context a path
    that silently scans as "clean" because it doesn't exist is exactly
    how the xargs word-splitting bug hid files from the scanner — so
    we never skip silently.
    """

    if not path.exists():
        sys.stderr.write(
            f"secret-scan WARNING: path not found, skipping: {path}\n"
            "  (expected for deleted files; anything else may mean the "
            "hook mangled a filename — spaces/brackets need -z/--staged)\n"
        )
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
    # Report with forward slashes regardless of OS: fingerprints embed
    # this name, and a baseline written on Windows (backslashes) would
    # silently fail to match the same finding in Linux CI. Git itself
    # always emits forward-slash paths, so posix is the stable form.
    return scan_text(path.as_posix(), text, allowlist)


# ---------------------------------------------------------------------------
# Allowlist file
# ---------------------------------------------------------------------------

def load_allowlist(path: Path) -> Set[str]:
    """Read a baseline file. Missing file → empty set.

    Format: one fingerprint per line,
    ``<path>:<pattern>:<token-sha256-prefix>`` (see ``fingerprint``).
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
# Git file enumeration (--staged / --all)
# ---------------------------------------------------------------------------

def _git_list_files(args: Sequence[str]) -> List[Path]:
    """Run a git command that lists NUL-separated paths; return Paths.

    NUL separation (``-z``) is the whole point: it is the only output
    mode where git neither quotes nor escapes, so names with spaces,
    brackets, or newlines round-trip exactly. Paths come back relative
    to the repo root, which is also the cwd in every context that uses
    these modes (git runs hooks from the top level; CI runs from the
    checkout root).
    """

    proc = subprocess.run(
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(
            f"secret-scan: git enumeration failed ({' '.join(args)}): {stderr}"
        )
    # surrogateescape: git emits raw filename bytes; this keeps even
    # non-UTF-8 names round-trippable instead of crashing the hook.
    out = proc.stdout.decode("utf-8", errors="surrogateescape")
    return [Path(p) for p in out.split("\0") if p]


def _staged_files() -> List[Path]:
    """Staged (Added/Copied/Modified) files, space-safe.

    ``--diff-filter=ACM`` excludes deletions up front, so any path we
    then fail to find on disk genuinely deserves scan_path's warning.
    """

    return _git_list_files(
        [
            "git", "diff", "--cached", "--name-only",
            "--diff-filter=ACM", "-z",
        ]
    )


def _all_tracked_files() -> List[Path]:
    """Every git-tracked file — the CI full-tree sweep.

    ``git ls-files`` (not a filesystem walk) so untracked local junk,
    virtualenvs, and .gitignore'd scratch files can't make CI flaky:
    only content that would actually ship in the repo is scanned.
    """

    return _git_list_files(["git", "ls-files", "-z"])


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
            "Path to an allowlist file (one "
            "'<path>:<pattern>:<token-sha256-prefix>' fingerprint per "
            "line; '#' for comments). Defaults to '.secrets-baseline' "
            "in the repo root."
        ),
    )
    # Enumeration modes. Mutually exclusive with each other; either one
    # replaces the positional file list so the shell never has to relay
    # filenames (the word-splitting bug lived in that relay).
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--staged",
        action="store_true",
        help=(
            "Enumerate staged files via 'git diff --cached --name-only "
            "--diff-filter=ACM -z' instead of taking paths on the "
            "command line. Space/bracket/newline-safe; use this from "
            "shell hooks."
        ),
    )
    mode.add_argument(
        "--all",
        action="store_true",
        help=(
            "Scan every git-tracked file ('git ls-files -z'). Used by "
            "CI to cover contributors without local hooks."
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

    if (args.staged or args.all) and args.paths:
        # Refuse the ambiguous combination rather than guessing which
        # list the caller meant.
        parser.error("--staged/--all cannot be combined with explicit paths")

    allowlist = load_allowlist(args.baseline)

    if args.staged:
        paths = _staged_files()
    elif args.all:
        paths = _all_tracked_files()
    else:
        paths = list(args.paths)

    all_findings: List[Finding] = []
    for p in paths:
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

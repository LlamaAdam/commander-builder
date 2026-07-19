"""Tests for ``scripts/pre_commit_secret_scan.py``.

The scanner is a pure-stdlib pre-commit hook. We test the public surface:

- ``scan_text(name, text, allowlist)`` returns ``Finding`` objects
- ``Finding`` carries enough context (pattern label, line number, snippet)
  for a developer to act on
- ``load_allowlist(path)`` parses the baseline file
- The CLI entry point exits non-zero on findings and zero on a clean diff

We also lock down a couple of regression-flavor cases:

- The scanner ignores its own pattern definitions (the test file would
  trigger every pattern otherwise)
- Long random base64 strings (image data, hashes) don't false-positive
- The Anthropic *placeholder* ``sk-ant-...`` in committed docs doesn't
  trigger
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "pre_commit_secret_scan.py"


@pytest.fixture(scope="module")
def scanner():
    """Import ``scripts/pre_commit_secret_scan.py`` as a module."""

    spec = importlib.util.spec_from_file_location(
        "pre_commit_secret_scan", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        pytest.skip("pre_commit_secret_scan.py not found")
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec so @dataclass can look up
    # the defining module (Python 3.14+ requirement).
    sys.modules["pre_commit_secret_scan"] = module
    spec.loader.exec_module(module)
    return module


# --- scan_text ---------------------------------------------------------------


class TestScanText:
    def test_clean_text_returns_no_findings(self, scanner):
        text = "print('hello')\nx = 1 + 2\n"
        assert scanner.scan_text("foo.py", text, allowlist=set()) == []

    def test_detects_anthropic_key(self, scanner):
        # Use a fake but well-formed Anthropic key shape.
        text = "ANTHROPIC_API_KEY=sk-ant-api03-" + "A" * 95
        findings = scanner.scan_text("config.env", text, allowlist=set())
        assert len(findings) == 1
        assert findings[0].pattern == "anthropic_api_key"
        assert findings[0].path == "config.env"
        assert findings[0].line == 1

    def test_detects_openai_key(self, scanner):
        # OpenAI keys start with "sk-" and are >=40 alphanum chars.
        text = "OPENAI_API_KEY=sk-" + "B" * 48
        findings = scanner.scan_text("config.env", text, allowlist=set())
        labels = {f.pattern for f in findings}
        assert "openai_api_key" in labels

    def test_detects_github_pat(self, scanner):
        # GitHub fine-grained PAT format.
        text = "GITHUB_TOKEN=ghp_" + "C" * 36
        findings = scanner.scan_text("workflow.yml", text, allowlist=set())
        assert any(f.pattern == "github_pat" for f in findings)

    def test_detects_aws_access_key(self, scanner):
        text = "AWS_ACCESS_KEY_ID=AKIA" + "D" * 16
        findings = scanner.scan_text("infra.tf", text, allowlist=set())
        assert any(f.pattern == "aws_access_key" for f in findings)

    def test_detects_private_key_header(self, scanner):
        # The pragma below opts THIS source line out of the repo-wide
        # scan (the fixture is an armor header, not a key); it sits in
        # a comment outside the string, so scan_text's input is
        # unchanged and the assertion still exercises detection.
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC...\n"  # pragma: secret-scan-allow
        findings = scanner.scan_text("id_rsa", text, allowlist=set())
        assert any(f.pattern == "private_key" for f in findings)

    def test_detects_openssh_private_key(self, scanner):
        text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXkt...\n"  # pragma: secret-scan-allow
        findings = scanner.scan_text("id_ed25519", text, allowlist=set())
        assert any(f.pattern == "private_key" for f in findings)

    def test_detects_bearer_token_with_long_value(self, scanner):
        text = 'Authorization: Bearer ' + "E" * 40
        findings = scanner.scan_text("req.http", text, allowlist=set())
        assert any(f.pattern == "bearer_token" for f in findings)

    def test_ignores_short_bearer_placeholder(self, scanner):
        # Documentation often shows "Bearer <token>" or "Bearer YOUR_TOKEN".
        text = "Authorization: Bearer YOUR_TOKEN_HERE"
        findings = scanner.scan_text("docs.md", text, allowlist=set())
        # YOUR_TOKEN_HERE is 14 chars and would only trip the pattern
        # if we accepted very short bearer values.
        assert not any(f.pattern == "bearer_token" for f in findings)

    def test_ignores_anthropic_placeholder(self, scanner):
        # SECRETS.md shows "sk-ant-..." as a placeholder.
        text = "ANTHROPIC_API_KEY=sk-ant-..."
        findings = scanner.scan_text("docs/SECRETS.md", text, allowlist=set())
        assert not any(f.pattern == "anthropic_api_key" for f in findings)

    def test_ignores_truncated_key_in_doc_example(self, scanner):
        # Common "sk-ant-api03-..." pattern in documentation.
        text = "Run with ANTHROPIC_API_KEY=sk-ant-api03-... commander-..."
        findings = scanner.scan_text("README.md", text, allowlist=set())
        assert not any(f.pattern == "anthropic_api_key" for f in findings)

    # --- token-level placeholder check (regression: whole-line bypass) ---

    def test_real_key_with_placeholder_word_in_comment_is_caught(self, scanner):
        # REGRESSION (adversarial review 2026-07-19): the placeholder
        # check used to skip the whole LINE, so a real key committed
        # cleanly whenever the same line mentioned EXAMPLE / YOUR_ /
        # REPLACE anywhere — e.g. in a trailing comment.
        text = (
            "ANTHROPIC_API_KEY=sk-ant-api03-"
            + "A" * 95
            + "  # replaces the EXAMPLE key"
        )
        findings = scanner.scan_text("config.env", text, allowlist=set())
        assert any(f.pattern == "anthropic_api_key" for f in findings)

    def test_real_key_with_trailing_ellipsis_comment_is_caught(self, scanner):
        # Same bypass class: '...' in a comment used to whitelist the
        # entire line, real key included.
        text = (
            "ANTHROPIC_API_KEY=sk-ant-api03-"
            + "B" * 95
            + "  # rotate quarterly, see docs ..."
        )
        findings = scanner.scan_text("config.env", text, allowlist=set())
        assert any(f.pattern == "anthropic_api_key" for f in findings)

    def test_placeholder_shaped_token_is_suppressed(self, scanner):
        # The marker INSIDE the matched token still suppresses: this is
        # the legitimate documentation-placeholder case.
        text = "ANTHROPIC_API_KEY=sk-ant-api03-YOUR_KEY_HERE_" + "x" * 60
        findings = scanner.scan_text("docs.md", text, allowlist=set())
        assert not any(f.pattern == "anthropic_api_key" for f in findings)

    def test_placeholder_bearer_token_is_suppressed(self, scanner):
        # Bearer values are the captured group; a long padded
        # placeholder that clears the 40-char floor must still be
        # recognized as a placeholder via its own content.
        text = "Authorization: Bearer YOUR_TOKEN_HERE_" + "x" * 30
        findings = scanner.scan_text("docs.md", text, allowlist=set())
        assert not any(f.pattern == "bearer_token" for f in findings)

    def test_real_bearer_token_next_to_placeholder_prose_is_caught(self, scanner):
        # Prose around the token must not suppress it.
        text = "# EXAMPLE request:  Authorization: Bearer " + "F" * 48
        findings = scanner.scan_text("docs.md", text, allowlist=set())
        assert any(f.pattern == "bearer_token" for f in findings)

    def test_allowlist_suppresses_finding(self, scanner):
        text = "ANTHROPIC_API_KEY=sk-ant-api03-" + "A" * 95
        findings = scanner.scan_text("config.env", text, allowlist=set())
        assert findings, "sanity: pattern should fire"
        key = scanner.fingerprint(findings[0])
        allowlist = {key}
        suppressed = scanner.scan_text(
            "config.env", text, allowlist=allowlist
        )
        assert suppressed == []

    def test_line_numbers_are_one_indexed(self, scanner):
        text = "first line\nsecond line\nAKIA" + "Z" * 16 + "\n"
        findings = scanner.scan_text(
            "infra.tf", text, allowlist=set()
        )
        assert findings
        assert findings[0].line == 3

    def test_snippet_truncates_long_lines(self, scanner):
        # Pad with non-alphanumerics so the AWS regex's word-boundary
        # lookbehind still fires after a long prefix.
        text = "x " * 250 + "AKIA" + "Y" * 16
        findings = scanner.scan_text(
            "long.log", text, allowlist=set()
        )
        assert findings
        # Snippet should be short enough to be reasonable terminal output.
        assert len(findings[0].snippet) <= 250

    def test_inline_skip_comment_suppresses_finding(self, scanner):
        # Developers can mark a line ``# pragma: secret-scan-allow`` to
        # whitelist a deliberate test fixture.
        text = (
            "ANTHROPIC_API_KEY=sk-ant-api03-"
            + "A" * 95
            + "  # pragma: secret-scan-allow\n"
        )
        findings = scanner.scan_text("test.env", text, allowlist=set())
        assert findings == []

    def test_does_not_false_positive_on_normal_base64(self, scanner):
        # Sample of typical base64-encoded image data; should NOT trip
        # the AWS or generic-secret pattern.
        text = (
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAA"
            "AEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        )
        findings = scanner.scan_text("img.html", text, allowlist=set())
        # We accept that some patterns may match by coincidence; the
        # key contract is that AWS and Anthropic patterns do NOT fire.
        labels = {f.pattern for f in findings}
        assert "anthropic_api_key" not in labels
        assert "aws_access_key" not in labels


# --- fingerprint -------------------------------------------------------------


class TestFingerprint:
    def test_fingerprint_is_content_based_not_line_based(self, scanner):
        # REGRESSION: fingerprints used to embed the line number, so
        # any edit ABOVE a baselined line silently invalidated the
        # allowlist entry. Content-based fingerprints survive reflows.
        secret_line = "ANTHROPIC_API_KEY=sk-ant-api03-" + "A" * 95
        f1 = scanner.scan_text("config.env", secret_line, allowlist=set())[0]
        shifted = "# new comment\n# another\n" + secret_line
        f2 = scanner.scan_text("config.env", shifted, allowlist=set())[0]
        assert f1.line != f2.line, "sanity: the finding really moved"
        assert scanner.fingerprint(f1) == scanner.fingerprint(f2)

    def test_fingerprint_does_not_leak_the_token(self, scanner):
        # The baseline is a committed file; it must never contain the
        # secret-shaped string itself, only a hash prefix.
        token = "sk-ant-api03-" + "A" * 95
        findings = scanner.scan_text(
            "config.env", "KEY=" + token, allowlist=set()
        )
        fp = scanner.fingerprint(findings[0])
        assert token not in fp
        assert fp.startswith("config.env:anthropic_api_key:")

    def test_different_tokens_get_different_fingerprints(self, scanner):
        fa = scanner.scan_text(
            "e.env", "KEY=sk-ant-api03-" + "A" * 95, allowlist=set()
        )[0]
        fb = scanner.scan_text(
            "e.env", "KEY=sk-ant-api03-" + "B" * 95, allowlist=set()
        )[0]
        assert scanner.fingerprint(fa) != scanner.fingerprint(fb)


# --- allowlist file ----------------------------------------------------------


class TestAllowlist:
    def test_load_missing_file_returns_empty_set(self, scanner, tmp_path):
        result = scanner.load_allowlist(tmp_path / "does-not-exist.txt")
        assert result == set()

    def test_load_parses_one_fingerprint_per_line(self, scanner, tmp_path):
        baseline = tmp_path / "baseline.txt"
        baseline.write_text(
            "# Comment line\n"
            "config.env:anthropic_api_key:3f2a9c81d0e4\n"
            "\n"
            "scripts/demo.sh:openai_api_key:aa00bb11cc22\n"
        )
        result = scanner.load_allowlist(baseline)
        assert "config.env:anthropic_api_key:3f2a9c81d0e4" in result
        assert "scripts/demo.sh:openai_api_key:aa00bb11cc22" in result
        assert len(result) == 2


# --- CLI ---------------------------------------------------------------------


class TestCli:
    def test_main_clean_files_returns_zero(self, scanner, tmp_path):
        f = tmp_path / "clean.py"
        f.write_text("x = 1 + 2\n")
        rc = scanner.main([str(f)])
        assert rc == 0

    def test_main_dirty_file_returns_nonzero(self, scanner, tmp_path, capsys):
        f = tmp_path / "dirty.env"
        f.write_text("ANTHROPIC_API_KEY=sk-ant-api03-" + "X" * 95 + "\n")
        rc = scanner.main([str(f)])
        assert rc != 0
        out = capsys.readouterr()
        # User-facing output should name the file and the pattern.
        combined = out.out + out.err
        assert "dirty.env" in combined
        assert "anthropic_api_key" in combined

    def test_main_with_allowlist_suppresses(self, scanner, tmp_path):
        f = tmp_path / "dirty.env"
        f.write_text("ANTHROPIC_API_KEY=sk-ant-api03-" + "X" * 95 + "\n")

        # Build the fingerprint exactly the way the scanner advertises
        # it (content-based) rather than hand-assembling the format —
        # keeps the test honest if the format evolves again.
        findings = scanner.scan_path(f, set())
        assert findings, "sanity: the file should trip the scanner"
        baseline = tmp_path / "baseline.txt"
        baseline.write_text(scanner.fingerprint(findings[0]) + "\n")
        rc = scanner.main(["--baseline", str(baseline), str(f)])
        assert rc == 0

    def test_main_skips_binary_files(self, scanner, tmp_path):
        # A real binary file shouldn't trip the scanner.
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024)
        rc = scanner.main([str(f)])
        assert rc == 0

    def test_main_skips_files_in_skip_list(self, scanner, tmp_path):
        # Files matching the built-in skip patterns (e.g. *.lock, *.zip)
        # should be ignored even if they happen to look secret-bearing.
        zip_file = tmp_path / "cards.zip"
        zip_file.write_text("AKIA" + "Z" * 16 + "\n")
        rc = scanner.main([str(zip_file)])
        assert rc == 0

    def test_main_missing_file_warns_but_does_not_fail(
        self, scanner, tmp_path, capsys
    ):
        # Argparse-supplied paths can include deleted files when git
        # invokes the hook on `git commit -a` after a `git rm` — that
        # must not abort the commit. But it must WARN: a silently
        # "clean" missing path is exactly how the xargs word-splitting
        # bug hid real files from the scanner.
        rc = scanner.main([str(tmp_path / "does-not-exist.py")])
        assert rc == 0
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "does-not-exist.py" in err


# --- git enumeration modes (--staged / --all) --------------------------------


def _git(repo: Path, *args: str) -> None:
    """Run a git command in ``repo``; fail the test loudly on error."""

    proc = subprocess.run(
        # Inline identity config so commits work on runners with no
        # global git config; no network, no signing.
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"git {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
    )


@pytest.fixture()
def git_repo(tmp_path):
    """A throwaway git repo with one initial commit."""

    _git(tmp_path, "init", "-q")
    # An initial commit so `git diff --cached` has a HEAD to diff
    # against on every git version we might meet.
    _git(tmp_path, "commit", "--allow-empty", "-m", "init")
    return tmp_path


class TestEnumerationModes:
    """--staged / --all must see EVERY file, including hostile names.

    REGRESSION (adversarial review 2026-07-19): the shell hook used to
    do `echo "$staged_files" | xargs python ...`, which word-split
    '[USER] My Deck [B3].dck' into fragments; each fragment was a
    missing path that scan_path silently ignored — so the real file was
    never scanned at all. The scanner now enumerates staged files
    itself over a NUL-separated channel.
    """

    def test_staged_scans_filename_with_spaces_and_brackets(
        self, scanner, git_repo, monkeypatch, capsys
    ):
        deck = git_repo / "[USER] My Deck [B3].dck"
        deck.write_text(
            "ANTHROPIC_API_KEY=sk-ant-api03-" + "S" * 95 + "\n",
            encoding="utf-8",
        )
        _git(git_repo, "add", "-A")
        monkeypatch.chdir(git_repo)
        rc = scanner.main(["--staged"])
        assert rc != 0, "the staged secret in the bracketed filename must fail"
        err = capsys.readouterr().err
        assert "[USER] My Deck [B3].dck" in err
        assert "anthropic_api_key" in err

    def test_staged_clean_repo_passes(
        self, scanner, git_repo, monkeypatch
    ):
        (git_repo / "clean file.py").write_text("x = 1\n", encoding="utf-8")
        _git(git_repo, "add", "-A")
        monkeypatch.chdir(git_repo)
        assert scanner.main(["--staged"]) == 0

    def test_staged_ignores_deleted_files_without_warning(
        self, scanner, git_repo, monkeypatch, capsys
    ):
        # --diff-filter=ACM excludes deletions, so a staged `git rm`
        # produces neither a finding nor a spurious missing-path
        # warning.
        f = git_repo / "doomed.py"
        f.write_text("x = 1\n", encoding="utf-8")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-m", "add doomed")
        _git(git_repo, "rm", "-q", "doomed.py")
        monkeypatch.chdir(git_repo)
        rc = scanner.main(["--staged"])
        assert rc == 0
        assert "WARNING" not in capsys.readouterr().err

    def test_all_scans_every_tracked_file(
        self, scanner, git_repo, monkeypatch, capsys
    ):
        # CI mode: committed (tracked) secrets are found even when
        # nothing is staged.
        f = git_repo / "committed [secrets].env"
        f.write_text("GITHUB_TOKEN=ghp_" + "G" * 40 + "\n", encoding="utf-8")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-m", "oops")
        monkeypatch.chdir(git_repo)
        rc = scanner.main(["--all"])
        assert rc != 0
        err = capsys.readouterr().err
        assert "committed [secrets].env" in err
        assert "github_pat" in err

    def test_all_untracked_secret_is_not_ci_concern(
        self, scanner, git_repo, monkeypatch
    ):
        # --all uses `git ls-files`, not a filesystem walk: untracked
        # local scratch (e.g. a developer's real .env) must not fail
        # CI — it is not shipping anywhere.
        (git_repo / ".env").write_text(
            "ANTHROPIC_API_KEY=sk-ant-api03-" + "U" * 95 + "\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(git_repo)
        assert scanner.main(["--all"]) == 0

    def test_mode_flags_reject_explicit_paths(self, scanner, git_repo):
        # Ambiguous invocation must error rather than guess.
        with pytest.raises(SystemExit):
            scanner.main(["--staged", str(git_repo / "x.py")])


# --- repo integration smoke -------------------------------------------------


class TestRepoSmoke:
    """The current repo should be clean against the scanner."""

    def test_committed_docs_files_are_clean(self, scanner):
        # SECRETS.md contains "sk-ant-api03-..." as a placeholder; the
        # scanner should not fire on the committed content.
        secrets_doc = REPO_ROOT / "docs" / "SECRETS.md"
        text = secrets_doc.read_text(encoding="utf-8")
        findings = scanner.scan_text(
            str(secrets_doc), text, allowlist=set()
        )
        assert findings == [], (
            "SECRETS.md should not contain any real secret patterns; "
            f"got: {[(f.pattern, f.line) for f in findings]}"
        )

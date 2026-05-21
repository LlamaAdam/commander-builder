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
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKC...\n"
        findings = scanner.scan_text("id_rsa", text, allowlist=set())
        assert any(f.pattern == "private_key" for f in findings)

    def test_detects_openssh_private_key(self, scanner):
        text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXkt...\n"
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


# --- allowlist file ----------------------------------------------------------


class TestAllowlist:
    def test_load_missing_file_returns_empty_set(self, scanner, tmp_path):
        result = scanner.load_allowlist(tmp_path / "does-not-exist.txt")
        assert result == set()

    def test_load_parses_one_fingerprint_per_line(self, scanner, tmp_path):
        baseline = tmp_path / "baseline.txt"
        baseline.write_text(
            "# Comment line\n"
            "config.env:1:anthropic_api_key\n"
            "\n"
            "scripts/demo.sh:5:openai_api_key\n"
        )
        result = scanner.load_allowlist(baseline)
        assert "config.env:1:anthropic_api_key" in result
        assert "scripts/demo.sh:5:openai_api_key" in result
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

        baseline = tmp_path / "baseline.txt"
        # Fingerprint format: <relative-path>:<line>:<pattern>
        baseline.write_text(f"{f}:1:anthropic_api_key\n")
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

    def test_main_handles_missing_file_gracefully(self, scanner, tmp_path):
        # Argparse-supplied paths can include deleted files when git
        # invokes the hook on `git commit -a` after a `git rm`. The
        # scanner should not crash.
        rc = scanner.main([str(tmp_path / "does-not-exist.py")])
        assert rc == 0


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

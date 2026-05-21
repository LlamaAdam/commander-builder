"""Tests for the pre-commit secret scanner (scripts/scan_secrets.py)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scan_secrets  # noqa: E402


def _diff(added_lines, file="config.py"):
    body = f"+++ b/{file}\n" + "\n".join("+" + ln for ln in added_lines)
    return "diff --git a/%s b/%s\n%s" % (file, file, body)


def test_catches_anthropic_key():
    hits = scan_secrets.scan_diff(_diff(['ANTHROPIC_API_KEY = "sk-ant-api03-AbCdEf0123456789XyZ"']))
    assert any(name == "anthropic_api_key" for name, _, _ in hits)


def test_catches_bearer_and_jwt():
    hits = scan_secrets.scan_diff(_diff([
        'h = {"Authorization": "Bearer abcdef0123456789ABCDEF0123"}',
        'tok = "eyJhbGciOiJID.eyJzdWIiOiIxMjM0NQ.SflKxwRJSMeKKF2QT4"',
    ]))
    names = {n for n, _, _ in hits}
    assert "bearer_token" in names and "jwt" in names


def test_ignores_removed_lines():
    # A secret on a REMOVED ('-') line is being deleted -- not a leak.
    diff = "+++ b/config.py\n-ANTHROPIC_API_KEY = \"sk-ant-api03-AbCdEf0123456789XyZ\""
    assert scan_secrets.scan_diff(diff) == []


def test_ignores_diff_header_plusplusplus():
    # The '+++ b/file' header must never be treated as an added line.
    assert scan_secrets.scan_diff("+++ b/sk-ant-not-a-secret.py\n+x = 1") == []


def test_allows_placeholders():
    hits = scan_secrets.scan_diff(_diff([
        '# ANTHROPIC_API_KEY=sk-ant-...',
        'key = "<your-key>"',
        'EXAMPLE_KEY = "sk-ant-EXAMPLE"',
    ]))
    assert hits == []


def test_clean_diff_passes():
    assert scan_secrets.scan_diff(_diff(['x = 1', 'def foo():', '    return 42'])) == []


def test_skips_scanners_own_files():
    # The scanner + its tests carry credential-shaped fixtures by design; they
    # must be exempt so the hook doesn't trip on itself.
    secret = ['k = "sk-ant-api03-AbCdEf0123456789XyZ"']
    assert scan_secrets.scan_diff(_diff(secret, file="tests/test_scan_secrets.py")) == []
    assert scan_secrets.scan_diff(_diff(secret, file="scripts/scan_secrets.py")) == []
    # ...but a real source file is still scanned.
    assert scan_secrets.scan_diff(_diff(secret, file="src/commander_builder/foo.py"))


def test_redacts_snippet_in_hit():
    hits = scan_secrets.scan_diff(_diff(['k = "sk-ant-api03-AbCdEf0123456789XyZ"']))
    assert hits, "should have caught the key"
    _name, _file, snippet = hits[0]
    # Snippet is redacted (middle elided) -- doesn't echo the full secret.
    assert "…" in snippet and snippet != "sk-ant-api03-AbCdEf0123456789XyZ"

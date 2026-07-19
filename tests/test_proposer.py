"""proposer.py tests — manual backend works fully, Claude/Ollama stubs.

The router fallback behavior is the most important guarantee: if Claude is
configured but unavailable (no key, no SDK, etc.), the call falls back to
manual rather than crashing the iteration loop.
"""
import json
from pathlib import Path

import pytest

from commander_builder.proposer import (
    ProposerConfig,
    ProposerInput,
    ProposerOutput,
    claude_propose,
    manual_propose,
    ollama_propose,
    propose,
)


def _make_input(tmp_path, deck_id="abc-123") -> ProposerInput:
    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text("[Commander]\n1 Test\n[Main]\n1 Sol Ring\n", encoding="utf-8")
    return ProposerInput(deck_path=deck, bracket=3, deck_id=deck_id)


def _write_manifest(path: Path, **overrides) -> None:
    payload = {
        "added": ["NewCard"],
        "removed": ["OldCard"],
        "rationale": "tightened removal",
        "audit_version": "v3",
        **overrides,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- ProposerOutput --------------------------------------------------------

def test_proposer_output_to_dict_fills_timestamp():
    out = ProposerOutput(added=["A"], removed=["B"], rationale="r")
    d = out.to_dict()
    assert d["audit_timestamp"] is not None
    assert d["added"] == ["A"]


def test_proposer_output_preserves_explicit_timestamp():
    fixed = "2026-04-26T00:00:00+00:00"
    out = ProposerOutput(audit_timestamp=fixed)
    assert out.to_dict()["audit_timestamp"] == fixed


# --- manual_propose --------------------------------------------------------

def test_manual_propose_reads_default_manifest_path(tmp_path):
    input_ = _make_input(tmp_path)
    manifest_path = input_.deck_path.parent / f"{input_.deck_path.name}.audit_manifest.json"
    _write_manifest(manifest_path, rationale="from default path")

    out = manual_propose(input_, ProposerConfig())
    assert out.added == ["NewCard"]
    assert out.rationale == "from default path"
    assert out.source == "manual"
    assert out.deck_id == "abc-123"


def test_manual_propose_respects_explicit_manifest_path(tmp_path):
    input_ = _make_input(tmp_path)
    custom = tmp_path / "elsewhere.json"
    _write_manifest(custom, rationale="elsewhere")

    out = manual_propose(input_, ProposerConfig(manifest_path=custom))
    assert out.rationale == "elsewhere"


def test_manual_propose_raises_when_manifest_missing(tmp_path):
    input_ = _make_input(tmp_path)
    with pytest.raises(FileNotFoundError) as exc_info:
        manual_propose(input_, ProposerConfig())
    # Error message should point the user at how to produce the manifest.
    assert "Moxfield audit prompt" in str(exc_info.value)


def test_manual_propose_handles_minimal_manifest(tmp_path):
    """Older manifests may omit `rationale` / `audit_version`. Defaults
    should kick in cleanly."""
    input_ = _make_input(tmp_path)
    custom = tmp_path / "minimal.json"
    custom.write_text(json.dumps({"added": [], "removed": []}), encoding="utf-8")

    out = manual_propose(input_, ProposerConfig(manifest_path=custom))
    assert out.added == []
    assert out.audit_version == "v3"  # default
    assert out.rationale == ""


def test_manual_propose_input_deck_id_wins_over_manifest(tmp_path):
    """When the caller passes a deck_id (e.g. resolved from .dck metadata),
    it should override whatever's in the manifest."""
    input_ = ProposerInput(
        deck_path=tmp_path / "x.dck", bracket=3, deck_id="from-input",
    )
    (tmp_path / "x.dck").write_text("[Commander]\n1 Test", encoding="utf-8")
    manifest = tmp_path / "x.dck.audit_manifest.json"
    _write_manifest(manifest, deck_id="from-manifest")

    out = manual_propose(input_, ProposerConfig())
    assert out.deck_id == "from-input"


# --- claude_propose / ollama_propose stubs ---------------------------------

def test_claude_propose_unimplemented_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    input_ = _make_input(tmp_path)
    with pytest.raises(NotImplementedError) as exc_info:
        claude_propose(input_, ProposerConfig())
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_claude_propose_unimplemented_without_sdk(tmp_path, monkeypatch):
    """Key set but `anthropic` SDK fails to import → NotImplementedError.
    Override `__import__` for the `anthropic` name only."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    import builtins
    import sys
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)

    real_import = builtins.__import__
    def fake_import(name, *a, **kw):
        if name == "anthropic":
            raise ImportError("simulated: anthropic not installed")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    input_ = _make_input(tmp_path)
    with pytest.raises(NotImplementedError, match="pip install anthropic"):
        claude_propose(input_, ProposerConfig())


@pytest.mark.slow
def test_ollama_propose_is_unimplemented(tmp_path):
    input_ = _make_input(tmp_path)
    with pytest.raises(NotImplementedError):
        ollama_propose(input_, ProposerConfig())


# --- propose() router behavior --------------------------------------------

def test_propose_uses_manual_by_default(tmp_path):
    input_ = _make_input(tmp_path)
    manifest = input_.deck_path.parent / f"{input_.deck_path.name}.audit_manifest.json"
    _write_manifest(manifest, rationale="manual default")
    out = propose(input_)
    assert out.source == "manual"
    assert out.rationale == "manual default"


def test_propose_falls_back_to_manual_when_claude_unwired(tmp_path, monkeypatch):
    """use_claude=True but no key / no SDK → fall back to manual rather than
    crashing. This is the whole reason the router exists."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    input_ = _make_input(tmp_path)
    manifest = input_.deck_path.parent / f"{input_.deck_path.name}.audit_manifest.json"
    _write_manifest(manifest, rationale="fallback")

    out = propose(input_, ProposerConfig(use_claude=True))
    assert out.source == "manual"


@pytest.mark.slow
def test_propose_falls_back_when_both_llm_backends_unwired(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    input_ = _make_input(tmp_path)
    manifest = input_.deck_path.parent / f"{input_.deck_path.name}.audit_manifest.json"
    _write_manifest(manifest)

    out = propose(input_, ProposerConfig(use_claude=True, use_ollama=True))
    assert out.source == "manual"


def test_propose_propagates_when_manual_also_fails(tmp_path):
    """No manifest on disk, no LLM backends wired → caller gets the explicit
    FileNotFoundError, not a silent empty manifest."""
    input_ = _make_input(tmp_path)
    with pytest.raises(FileNotFoundError):
        propose(input_, ProposerConfig())


def test_propose_handles_claude_runtime_error_with_warn(tmp_path, monkeypatch, capsys):
    """If claude_propose raises something OTHER than NotImplementedError,
    log a WARN and fall through. (e.g. transient API outage.)"""
    input_ = _make_input(tmp_path)
    manifest = input_.deck_path.parent / f"{input_.deck_path.name}.audit_manifest.json"
    _write_manifest(manifest)

    def boom(*a, **kw):
        raise RuntimeError("rate limited")
    monkeypatch.setattr("commander_builder.proposer.claude_propose", boom)

    out = propose(input_, ProposerConfig(use_claude=True))
    assert out.source == "manual"
    captured = capsys.readouterr()
    assert "WARN" in captured.out
    assert "claude_propose failed" in captured.out


def test_propose_garbage_claude_response_is_loud_not_manual(
        tmp_path, monkeypatch):
    """Regression for the misleading-fallback bug: when Claude RESPONDS but
    with garbage, propose() must re-raise the parse error — NOT fall
    through to manual_propose, whose 'No manifest at ...'
    FileNotFoundError masked the real failure. manual_propose must not
    even be invoked."""
    from commander_builder._llm_json import LLMJsonError
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _mock_claude_sdk(monkeypatch, "Here are my thoughts on the deck...")

    manual_calls: list = []
    def spy_manual(input_, config):
        manual_calls.append(input_)
        raise AssertionError("manual_propose must not be reached")
    monkeypatch.setattr("commander_builder.proposer.manual_propose", spy_manual)

    # Deliberately NO manifest file on disk: the old behavior surfaced
    # FileNotFoundError here instead of the parse failure.
    input_ = _make_input(tmp_path)
    with pytest.raises(LLMJsonError, match="claude_propose"):
        propose(input_, ProposerConfig(use_claude=True))
    assert manual_calls == []


def test_propose_garbage_ollama_response_is_loud_not_manual(
        tmp_path, monkeypatch):
    """Same loud-error rule for the Ollama backend: a daemon that answers
    with non-JSON must surface LLMJsonError, not degrade to manual."""
    from commander_builder._llm_json import LLMJsonError
    payload = json.dumps({"response": "not json at all"}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        return _FakeUrlOpenResponse(payload)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    input_ = _make_input(tmp_path)
    with pytest.raises(LLMJsonError, match="ollama_propose"):
        propose(input_, ProposerConfig(use_ollama=True))


# --- claude_propose success path (mocked Anthropic SDK) --------------------

def _fake_anthropic_response(text: str):
    class _Block:
        def __init__(self, t): self.text = t
    class _Msg:
        def __init__(self, t): self.content = [_Block(t)]
    return _Msg(text)


def test_claude_propose_parses_valid_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    manifest = json.dumps({
        "added": ["NewCard"], "removed": ["OldCard"],
        "rationale": "tightened removal package",
        "audit_version": "v3",
    })

    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _fake_anthropic_response(manifest)
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    input_ = _make_input(tmp_path)
    out = claude_propose(input_, ProposerConfig())
    assert out.source == "claude"
    assert out.added == ["NewCard"]
    assert out.removed == ["OldCard"]
    assert out.rationale == "tightened removal package"
    # monkeypatch.setitem auto-cleans up.


def _mock_claude_sdk(monkeypatch, text: str):
    """Install a fake `anthropic` module whose client returns `text`."""
    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _fake_anthropic_response(text)
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)


def test_claude_propose_strips_markdown_code_fences(tmp_path, monkeypatch):
    """Claude sometimes wraps JSON in ```json ... ``` despite being told not
    to. We strip the fences before parsing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    fenced = (
        "```json\n"
        + json.dumps({"added": ["A"], "removed": ["B"], "rationale": "x"})
        + "\n```"
    )
    _mock_claude_sdk(monkeypatch, fenced)

    out = claude_propose(_make_input(tmp_path), ProposerConfig())
    assert out.added == ["A"]
    assert out.removed == ["B"]
    # monkeypatch.setitem auto-cleans up.


def test_claude_propose_parses_prose_then_fenced_json(tmp_path, monkeypatch):
    """Regression: a response with prose BEFORE the ```json fence used to
    throw JSONDecodeError (the old strip only fired when the response
    STARTED with ```), which propose() then buried under manual_propose's
    misleading FileNotFoundError."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _mock_claude_sdk(
        monkeypatch,
        "Looking at this deck, the removal package is thin.\n```json\n"
        + json.dumps({"added": ["A"], "removed": ["B"], "rationale": "x"})
        + "\n```\nHappy to elaborate.",
    )

    out = claude_propose(_make_input(tmp_path), ProposerConfig())
    assert out.added == ["A"]
    assert out.removed == ["B"]


def test_claude_propose_garbage_raises_llm_json_error(tmp_path, monkeypatch):
    """Unparseable prose => LLMJsonError naming the call site + model and
    quoting the response — NOT a JSONDecodeError, and definitely not a
    fall-through to manual."""
    from commander_builder._llm_json import LLMJsonError
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _mock_claude_sdk(monkeypatch, "I would swap a few cards around.")

    with pytest.raises(LLMJsonError) as exc_info:
        claude_propose(_make_input(tmp_path), ProposerConfig())
    msg = str(exc_info.value)
    assert "claude_propose" in msg
    assert "claude-sonnet-4-5" in msg           # model context
    assert "swap a few cards" in msg            # response snippet


def test_claude_propose_truncated_json_raises_llm_json_error(
        tmp_path, monkeypatch):
    """max_tokens cutoff (object never closes) => specific LLMJsonError."""
    from commander_builder._llm_json import LLMJsonError
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _mock_claude_sdk(
        monkeypatch, '{"added": ["A", "B"], "removed": ["C", "D"], "ratio'
    )
    with pytest.raises(LLMJsonError, match="claude_propose"):
        claude_propose(_make_input(tmp_path), ProposerConfig())


def test_claude_propose_input_deck_id_overrides_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    manifest = json.dumps({
        "added": [], "removed": [], "rationale": "",
        "deck_id": "from-claude",
    })

    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _fake_anthropic_response(manifest)
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    input_ = _make_input(tmp_path, deck_id="from-input")
    out = claude_propose(input_, ProposerConfig())
    assert out.deck_id == "from-input"
    # monkeypatch.setitem auto-cleans up.


def test_claude_propose_handles_empty_response(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")

    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _fake_anthropic_response("")
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    with pytest.raises(RuntimeError, match="empty response"):
        claude_propose(_make_input(tmp_path), ProposerConfig())
    # monkeypatch.setitem auto-cleans up.


# --- ollama_propose success path (mocked HTTP) -----------------------------

class _FakeUrlOpenResponse:
    def __init__(self, body: bytes):
        self._body = body
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_ollama_propose_parses_daemon_response(tmp_path, monkeypatch):
    inner = json.dumps({
        "added": ["LocalCard"], "removed": ["StaleCard"],
        "rationale": "tightened curve", "audit_version": "v3",
    })
    payload = json.dumps({"response": inner}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        return _FakeUrlOpenResponse(payload)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    out = ollama_propose(_make_input(tmp_path), ProposerConfig())
    assert out.source == "ollama"
    assert out.added == ["LocalCard"]
    assert out.removed == ["StaleCard"]


def test_ollama_propose_falls_back_when_daemon_unreachable(tmp_path, monkeypatch):
    import urllib.error
    def network_down(req, timeout=None):
        raise urllib.error.URLError("daemon not running")
    monkeypatch.setattr("urllib.request.urlopen", network_down)

    with pytest.raises(NotImplementedError, match="Ollama daemon not reachable"):
        ollama_propose(_make_input(tmp_path), ProposerConfig())


# ---------------------------------------------------------------------------
# `python -m commander_builder.proposer` entry-point regression test
#
# Background: running proposer.py directly used to fail with a circular-
# import (`cannot import name 'auto_curate_main' from partially
# initialized module 'commander_builder._proposer_cli'`). Caught during
# the 2026-05-20 web/curator end-to-end smoke test. Fixed by aliasing
# the `__main__` module under `commander_builder.proposer` before any
# sibling import fires. This pins the contract.
# ---------------------------------------------------------------------------


def test_python_m_commander_builder_proposer_help_runs():
    """`python -m commander_builder.proposer --help` must exit 0.

    This is the regression test for the 2026-05-20 circular-import bug
    where -m invocation crashed because `_proposer_cli` re-loaded
    proposer.py before its module-scope export of `auto_curate_main`
    completed.
    """
    import subprocess
    import sys as _sys

    result = subprocess.run(
        [_sys.executable, "-m", "commander_builder.proposer", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"python -m commander_builder.proposer --help failed:\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:1500]}"
    )
    # Sanity: help text mentions the actual CLI surface so we know we
    # reached the real entry point (not a stub or early-exit).
    assert "commander-auto-curate" in result.stdout
    assert "--mode" in result.stdout

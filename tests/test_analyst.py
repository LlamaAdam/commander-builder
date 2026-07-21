"""analyst.py tests — heuristic verdict logic + router behavior + LLM backends.

LLM backends are mocked (anthropic SDK stand-in for `claude_verdict`,
urlopen stand-in for `ollama_verdict`) so the suite stays offline. Stub
fallback paths are also verified — the router catches NotImplementedError
and degrades to the heuristic.
"""
import json

import pytest

from commander_builder.analyst import (
    AnalystConfig,
    AnalystInput,
    Verdict,
    analyze,
    claude_verdict,
    heuristic_verdict,
    ollama_verdict,
)


def _input(*, old_wins=4, new_wins=4, draws=2, total=10, manifest=None) -> AnalystInput:
    return AnalystInput(
        deck_name="test.dck",
        bracket=3,
        audit_manifest=manifest or {"added": ["A"], "removed": ["B"], "rationale": "x"},
        sim_report={
            "total_games": total,
            "draws": draws,
            "old_stats": {"wins": old_wins},
            "new_stats": {"wins": new_wins},
        },
    )


# --- heuristic_verdict -----------------------------------------------------

def test_heuristic_kept_when_strong_improvement():
    v = heuristic_verdict(_input(old_wins=4, new_wins=10, draws=0, total=14), AnalystConfig())
    assert v.label == "kept"
    assert v.confidence >= 0.75
    assert "10-4" in v.reasoning


def test_heuristic_reverted_when_strong_regression():
    v = heuristic_verdict(_input(old_wins=10, new_wins=4, draws=0, total=14), AnalystConfig())
    assert v.label == "reverted"
    assert v.confidence >= 0.75


def test_heuristic_neutral_when_within_noise():
    v = heuristic_verdict(_input(old_wins=5, new_wins=6, draws=0, total=11), AnalystConfig())
    assert v.label == "neutral"
    assert v.confidence < 0.75


def test_heuristic_inconclusive_when_too_many_draws():
    # 18 of 20 games drew (matches the real Hakbal-vs-Hash smoke test).
    v = heuristic_verdict(_input(old_wins=1, new_wins=1, draws=18, total=20), AnalystConfig())
    assert v.label == "neutral"
    assert "Inconclusive" in v.reasoning
    assert any("decks_drew_too_often" in lesson for lesson in v.lessons)


def test_heuristic_inputs_lessons_for_kept():
    v = heuristic_verdict(_input(old_wins=2, new_wins=8, draws=0, total=10), AnalystConfig())
    assert any("swap_kept" in lesson for lesson in v.lessons)


def test_heuristic_inputs_lessons_for_reverted():
    v = heuristic_verdict(_input(old_wins=8, new_wins=2, draws=0, total=10), AnalystConfig())
    assert any("swap_reverted" in lesson for lesson in v.lessons)


# --- analyze() router ------------------------------------------------------

def test_analyze_returns_heuristic_when_strong_signal():
    """High-confidence heuristic short-circuits — no LLM escalation needed."""
    v = analyze(_input(old_wins=2, new_wins=10, draws=0, total=12))
    assert v.source == "heuristic"
    assert v.label == "kept"


def test_analyze_falls_back_to_heuristic_when_llm_unwired(monkeypatch):
    """Even with use_claude=True, the backends raise NotImplementedError when
    unwired (no API key, no ollama daemon); router falls back to heuristic."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import urllib.error
    def network_down(req, timeout=None):
        raise urllib.error.URLError("no daemon")
    monkeypatch.setattr("urllib.request.urlopen", network_down)

    config = AnalystConfig(use_claude=True, use_ollama=True)
    # Noise band: heuristic confidence is low, would normally escalate.
    v = analyze(_input(old_wins=5, new_wins=6, draws=0, total=11), config=config)
    assert v.source == "heuristic"


def test_analyze_default_config_no_llm_no_escalation():
    config = AnalystConfig()
    assert config.use_claude is False
    assert config.use_ollama is False
    v = analyze(_input(old_wins=4, new_wins=5, draws=0, total=9), config=config)
    assert v.source == "heuristic"


# --- LLM stubs ------------------------------------------------------------

def test_claude_verdict_unimplemented_without_key(monkeypatch):
    """No ANTHROPIC_API_KEY → falls back via NotImplementedError."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(NotImplementedError, match="ANTHROPIC_API_KEY"):
        claude_verdict(_input(), AnalystConfig())


def test_ollama_verdict_unimplemented_when_daemon_unreachable(monkeypatch):
    import urllib.error
    def network_down(req, timeout=None):
        raise urllib.error.URLError("no daemon")
    monkeypatch.setattr("urllib.request.urlopen", network_down)
    with pytest.raises(NotImplementedError, match="not reachable"):
        ollama_verdict(_input(), AnalystConfig())


# --- Verdict serialization -------------------------------------------------

def test_verdict_to_dict_round_trips():
    v = Verdict(label="kept", confidence=0.9, reasoning="x", lessons=["y"])
    d = v.to_dict()
    assert d["label"] == "kept"
    assert d["confidence"] == 0.9
    assert d["lessons"] == ["y"]


# --- claude_verdict success path (mocked Anthropic client) -----------------

def _fake_anthropic_response(text: str):
    """Build a minimal stand-in for an `anthropic.types.Message`."""
    class _Block:
        def __init__(self, t): self.text = t
    class _Msg:
        def __init__(self, t): self.content = [_Block(t)]
    return _Msg(text)


def test_claude_verdict_parses_valid_json_response(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")

    fake_payload = json.dumps({
        "label": "kept",
        "confidence": 0.92,
        "reasoning": "New version dominated 12-2.",
        "lessons": ["finishers reduced draw rate"],
    })

    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _fake_anthropic_response(fake_payload)
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    v = claude_verdict(_input(old_wins=2, new_wins=12, draws=0, total=14), AnalystConfig())
    assert v.source == "claude"
    assert v.label == "kept"
    assert v.confidence == 0.92
    assert "finishers" in str(v.lessons)
    # monkeypatch.setitem auto-cleans up; no manual pop needed.


def test_claude_verdict_normalizes_invalid_label(monkeypatch):
    """Bad label from the model gets coerced to 'neutral' rather than crashing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    fake_payload = json.dumps({"label": "garbage", "confidence": 0.5, "reasoning": "x"})

    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _fake_anthropic_response(fake_payload)
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    v = claude_verdict(_input(), AnalystConfig())
    assert v.label == "neutral"
    # monkeypatch.setitem auto-cleans up; no manual pop needed.


def test_claude_verdict_handles_empty_response(monkeypatch):
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
    # Empty response now raises NotImplementedError (not RuntimeError) so the
    # analyze() router — which only catches NotImplementedError — degrades to
    # the heuristic verdict instead of crashing the pipeline.
    with pytest.raises(NotImplementedError, match="empty response"):
        claude_verdict(_input(), AnalystConfig())
    # monkeypatch.setitem auto-cleans up; no manual pop needed.


def test_claude_verdict_tolerates_code_fenced_json(monkeypatch):
    """Model output wrapped in ```json ... ``` must still parse (not crash)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    fenced = ('```json\n{"label": "kept", "confidence": 0.8, '
              '"reasoning": "x", "lessons": []}\n```')

    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _fake_anthropic_response(fenced)
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    v = claude_verdict(_input(), AnalystConfig())
    assert v.label == "kept" and v.confidence == 0.8


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


def test_claude_verdict_unparseable_raises_llm_json_error(monkeypatch):
    """Non-JSON prose => LLMJsonError (NOT NotImplementedError).

    The distinction is deliberate: NotImplementedError means "backend
    not wired" (silent fall-through in analyze()); a parse failure means
    the backend responded with garbage, which analyze() catches with a
    LOUD warning before degrading to the heuristic."""
    from commander_builder._llm_json import LLMJsonError
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _mock_claude_sdk(monkeypatch, "I think this looks fine!")
    with pytest.raises(LLMJsonError, match="claude_verdict"):
        claude_verdict(_input(), AnalystConfig())


def test_claude_verdict_parses_prose_then_fenced_json(monkeypatch):
    """Prose preamble BEFORE a ```json fence must still parse — the old
    startswith-``` strip missed this shape entirely."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _mock_claude_sdk(
        monkeypatch,
        'Looking at this sim, here is my verdict:\n```json\n'
        '{"label": "kept", "confidence": 0.8, "reasoning": "x", "lessons": []}'
        '\n```\nLet me know if you need more detail.',
    )
    v = claude_verdict(_input(), AnalystConfig())
    assert v.label == "kept" and v.confidence == 0.8


def test_claude_verdict_truncated_json_raises_llm_json_error(monkeypatch):
    """max_tokens truncation (object never closes) => specific LLMJsonError
    quoting the response, not a crash or a silent NotImplementedError."""
    from commander_builder._llm_json import LLMJsonError
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _mock_claude_sdk(
        monkeypatch, '{"label": "kept", "confidence": 0.8, "reasoning": "the new'
    )
    with pytest.raises(LLMJsonError, match="truncated"):
        claude_verdict(_input(), AnalystConfig())


def test_analyze_degrades_to_heuristic_on_garbage_claude_response(
        monkeypatch, capsys):
    """Wired Claude backend returns unparseable prose: analyze() must NOT
    crash the iteration loop — it warns loudly and returns the heuristic
    verdict. (Previously the router only caught NotImplementedError, so
    any parse failure escaped and killed the whole run.)"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _mock_claude_sdk(monkeypatch, "Sorry, I cannot produce JSON today.")

    # Noise band: heuristic confidence is low → router escalates to claude.
    v = analyze(
        _input(old_wins=5, new_wins=6, draws=0, total=11),
        config=AnalystConfig(use_claude=True),
    )
    assert v.source == "heuristic"
    assert v.label == "neutral"
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "claude_verdict failed" in captured.out


def test_analyze_degrades_to_heuristic_on_claude_api_error(
        monkeypatch, capsys):
    """A wired backend that errors at call time (rate limit, outage) must
    also degrade to the heuristic with a loud warning, not crash."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")

    def boom(input_, config):
        raise RuntimeError("simulated API outage")
    monkeypatch.setattr("commander_builder.analyst.claude_verdict", boom)

    v = analyze(
        _input(old_wins=5, new_wins=6, draws=0, total=11),
        config=AnalystConfig(use_claude=True),
    )
    assert v.source == "heuristic"
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "simulated API outage" in captured.out


def test_claude_verdict_non_numeric_confidence_defaults(monkeypatch):
    """A model that writes confidence: "high" must not crash with ValueError."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    payload = '{"label": "kept", "confidence": "high", "reasoning": "x"}'

    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _fake_anthropic_response(payload)
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    v = claude_verdict(_input(), AnalystConfig())
    assert v.label == "kept" and v.confidence == 0.5  # defaulted


# --- ollama_verdict success path (mocked HTTP) -----------------------------

class _FakeUrlOpenResponse:
    def __init__(self, body: bytes):
        self._body = body
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_ollama_verdict_parses_daemon_response(monkeypatch):
    inner = json.dumps({
        "label": "reverted",
        "confidence": 0.8,
        "reasoning": "lost 3-9",
        "lessons": ["cuts removed too much defense"],
    })
    payload = json.dumps({"response": inner}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        return _FakeUrlOpenResponse(payload)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    v = ollama_verdict(
        _input(old_wins=9, new_wins=3, draws=0, total=12),
        AnalystConfig(),
    )
    assert v.source == "ollama"
    assert v.label == "reverted"
    assert v.confidence == 0.8


def test_ollama_verdict_falls_back_when_daemon_unreachable(monkeypatch):
    import urllib.error

    def network_down(req, timeout=None):
        raise urllib.error.URLError("no daemon")
    monkeypatch.setattr("urllib.request.urlopen", network_down)

    with pytest.raises(NotImplementedError, match="Ollama daemon not reachable"):
        ollama_verdict(_input(), AnalystConfig())


def test_ollama_verdict_normalizes_invalid_label(monkeypatch):
    inner = json.dumps({"label": "garbage", "confidence": 0.5, "reasoning": "x"})
    payload = json.dumps({"response": inner}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        return _FakeUrlOpenResponse(payload)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    v = ollama_verdict(_input(), AnalystConfig())
    assert v.label == "neutral"


# --- analyze() router with real backends mocked ----------------------------

def test_analyze_uses_claude_when_heuristic_uncertain(monkeypatch):
    """Noise-band heuristic (low confidence) → router escalates to claude."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    fake_payload = json.dumps({
        "label": "kept", "confidence": 0.7,
        "reasoning": "from claude", "lessons": [],
    })

    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _fake_anthropic_response(fake_payload)
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    # Noise band: heuristic confidence is low → escalate.
    v = analyze(
        _input(old_wins=5, new_wins=6, draws=0, total=11),
        config=AnalystConfig(use_claude=True),
    )
    assert v.source == "claude"
    # monkeypatch.setitem auto-cleans up; no manual pop needed.

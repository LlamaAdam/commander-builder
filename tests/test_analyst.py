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


# --- binomial_two_sided_p --------------------------------------------------

def test_binomial_p_even_split_is_one():
    from commander_builder.analyst import binomial_two_sided_p
    assert binomial_two_sided_p(10, 20) == 1.0


def test_binomial_p_values_match_exact_tail_sums():
    from commander_builder.analyst import binomial_two_sided_p
    # 12-8 over 20 decisive: p ~= 0.503 — a coin does this half the time.
    assert abs(binomial_two_sided_p(12, 20) - 0.50344) < 1e-4
    # 16-4 over 20 decisive: p ~= 0.0118 — real signal.
    assert abs(binomial_two_sided_p(16, 20) - 0.011818) < 1e-5


def test_binomial_p_symmetric_and_degenerate():
    from commander_builder.analyst import binomial_two_sided_p
    assert binomial_two_sided_p(16, 20) == binomial_two_sided_p(4, 20)
    assert binomial_two_sided_p(0, 0) == 1.0  # no evidence, never significant


# --- heuristic_verdict -----------------------------------------------------

def test_heuristic_kept_when_strong_improvement():
    # 16-4 over 20 head-to-head decisive: exact binomial p ~= 0.012 < 0.05.
    v = heuristic_verdict(_input(old_wins=4, new_wins=16, draws=0, total=20), AnalystConfig())
    assert v.label == "kept"
    assert v.confidence >= 0.75
    assert "16-4" in v.reasoning


def test_heuristic_reverted_when_strong_regression():
    v = heuristic_verdict(_input(old_wins=16, new_wins=4, draws=0, total=20), AnalystConfig())
    assert v.label == "reverted"
    assert v.confidence >= 0.75


def test_heuristic_neutral_when_within_noise():
    v = heuristic_verdict(_input(old_wins=5, new_wins=6, draws=0, total=11), AnalystConfig())
    assert v.label == "neutral"
    assert v.confidence < 0.75


def test_heuristic_12_8_over_20_is_not_strong():
    """Sample-size awareness (2026-08-14): under the null, pair-decisive
    wins split ~Binomial(n, 0.5) — with 20 decisive P(|new-old| >= 4)
    ~= 0.50, so the old margin_strong_threshold=4 rule blessed half of
    all NEUTRAL swaps with a confident verdict. 12-8 must be neutral."""
    v = heuristic_verdict(_input(old_wins=8, new_wins=12, draws=0, total=20), AnalystConfig())
    assert v.label == "neutral"
    assert v.confidence < 0.75


def test_heuristic_16_4_over_20_is_strong():
    """Same decisive count as above, genuinely lopsided split (p ~= 0.012)
    → strong kept with router-short-circuiting confidence."""
    v = heuristic_verdict(_input(old_wins=4, new_wins=16, draws=0, total=20), AnalystConfig())
    assert v.label == "kept"
    assert v.confidence >= 0.75


def test_heuristic_uses_head_to_head_decisive_not_total_minus_draws():
    """FIX 1 regression test: in a 4-player pod the filler seats win about
    half the games. Here 20 games completed with 0 draws but the A/B pair
    won only 3 between them (fillers took 17). The old
    ``decisive = total - draws`` computed 20 and passed the 8-game gate;
    head-to-head decisive is 3 → the verdict must be the inconclusive
    neutral, not a margin call on filler noise."""
    v = heuristic_verdict(_input(old_wins=2, new_wins=1, draws=0, total=20), AnalystConfig())
    assert v.label == "neutral"
    assert v.confidence == 0.3
    assert "Inconclusive" in v.reasoning
    assert "3/20" in v.reasoning


def test_heuristic_inconclusive_when_too_many_draws():
    # 18 of 20 games drew (matches the real Hakbal-vs-Hash smoke test).
    v = heuristic_verdict(_input(old_wins=1, new_wins=1, draws=18, total=20), AnalystConfig())
    assert v.label == "neutral"
    assert "Inconclusive" in v.reasoning
    assert any("decks_drew_too_often" in lesson for lesson in v.lessons)


def test_heuristic_inputs_lessons_for_kept():
    # 16-4 over 20 decisive: p ~= 0.012 → kept. (20 decisive: the aligned
    # MIN_DECISIVE_GAMES_FOR_VERDICT floor — 14 would now be inconclusive.)
    v = heuristic_verdict(_input(old_wins=4, new_wins=16, draws=0, total=20), AnalystConfig())
    assert any("swap_kept" in lesson for lesson in v.lessons)


def test_heuristic_inputs_lessons_for_reverted():
    v = heuristic_verdict(_input(old_wins=16, new_wins=4, draws=0, total=20), AnalystConfig())
    assert any("swap_reverted" in lesson for lesson in v.lessons)


def test_min_decisive_floor_aligned_with_proposer_sim():
    """Fix 3 (2026-08-16): the analyst's decisive-games floor must be THE
    SAME constant _proposer_sim gates on — the old AnalystConfig default
    of 8 let the analyst render confident verdicts on samples the
    auto-curate path correctly called 'inconclusive'."""
    from commander_builder import _proposer_sim
    from commander_builder.analyst import MIN_DECISIVE_GAMES_FOR_VERDICT
    assert MIN_DECISIVE_GAMES_FOR_VERDICT == 20
    assert AnalystConfig().min_decisive_games == MIN_DECISIVE_GAMES_FOR_VERDICT
    assert (_proposer_sim.MIN_DECISIVE_GAMES_FOR_VERDICT
            == MIN_DECISIVE_GAMES_FOR_VERDICT)


def test_heuristic_inconclusive_below_aligned_floor():
    """14 decisive games (a lopsided 12-2, p ~= 0.013) sits below the
    aligned 20-decisive floor: the verdict must be the inconclusive
    neutral, not a confident kept — matching _verdict_from_ab, which
    returns 'inconclusive' for the same outcome."""
    v = heuristic_verdict(_input(old_wins=2, new_wins=12, draws=0, total=14), AnalystConfig())
    assert v.label == "neutral"
    assert v.confidence == 0.3
    assert "Inconclusive" in v.reasoning


# --- analyze() router ------------------------------------------------------

def test_analyze_returns_heuristic_when_strong_signal():
    """High-confidence heuristic short-circuits — no LLM escalation needed."""
    v = analyze(_input(old_wins=4, new_wins=16, draws=0, total=20))
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


# --- LLM-facing sim summaries carry signed margin / winner / draws ---------

def _regression_input() -> AnalystInput:
    """A 6-game REGRESSION: old 13, new 7, 2 draws. The persisted report
    dict carries margin=6 (abs) — exactly the shape that used to make a
    model write confidently wrong 'improvement' lessons."""
    return AnalystInput(
        deck_name="test.dck",
        bracket=3,
        audit_manifest={"added": ["A"], "removed": ["B"], "rationale": "x"},
        sim_report={
            "total_games": 30,
            "draws": 2,
            "margin": 6,          # ComparisonReport.margin is abs()
            "winner": "old",
            "old_stats": {"wins": 13},
            "new_stats": {"wins": 7},
        },
    )


def test_claude_summary_contains_signed_margin_winner_draws(monkeypatch):
    """FIX 3 regression test: the sim summary sent to Claude must carry the
    SIGNED margin (-6 here), the winner, draws, and the head-to-head
    decisive count — never the bare absolute margin."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    captured = {}

    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    captured.update(kw)
                    return _fake_anthropic_response(json.dumps({
                        "label": "reverted", "confidence": 0.9,
                        "reasoning": "x", "lessons": [],
                    }))
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    claude_verdict(_regression_input(), AnalystConfig())
    payload = json.loads(captured["messages"][0]["content"])
    summary = payload["sim_summary"]
    assert summary["signed_margin"] == -6
    assert summary["winner"] == "old"
    assert summary["draws"] == 2
    assert summary["h2h_decisive"] == 20
    # The ambiguous absolute margin is no longer forwarded to the model.
    assert "margin" not in summary


def test_claude_prompt_describes_signed_margin_not_fixed_threshold():
    """The verdict system prompt's kept/reverted criteria must speak the
    signed-margin / significance language, not the retired 'margin >= 4
    wins / 20 games' absolute rule."""
    from commander_builder.analyst import _CLAUDE_VERDICT_SYSTEM
    assert "signed_margin" in _CLAUDE_VERDICT_SYSTEM
    assert "h2h_decisive" in _CLAUDE_VERDICT_SYSTEM
    assert "margin >=4" not in _CLAUDE_VERDICT_SYSTEM


def test_ollama_summary_contains_signed_margin_winner_draws(monkeypatch):
    """Same FIX 3 guarantee for the Ollama path — its old summary omitted
    the winner entirely and forwarded the absolute margin."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        inner = json.dumps({"label": "reverted", "confidence": 0.8,
                            "reasoning": "x", "lessons": []})
        return _FakeUrlOpenResponse(
            json.dumps({"response": inner}).encode("utf-8"))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ollama_verdict(_regression_input(), AnalystConfig())
    prompt = captured["body"]["prompt"]
    assert '"signed_margin": -6' in prompt
    assert '"winner": "old"' in prompt
    assert '"draws": 2' in prompt
    assert '"h2h_decisive": 20' in prompt


def test_summary_derives_winner_when_report_omits_it(monkeypatch):
    """AB-shaped sim_report dicts have no 'winner' key; the summary must
    derive it from the win counts rather than sending null."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        inner = json.dumps({"label": "kept", "confidence": 0.8,
                            "reasoning": "x", "lessons": []})
        return _FakeUrlOpenResponse(
            json.dumps({"response": inner}).encode("utf-8"))
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ollama_verdict(_input(old_wins=3, new_wins=9, draws=1, total=20),
                   AnalystConfig())
    assert '"winner": "new"' in captured["body"]["prompt"]


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

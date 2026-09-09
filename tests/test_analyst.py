"""analyst.py tests — heuristic verdict logic + router behavior + LLM backends.

The Claude backend is mocked (an anthropic SDK stand-in) so the suite stays
offline. Stub fallback paths are also verified — the router catches
NotImplementedError and degrades to the heuristic.

The Ollama VERDICT rung was RETIRED by decision A4 (2026-08-27), the same
way `proposer.ollama_propose` was on 2026-08-17: a verdict is open-ended
synthesis, not the narrow supplied-evidence classification a small local
model can do, and nothing in `src/` ever set `use_ollama` so it never ran.
What is pinned here is that the retirement is inert AND loud —
`use_ollama=True` still constructs, makes no network call, and says where
local models went — and that the router does not swallow the retirement
note in its quiet NotImplementedError arm. The live local-model tier has
its own tests in `tests/test_local_model.py`.
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
    # 10-11 over 21 decisive: AT the trustworthy floor, within noise.
    # (Re-pinned 2026-09-03, R3 C-01: the old 5-6 over 11 sat BELOW the
    # 20-decisive floor and only read "neutral" because the floor branch
    # mislabeled sub-floor sims; that case is now 'inconclusive'.)
    v = heuristic_verdict(_input(old_wins=10, new_wins=11, draws=0, total=21), AnalystConfig())
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
    head-to-head decisive is 3 → the verdict must be 'inconclusive', not
    a margin call on filler noise. (Re-pinned 2026-09-03, R3 C-01: the
    floor branch used to say "neutral", the schema's word for a
    TRUSTWORTHY near-tie.)"""
    v = heuristic_verdict(_input(old_wins=2, new_wins=1, draws=0, total=20), AnalystConfig())
    assert v.label == "inconclusive"
    assert v.confidence == 0.3
    assert "Inconclusive" in v.reasoning
    assert "3/20" in v.reasoning


def test_heuristic_inconclusive_when_too_many_draws():
    # 18 of 20 games drew (matches the real Hakbal-vs-Hash smoke test).
    v = heuristic_verdict(_input(old_wins=1, new_wins=1, draws=18, total=20), AnalystConfig())
    # R3 C-01 (2026-09-03): 'inconclusive', the knowledge_log label for
    # "measured, not decided" — no longer a trustworthy-looking 'neutral'.
    assert v.label == "inconclusive"
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
    aligned 20-decisive floor: the verdict must be 'inconclusive', not a
    confident kept — matching _verdict_from_ab, which returns
    'inconclusive' for the same outcome (and, since 2026-09-03 / R3
    C-01, the same LABEL: the analyst used to write 'neutral' here)."""
    v = heuristic_verdict(_input(old_wins=2, new_wins=12, draws=0, total=14), AnalystConfig())
    assert v.label == "inconclusive"
    assert v.confidence == 0.3
    assert "Inconclusive" in v.reasoning


# --- analyze() router ------------------------------------------------------

def test_analyze_returns_heuristic_when_strong_signal():
    """High-confidence heuristic short-circuits — no LLM escalation needed."""
    v = analyze(_input(old_wins=4, new_wins=16, draws=0, total=20))
    assert v.source == "heuristic"
    assert v.label == "kept"


def test_analyze_falls_back_to_heuristic_when_llm_unwired(monkeypatch):
    """Even with use_claude=True, claude_verdict raises NotImplementedError
    when unwired (no API key); the router falls back to the heuristic. The
    retired use_ollama rung must not change that — and must not reach the
    network, so urlopen is booby-trapped rather than merely stubbed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    def no_network(*a, **kw):
        raise AssertionError("analyze() must not open a socket here")
    monkeypatch.setattr("urllib.request.urlopen", no_network)

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


def test_ollama_verdict_is_retired():
    """Decision A4: the local-verdict path is retired, not merely unwired.
    It raises without touching the network and the message points at the
    replacement (`local_model`) and the reason verdicts stay on Claude."""
    def explode(*a, **kw):
        raise AssertionError("retired ollama verdict path made a network call")
    import urllib.request
    original = urllib.request.urlopen
    urllib.request.urlopen = explode
    try:
        with pytest.raises(NotImplementedError) as exc_info:
            ollama_verdict(_input(), AnalystConfig())
    finally:
        urllib.request.urlopen = original
    message = str(exc_info.value)
    assert "retired" in message
    assert "local_model" in message
    assert "COMMANDER_BUILDER_LOCAL_MODEL" in message
    # It says WHY verdicts stay on Claude, not just that the flag is dead.
    assert "Claude" in message


def test_ollama_verdict_retired_note_matches_the_exception():
    """One wording, one source of truth: the note the router prints and the
    note the stub raises are the same string (the proposer retirement's
    shape)."""
    from commander_builder.analyst import OLLAMA_VERDICT_RETIRED_NOTE
    with pytest.raises(NotImplementedError) as exc_info:
        ollama_verdict(_input(), AnalystConfig())
    assert str(exc_info.value) == OLLAMA_VERDICT_RETIRED_NOTE


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
    # (10-11 over 21 decisive since 2026-09-03 / R3 C-01: the old 5-6
    # over 11 was BELOW the decisive floor and is now 'inconclusive',
    # which also escalates but is not the "within noise" case this
    # test describes.)
    v = analyze(
        _input(old_wins=10, new_wins=11, draws=0, total=21),
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


# --- The retired rung inside analyze() -------------------------------------

def test_analyze_with_use_ollama_makes_no_network_call(monkeypatch, capsys):
    """A retired flag must be inert, not silently inert. `use_ollama=True`
    still constructs (config back-compat), reaches NO daemon at all, and
    prints the retirement note before the router continues down the ladder
    to the heuristic."""
    def explode(*a, **kw):
        raise AssertionError("retired ollama verdict path made a network call")
    monkeypatch.setattr("urllib.request.urlopen", explode)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # Noise band: heuristic confidence is 0.4, below the 0.75 bar, so the
    # router DOES reach the retired rung rather than short-circuiting.
    v = analyze(_input(old_wins=5, new_wins=6, draws=0, total=11),
                config=AnalystConfig(use_ollama=True))
    assert v.source == "heuristic"
    printed = capsys.readouterr().out
    assert "retired" in printed
    assert "local_model" in printed


def test_analyze_does_not_swallow_the_retirement_note(monkeypatch, capsys):
    """The regression this retirement's SHAPE exists to prevent.

    The router's NotImplementedError arm is a quiet fall-through by
    contract ("backend not wired" is normal). If the retired stub were
    still CALLED from inside that try/except, its NotImplementedError
    would be swallowed whole and the operator would see nothing at all —
    the same silent degrade the retirement removes. So `analyze()` must
    never invoke `ollama_verdict`, and must print instead."""
    calls = []

    def spy(input_, config):
        calls.append(input_)
        raise NotImplementedError("must not be called from analyze()")
    monkeypatch.setattr("commander_builder.analyst.ollama_verdict", spy)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    analyze(_input(old_wins=5, new_wins=6, draws=0, total=11),
            config=AnalystConfig(use_ollama=True))
    assert calls == []
    assert "retired" in capsys.readouterr().out


def test_analyze_retired_rung_still_escalates_to_claude(monkeypatch, capsys):
    """The retired rung is a print, not a `return` and not a `raise`: a
    config with BOTH flags set must still reach Claude."""
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

    v = analyze(_input(old_wins=5, new_wins=6, draws=0, total=11),
                config=AnalystConfig(use_claude=True, use_ollama=True))
    assert v.source == "claude"
    assert "retired" in capsys.readouterr().out


def test_retired_config_fields_still_construct():
    """Back-compat: an out-of-tree caller that still passes the retired
    knobs must not get a TypeError."""
    config = AnalystConfig(
        use_ollama=True,
        ollama_model="llama3.2:3b",
        ollama_url="http://localhost:11434/api/generate",
    )
    assert config.use_ollama is True
    assert config.ollama_model == "llama3.2:3b"


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


def test_summary_derives_winner_when_report_omits_it():
    """AB-shaped sim_report dicts have no 'winner' key; the summary must
    derive it from the win counts rather than emitting null.

    Called through ``_summarize_h2h`` directly since the retirement of
    ``ollama_verdict`` (2026-08-27) left ``claude_verdict`` as its only
    caller — and that path is already covered above. Pinning the helper
    itself keeps the derive-the-winner rule tested without a second SDK
    stub."""
    from commander_builder.analyst import _summarize_h2h

    summary = _summarize_h2h({
        "total_games": 20, "draws": 1,
        "old_stats": {"wins": 3}, "new_stats": {"wins": 9},
    })
    assert summary["winner"] == "new"
    assert summary["signed_margin"] == 6
    assert summary["h2h_decisive"] == 12

    # Symmetric, and a genuine tie is reported as one rather than as "new".
    assert _summarize_h2h({
        "old_stats": {"wins": 5}, "new_stats": {"wins": 5},
    })["winner"] == "tie"


def test_summary_never_forwards_the_ambiguous_absolute_margin():
    """``ComparisonReport.margin`` is ``abs(new - old)``: a model reading
    'margin: 6' on a 6-game REGRESSION writes confidently wrong lessons.
    The helper must drop it even when the report carries it."""
    from commander_builder.analyst import _summarize_h2h

    summary = _summarize_h2h({
        "total_games": 30, "draws": 2, "margin": 6, "winner": "old",
        "old_stats": {"wins": 13}, "new_stats": {"wins": 7},
    })
    assert "margin" not in summary
    assert summary["signed_margin"] == -6


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

"""Lift Web (ManaFoundry parity) — co-occurrence lift over the
harvested corpus.

Everything here is offline. The corpus is synthetic .dck files in
tmp_path with HAND-COMPUTED expected lift values (each test spells out
its arithmetic), so a formula regression fails loudly with the exact
numbers in the assertion.

Layout:
- lift math + corpus rules + cache: pure lift_analysis unit tests
  (fast lane).
- dashboard payload / route: direct payload calls + one Flask client
  probe (fast lane).
- advisor integration: full advise() pipeline with stubbed EDHREC
  (slow lane, matching the convention in test_collection.py /
  test_improvement_advisor.py).
- CLI --show-lift: main() with a stubbed advise (fast lane, matching
  the CLI-threading tests in test_collection.py).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from commander_builder import lift_analysis as la
from commander_builder.lift_analysis import (
    band_for_bracket,
    bracket_from_filename,
    build_corpus,
    compute_lift_matrix,
    format_lift_report,
    lift_candidates,
    lift_picks_payload,
    lift_recommendations,
    lift_value,
    load_or_build_matrix,
    top_deck_pairs,
)


@pytest.fixture(autouse=True)
def _isolate_lift_cache(tmp_path, monkeypatch):
    """Redirect the module-level default cache path to a per-test temp
    file so tests never read/write the repo's real
    ``.cache/lift_matrix.v1.json``. Works because
    ``load_or_build_matrix`` resolves ``cache_path=None`` against the
    module attribute at CALL time (the DEFAULT_DB_PATH lesson)."""
    monkeypatch.setattr(
        la, "DEFAULT_CACHE_PATH", tmp_path / "_lift_cache.json",
    )


def _dck(deck_dir: Path, filename: str, cards: list[str],
         commander: str | None = None) -> Path:
    deck_dir.mkdir(parents=True, exist_ok=True)
    body = []
    if commander:
        body += ["[Commander]", f"1 {commander}"]
    body.append("[Main]")
    body += [f"1 {c}" for c in cards]
    p = deck_dir / filename
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


def _corpus_dir(tmp_path: Path, decks: list[list[str]],
                bracket: int = 3) -> Path:
    """Write ``decks`` as corpus-eligible files 'Deck NN [B<n>].dck'."""
    d = tmp_path / "decks"
    for i, cards in enumerate(decks):
        _dck(d, f"Deck {i:02d} [B{bracket}].dck", cards)
    return d


# ---------------------------------------------------------------------------
# Lift math — hand-computed exactness
# ---------------------------------------------------------------------------

def test_lift_math_exact(tmp_path):
    """N=12. A+B together in 3 decks; A alone in 1; B alone in 3;
    5 filler decks. cA=4, cB=6, co=3.
    lift = co*N / (cA*cB) = 3*12 / (4*6) = 36/24 = 1.5 exactly."""
    d = _corpus_dir(tmp_path, (
        [["Alpha Strike", "Beta Ray"]] * 3
        + [["Alpha Strike"]]
        + [["Beta Ray"]] * 3
        + [["Filler Card"]] * 5
    ))
    matrix = compute_lift_matrix(build_corpus(d))
    assert matrix["too_small"] is False
    assert matrix["n_decks"] == 12
    assert matrix["counts"]["alpha strike"] == 4
    assert matrix["counts"]["beta ray"] == 6
    assert lift_value(matrix, "alpha strike", "beta ray") == 1.5
    # Symmetric: argument order must not matter.
    assert lift_value(matrix, "beta ray", "alpha strike") == 1.5


def test_support_floor_drops_two_deck_pairs(tmp_path):
    """A pair seen in only 2 decks stays out of the matrix entirely —
    with co=1..2 the lift estimate is dominated by coincidence (see
    the module docstring's co=1 -> lift=N pathology)."""
    d = _corpus_dir(tmp_path, (
        [["Rare One", "Rare Two"]] * 2          # co = 2 < floor of 3
        + [["Rare One"]] + [["Rare Two"]]
        + [["Filler Card"]] * 8
    ))
    matrix = compute_lift_matrix(build_corpus(d))
    assert lift_value(matrix, "rare one", "rare two") is None
    # Not just unreported — genuinely absent from the sparse store.
    assert "rare two" not in matrix["pairs"].get("rare one", {})


def test_staples_and_basics_excluded_from_vocabulary(tmp_path):
    """Sol Ring / basics pair with everything (P ~ 1 -> lift ~ 1):
    pure noise, so they never enter the vocabulary at all."""
    d = _corpus_dir(tmp_path, [
        ["Sol Ring", "Forest", "Snow-Covered Island", "Real Card"],
    ] * 10)
    matrix = compute_lift_matrix(build_corpus(d))
    assert "real card" in matrix["counts"]
    assert "sol ring" not in matrix["counts"]
    assert "forest" not in matrix["counts"]
    assert "snow-covered island" not in matrix["counts"]


def test_corpus_excludes_user_and_control_keeps_ref(tmp_path):
    d = tmp_path / "decks"
    _dck(d, "[USER] Mine [B3].dck", ["User Card"])
    _dck(d, "[CONTROL] do-nothing calib1 [B3].dck", ["Control Card"])
    _dck(d, "[REF] mox Community Build [B3].dck", ["Ref Card"])
    _dck(d, "Pool Deck [B3].dck", ["Pool Card"])
    corpus = build_corpus(d)
    names = {c.filename for c in corpus}
    assert "[REF] mox Community Build [B3].dck" in names
    assert "Pool Deck [B3].dck" in names
    assert not any(n.startswith("[USER]") for n in names)
    assert not any(n.startswith("[CONTROL]") for n in names)


def test_commander_section_counts_toward_vocabulary(tmp_path):
    d = tmp_path / "decks"
    for i in range(10):
        _dck(d, f"Deck {i} [B3].dck", ["Main Card"],
             commander="Legendary Boss")
    matrix = compute_lift_matrix(build_corpus(d))
    assert matrix["counts"]["legendary boss"] == 10
    assert lift_value(matrix, "legendary boss", "main card") == 1.0


def test_bracket_tag_parsing():
    assert bracket_from_filename("Some Deck [B3].dck") == 3
    assert bracket_from_filename("[REF] mox Thing [B5].dck") == 5
    assert bracket_from_filename("No Tag.dck") is None
    assert bracket_from_filename("Bogus [B9].dck") is None
    assert band_for_bracket(1) == "B1-2"
    assert band_for_bracket(2) == "B1-2"
    assert band_for_bracket(3) == "B3"
    assert band_for_bracket(4) == "B4-5"
    assert band_for_bracket(5) == "B4-5"
    assert band_for_bracket(None) is None


# ---------------------------------------------------------------------------
# Small-corpus refusal + band fallback
# ---------------------------------------------------------------------------

def test_small_corpus_refusal(tmp_path):
    """9 decks < MIN_CORPUS_DECKS: every surface must say 'too small'
    rather than emit numerically meaningless lift values."""
    d = _corpus_dir(tmp_path, [["Card A", "Card B"]] * 9)
    matrix = compute_lift_matrix(build_corpus(d))
    assert matrix["too_small"] is True
    assert matrix["counts"] == {} and matrix["pairs"] == {}

    deck = _dck(tmp_path / "decks", "[USER] Me [B3].dck", ["Card A"])
    payload = lift_picks_payload(deck, d)
    assert payload["picks"] == []
    assert "corpus too small" in payload["reason"]
    assert lift_recommendations(deck, d, bracket=3) == []
    assert "Corpus too small" in format_lift_report(deck, d)


def test_band_matrix_present_only_when_populated(tmp_path):
    """12 B3 decks + 2 B4 decks: B3 band materializes (>= 10), B4-5
    stays out (2 < 10) and B4 queries fall back to overall."""
    d = _corpus_dir(tmp_path, [["Card A", "Card B"]] * 12, bracket=3)
    _dck(d, "Extra One [B4].dck", ["Card A", "Card C"])
    _dck(d, "Extra Two [B4].dck", ["Card A", "Card C"])
    matrix = compute_lift_matrix(build_corpus(d))
    assert set(matrix["bands"]) == {"B3"}
    assert matrix["bands"]["B3"]["n_decks"] == 12
    # B3 band section is used for bracket 3...
    section, band = la._section_for(matrix, 3)
    assert band == "B3" and section["n_decks"] == 12
    # ...brackets whose band is missing fall back to overall.
    section, band = la._section_for(matrix, 4)
    assert band == "overall" and section["n_decks"] == 14


def test_untagged_decks_count_overall_but_no_band(tmp_path):
    d = tmp_path / "decks"
    for i in range(12):
        _dck(d, f"Untagged {i}.dck", ["Card A"])
    matrix = compute_lift_matrix(build_corpus(d))
    assert matrix["n_decks"] == 12
    assert matrix["bands"] == {}


# ---------------------------------------------------------------------------
# Cache — hit + mtime invalidation
# ---------------------------------------------------------------------------

def test_cache_hit_and_mtime_invalidation(tmp_path, monkeypatch):
    d = _corpus_dir(tmp_path, [["Card A", "Card B"]] * 12)
    cache = tmp_path / "cache" / "lift.json"

    calls = {"n": 0}
    real_compute = la.compute_lift_matrix

    def counting_compute(corpus):
        calls["n"] += 1
        return real_compute(corpus)

    monkeypatch.setattr(la, "compute_lift_matrix", counting_compute)

    m1 = load_or_build_matrix(d, cache_path=cache)
    assert calls["n"] == 1
    assert cache.exists()

    # Warm cache: same fingerprint -> loaded, not recomputed.
    m2 = load_or_build_matrix(d, cache_path=cache)
    assert calls["n"] == 1
    assert m2 == m1

    # Touch one corpus file's mtime -> fingerprint changes -> rebuild.
    victim = next(d.glob("*.dck"))
    st = victim.stat()
    os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    load_or_build_matrix(d, cache_path=cache)
    assert calls["n"] == 2


def test_too_small_corpus_is_never_cached(tmp_path):
    d = _corpus_dir(tmp_path, [["Card A"]] * 3)
    cache = tmp_path / "cache" / "lift.json"
    matrix = load_or_build_matrix(d, cache_path=cache)
    assert matrix["too_small"] is True
    assert not cache.exists()


def test_corrupt_cache_falls_through_to_rebuild(tmp_path):
    d = _corpus_dir(tmp_path, [["Card A", "Card B"]] * 12)
    cache = tmp_path / "cache" / "lift.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{not json", encoding="utf-8")
    matrix = load_or_build_matrix(d, cache_path=cache)
    assert matrix["n_decks"] == 12


# ---------------------------------------------------------------------------
# Candidate ranking + rationale
# ---------------------------------------------------------------------------

def _ranking_corpus(tmp_path) -> tuple[Path, Path]:
    """N=12: 4x {Xeno Prototype, Delta One, Delta Two};
    1x {Delta One}; 2x {Delta Two}; 5x {Filler Card}.

    cX=4, cD1=5, cD2=6; co(X,D1)=4, co(X,D2)=4.
    lift(X,D1) = 4*12/(4*5) = 2.4;  lift(X,D2) = 4*12/(4*6) = 2.0.
    Candidate score for X vs a deck holding D1+D2:
    mean(2.4, 2.0) = 2.2 with 2 supporting pairs; best partner D1.
    """
    d = _corpus_dir(tmp_path, (
        [["Xeno Prototype", "Delta One", "Delta Two"]] * 4
        + [["Delta One"]]
        + [["Delta Two"]] * 2
        + [["Filler Card"]] * 5
    ))
    deck = _dck(tmp_path / "decks", "[USER] Me [B3].dck",
                ["Delta One", "Delta Two", "Unrelated Card"])
    return d, deck


def test_candidate_ranking_score_and_min_pairs(tmp_path):
    d, deck = _ranking_corpus(tmp_path)
    matrix = compute_lift_matrix(build_corpus(d))
    deck_keys = la.deck_keys_for_path(deck)
    cands = lift_candidates(matrix, deck_keys, bracket=3)
    by_name = {c["card"]: c for c in cands}
    assert "Xeno Prototype" in by_name
    x = by_name["Xeno Prototype"]
    assert x["score"] == 2.2          # mean(2.4, 2.0), hand-computed
    assert x["n_pairs"] == 2
    # Filler Card pairs with nothing in the deck -> never a candidate.
    assert "Filler Card" not in by_name
    # In-deck cards are never candidates.
    assert "Delta One" not in by_name and "Delta Two" not in by_name


def test_candidate_requires_two_supporting_pairs(tmp_path):
    """A candidate whose only above-chance partner is a single in-deck
    card is combo-piece territory, not deck fit — excluded."""
    d = _corpus_dir(tmp_path, (
        [["Solo Partner", "Solo Candidate"]] * 4   # only one deck link
        + [["Filler Card"]] * 8
    ))
    deck = _dck(tmp_path / "decks", "[USER] Me [B3].dck",
                ["Solo Partner", "Another Card"])
    matrix = compute_lift_matrix(build_corpus(d))
    cands = lift_candidates(matrix, la.deck_keys_for_path(deck))
    assert all(c["card"] != "Solo Candidate" for c in cands)


def test_rationale_string_format(tmp_path):
    d, deck = _ranking_corpus(tmp_path)
    matrix = compute_lift_matrix(build_corpus(d))
    cands = lift_candidates(matrix, la.deck_keys_for_path(deck))
    x = next(c for c in cands if c["card"] == "Xeno Prototype")
    # Best partner is Delta One (lift 2.4 > 2.0); co=4 of cX=4 decks.
    assert x["rationale"] == (
        "appears with Delta One in 4/4 harvested decks (lift 2.4)"
    )


def test_top_deck_pairs_reports_in_deck_synergies(tmp_path):
    d, _deck = _ranking_corpus(tmp_path)
    matrix = compute_lift_matrix(build_corpus(d))
    # Deck holding all three linked cards: strongest in-deck pair is
    # (Xeno, Delta One) at lift 2.4.
    deck = _dck(tmp_path / "decks", "[USER] Full [B3].dck",
                ["Xeno Prototype", "Delta One", "Delta Two"])
    pairs = top_deck_pairs(matrix, la.deck_keys_for_path(deck))
    assert pairs, "expected at least one above-floor in-deck pair"
    top = pairs[0]
    assert {top["card_a"], top["card_b"]} == {"Xeno Prototype", "Delta One"}
    assert top["lift"] == 2.4
    assert top["co"] == 4


# ---------------------------------------------------------------------------
# Dashboard payload + color-identity filter (fake resolver)
# ---------------------------------------------------------------------------

def test_payload_shape_and_ci_filter_pass(tmp_path):
    d, deck = _ranking_corpus(tmp_path)
    payload = lift_picks_payload(
        deck, d, bracket=3,
        resolve_ci=lambda p: "G",
        ci_filter=lambda names, ci: (list(names), []),  # keep everything
    )
    assert set(payload) == {"corpus_size", "band", "picks", "reason"}
    assert payload["corpus_size"] == 12
    assert payload["band"] == "B3"
    assert payload["reason"] is None
    pick = payload["picks"][0]
    assert set(pick) == {"card", "score", "n_pairs", "rationale"}
    assert pick["card"] == "Xeno Prototype"


def test_payload_ci_filter_drops_offcolor_candidates(tmp_path):
    d, deck = _ranking_corpus(tmp_path)
    payload = lift_picks_payload(
        deck, d, bracket=3,
        resolve_ci=lambda p: "G",
        ci_filter=lambda names, ci: ([], list(names)),  # drop everything
    )
    assert payload["picks"] == []
    assert payload["reason"] == "no candidates cleared the lift bar"


def test_payload_skips_ci_filter_when_identity_unresolvable(tmp_path):
    """resolve_ci -> None means 'couldn't resolve the commander' (the
    enforce_color_identity None contract): filter must be skipped
    entirely, not applied against a phantom colorless identity."""
    d, deck = _ranking_corpus(tmp_path)

    def exploding_filter(names, ci):  # pragma: no cover - must not run
        raise AssertionError("CI filter must be skipped when CI is None")

    payload = lift_picks_payload(
        deck, d, bracket=3,
        resolve_ci=lambda p: None,
        ci_filter=exploding_filter,
    )
    assert payload["picks"], "unresolvable CI must not empty the picks"


def test_dashboard_route_attaches_lift_picks_fail_quiet(tmp_path, monkeypatch):
    """/api/dashboard always carries a lift_picks key — here with a
    2-deck dir the corpus is too small, so picks=[] with a reason
    (never a 500)."""
    from commander_builder.web.app import create_app

    deck_dir = tmp_path / "decks"
    _dck(deck_dir, "Alpha [B3].dck", ["Card A"], commander="Boss A")
    _dck(deck_dir, "Bravo [B3].dck", ["Card B"], commander="Boss B")
    # Offline stubs, mirroring the test_web_app client fixture.
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card", lambda n: None,
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda n, **kw: None,
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card_prints",
        lambda n, **kw: None,
    )
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    body = app.test_client().get("/api/dashboard?deck=Alpha%20%5BB3%5D").get_json()
    assert "lift_picks" in body
    assert body["lift_picks"]["picks"] == []
    assert "corpus too small" in body["lift_picks"]["reason"]


# ---------------------------------------------------------------------------
# Advisor integration (slow lane, like the rest of the advise suite)
# ---------------------------------------------------------------------------

@pytest.fixture
def _offline_advisor(monkeypatch):
    """Stub every network-adjacent advise() seam (same pattern as
    test_collection.py's fixture of the same name)."""
    from commander_builder.edhrec_client import CardEntry, CommanderPage
    fake_page = CommanderPage(
        commander_name="Test Commander",
        slug="test-commander",
        fetched_at="2026-07-21T00:00:00",
        top_cards=[CardEntry(name="Rhystic Study", inclusion_pct=80.0)],
        high_synergy_cards=[],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: fake_page,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_tag_page",
        lambda slug, **kw: None,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_average_deck",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name, **kw: None,
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )


def _advisor_corpus(tmp_path) -> tuple[Path, Path, Path]:
    """deck_dir with a 12-deck corpus where 'Xeno Prototype' pairs with
    the user deck's Delta One + Delta Two at lift 3.0 each:
    4x {X, D1, D2} + 8x {Filler} -> cX=cD1=cD2=4, co=4,
    lift = 4*12/(4*4) = 3.0; score mean(3.0, 3.0) = 3.0 >= 2.0."""
    deck_dir = tmp_path / "decks"
    match_dir = tmp_path / "matches"
    match_dir.mkdir()
    for i in range(4):
        _dck(deck_dir, f"Corpus {i} [B3].dck",
             ["Xeno Prototype", "Delta One", "Delta Two"])
    for i in range(8):
        _dck(deck_dir, f"Filler {i} [B3].dck", ["Filler Card"])
    user = _dck(deck_dir, "[USER] Test Commander [B3].dck",
                ["Delta One", "Delta Two", "Old Card"],
                commander="Test Commander")
    return user, deck_dir, match_dir


@pytest.mark.slow
def test_advise_appends_lift_adds_with_source_label(
    tmp_path, _offline_advisor,
):
    from commander_builder.improvement_advisor import advise
    user, deck_dir, match_dir = _advisor_corpus(tmp_path)
    report = advise(user, bracket=3, deck_dir=deck_dir, match_dir=match_dir)
    adds = {r.card: r for r in report.recommendations if r.action == "add"}
    assert "Xeno Prototype" in adds
    rec = adds["Xeno Prototype"]
    assert rec.evidence["source"] == "lift"
    assert rec.evidence["lift_score"] == 3.0
    assert rec.evidence["supporting_pairs"] == 2
    assert "appears with" in rec.reason and "lift" in rec.reason
    # The primary (EDHREC) picks still surface — lift is additive.
    assert "Rhystic Study" in adds


@pytest.mark.slow
def test_advise_lift_dedupes_against_primary_source(
    tmp_path, _offline_advisor, monkeypatch,
):
    """When the EDHREC heuristic already recommends the lift pick, the
    lift copy is deduped away (established sources win — lift recs are
    appended last)."""
    from commander_builder.edhrec_client import CardEntry, CommanderPage
    from commander_builder.improvement_advisor import advise
    user, deck_dir, match_dir = _advisor_corpus(tmp_path)
    page = CommanderPage(
        commander_name="Test Commander", slug="test-commander",
        fetched_at="2026-07-21T00:00:00",
        top_cards=[CardEntry(name="Xeno Prototype", inclusion_pct=80.0)],
        high_synergy_cards=[],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: page,
    )
    report = advise(user, bracket=3, deck_dir=deck_dir, match_dir=match_dir)
    xeno = [r for r in report.recommendations
            if r.action == "add" and r.card == "Xeno Prototype"]
    assert len(xeno) == 1
    assert xeno[0].evidence.get("source") != "lift"


@pytest.mark.slow
def test_advise_lift_adds_pass_through_ownership_filter(
    tmp_path, _offline_advisor,
):
    """Lift adds run through the same _filter_for_ownership stage as
    every other source: owned-only mode drops an unowned lift pick and
    discloses it."""
    from commander_builder import collection
    from commander_builder.improvement_advisor import advise
    user, deck_dir, match_dir = _advisor_corpus(tmp_path)
    coll = collection.collection_path()
    coll.parent.mkdir(parents=True, exist_ok=True)
    coll.write_text("Rhystic Study\n", encoding="utf-8")  # no Xeno

    report = advise(user, bracket=3, deck_dir=deck_dir,
                    match_dir=match_dir, owned_only=True)
    adds = [r.card for r in report.recommendations if r.action == "add"]
    assert "Xeno Prototype" not in adds
    assert {"card": "Xeno Prototype", "reason": "not owned"} in (
        report.skipped_for_ownership
    )


# ---------------------------------------------------------------------------
# CLI --show-lift (advise stubbed; fast lane)
# ---------------------------------------------------------------------------

def _stub_advise(monkeypatch):
    from commander_builder.improvement_advisor import AdviceReport

    def fake_advise(deck_path, bracket, **kwargs):
        return AdviceReport(
            deck_filename=Path(deck_path).name, deck_id=None,
            bracket=bracket, commander_names=["Test Commander"],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )


def test_cli_show_lift_prints_pairs_and_candidates(
    tmp_path, monkeypatch, capsys,
):
    from commander_builder import improvement_advisor as ia
    _stub_advise(monkeypatch)
    # Point the CLI's deck dir at a corpus-bearing tmp dir (main()
    # references the module attribute at call time). _ranking_corpus
    # already plants "[USER] Me [B3].dck" inside the same dir.
    deck_dir, user = _ranking_corpus(tmp_path)
    monkeypatch.setattr(ia, "DECK_DIR", deck_dir)

    rc = ia.main(["--user", user.name, "--bracket", "3", "--show-lift"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Lift analysis" in out
    assert "Top in-deck pairs" in out
    assert "Top candidate adds" in out
    assert "Xeno Prototype" in out
    assert "appears with Delta One in 4/4 harvested decks (lift 2.4)" in out


def test_cli_show_lift_reports_small_corpus(tmp_path, monkeypatch, capsys):
    from commander_builder import improvement_advisor as ia
    _stub_advise(monkeypatch)
    deck_dir = tmp_path / "empty-decks"
    user = _dck(deck_dir, "[USER] Me [B3].dck", ["Lonely Card"])
    monkeypatch.setattr(ia, "DECK_DIR", deck_dir)

    rc = ia.main(["--user", user.name, "--bracket", "3", "--show-lift"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Corpus too small" in out


def test_cli_without_show_lift_prints_no_lift_section(
    tmp_path, monkeypatch, capsys,
):
    from commander_builder import improvement_advisor as ia
    _stub_advise(monkeypatch)
    monkeypatch.setattr(ia, "DECK_DIR", tmp_path / "decks")
    rc = ia.main(["--user", "x.dck", "--bracket", "3"])
    assert rc == 0
    assert "Lift analysis" not in capsys.readouterr().out

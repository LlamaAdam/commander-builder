"""Tests for the explainable bracket estimator (ManaFoundry parity).

Pins:
  * every HARD BOUND against the repo's encoded bracket rules
    (prompts/moxfield_audit_v3.md table + combo_detection floors):
    any GC -> floor 3, >3 GCs -> floor 4 (3 GCs stays floor 3 — the
    table says "Max 3"), 2-card game-ending combo -> floor 4, MLD ->
    floor 4, 2+ extra turns -> floor 4;
  * each weighted signal's DIRECTION (tutors / fast mana / archetype /
    curve / salt push the raw score the right way);
  * the mismatch policy (>= 1 -> "check", >= 2 -> "mismatch"/True) at
    medium/high confidence, and the CONFIDENCE GATE: low-confidence
    estimates (signal starvation) report "low_signal" instead of
    check/mismatch, mismatch stays False, and every consumer renders
    distinct "unavailable/low-signal" copy instead of a warning;
  * the never-raises contract on degenerate decks;
  * the dashboard payload shape;
  * the pool-hygiene warning (fires at diff >= 2, silent at 1) at the
    helper level and through both callers (meta_test import,
    pool_curator CLI).

Every test monkeypatches load_game_changers / load_combos to fixed
sets so results don't depend on the .cache state or a network fetch.
"""

from __future__ import annotations

import json

import pytest

from commander_builder.bracket_estimator import (
    estimate_bracket,
    mismatch_warning,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A deliberately tiny, offline-stable GC set for tests. The real list
# (game_changers._FALLBACK) contains all of these; pinning a fixed set
# keeps the counts deterministic if WotC ever changes the real list.
_TEST_GC = {
    "Rhystic Study", "Smothering Tithe", "Cyclonic Rift",
    "Demonic Tutor", "Vampiric Tutor", "Mystical Tutor",
}

# Fixed combo DB (mirrors two entries of combo_detection._FALLBACK):
# one 2-card game-ending combo, one 3-card game-ending combo.
_TEST_COMBOS = [
    {"cards": ["Mikaeus, the Unhallowed", "Triskelion"],
     "produces": "Infinite damage"},
    {"cards": ["Underworld Breach", "Lion's Eye Diamond", "Brain Freeze"],
     "produces": "Win the game"},
]


@pytest.fixture(autouse=True)
def _pin_rule_data(monkeypatch):
    """Deterministic GC list + combo DB for every test in this module.

    The estimator imports both lazily at call time, so patching the
    source modules' loaders is sufficient (same pattern as
    test_deck_dashboard's lookup_card stubs).
    """
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers",
        lambda **_kw: set(_TEST_GC),
    )
    monkeypatch.setattr(
        "commander_builder.combo_detection.load_combos",
        lambda **_kw: list(_TEST_COMBOS),
    )


def _deck(*cards: str, lands: int = 35, filler: int = 0) -> str:
    """Synthesize a legal-ish .dck blob: commander + named cards +
    basic lands + optional distinct vanilla filler (distinct names so
    the dedup'd card count crosses the estimator's 20-card
    small-list confidence threshold)."""
    lines = ["[metadata]", "Name=Test", "[Commander]", "1 Test Commander",
             "[Main]"]
    lines += [f"1 {c}" for c in cards]
    lines += [f"1 Filler Creature {i}" for i in range(filler)]
    lines += [f"{lands} Forest"] if lands else []
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_plain_precon_list_estimates_core():
    """No GCs, no combos, no bumpers -> B2 'Core' precon baseline
    with floor 1 (nothing rule-violating)."""
    r = estimate_bracket(_deck(filler=30))
    assert r["estimate"] == 2
    assert r["floor"] == 1
    assert r["mismatch_level"] is None
    assert r["mismatch"] is False


# ---------------------------------------------------------------------------
# Hard bounds — Game Changer caps (prompt table: B1/B2=0, B3 max 3)
# ---------------------------------------------------------------------------

def test_one_game_changer_floors_at_3():
    r = estimate_bracket(_deck("Rhystic Study", filler=30))
    assert r["floor"] == 3
    assert r["estimate"] >= 3
    assert any("floor B3" in reason for reason in r["reasons"])


def test_three_game_changers_floor_stays_3_not_4():
    """The bracket table says B3 allows a MAX of 3 GCs — exactly 3 is
    still a legal B3 deck. (Deliberately diverges from
    _power_bracket's 3+ -> guess-4 heuristic; the table is the rule.)"""
    r = estimate_bracket(_deck(
        "Rhystic Study", "Smothering Tithe", "Cyclonic Rift", filler=30,
    ))
    assert r["floor"] == 3
    assert r["signals"]["n_game_changers"] == 3


def test_four_game_changers_floor_4():
    r = estimate_bracket(_deck(
        "Rhystic Study", "Smothering Tithe", "Cyclonic Rift",
        "Demonic Tutor", filler=30,
    ))
    assert r["floor"] == 4
    assert r["estimate"] >= 4
    assert any("exceeds B3's max of 3" in reason for reason in r["reasons"])


# ---------------------------------------------------------------------------
# Hard bounds — combos (combo_detection.combo_bracket_floor)
# ---------------------------------------------------------------------------

def test_two_card_game_ending_combo_floors_4():
    r = estimate_bracket(_deck(
        "Mikaeus, the Unhallowed", "Triskelion", filler=30,
    ))
    assert r["floor"] == 4
    assert r["signals"]["n_two_card_combos"] == 1


def test_three_card_game_ending_combo_floors_3():
    r = estimate_bracket(_deck(
        "Underworld Breach", "Lion's Eye Diamond", "Brain Freeze",
        filler=30,
    ))
    assert r["floor"] == 3
    assert r["signals"]["n_two_card_combos"] == 0
    assert r["signals"]["n_game_ending_combos"] == 1


# ---------------------------------------------------------------------------
# Hard bounds — MLD / extra turns (prompt auto-bumper lists)
# ---------------------------------------------------------------------------

def test_mass_land_denial_floors_4():
    r = estimate_bracket(_deck("Armageddon", filler=30))
    assert r["floor"] == 4
    assert r["signals"]["mld_cards"] == ["armageddon"]


def test_two_extra_turn_cards_floor_4():
    r = estimate_bracket(_deck("Time Warp", "Temporal Manipulation",
                               filler=30))
    assert r["floor"] == 4


def test_single_extra_turn_card_is_nudge_not_floor():
    """One extra-turn spell is B3-legal (un-chained); it contributes a
    weighted nudge but must NOT floor the deck at 4."""
    r = estimate_bracket(_deck("Time Warp", filler=30))
    assert r["floor"] == 1
    assert r["estimate"] < 4
    assert any("extra-turn" in reason for reason in r["reasons"])


# ---------------------------------------------------------------------------
# Weighted signals — direction pins (raw score must move the right way)
# ---------------------------------------------------------------------------

def _raw(deck_text: str, **kw) -> float:
    return estimate_bracket(deck_text, **kw)["signals"]["score_raw"]


def test_tutor_density_pushes_up_and_4_plus_steps_harder():
    """Non-GC tutors so the GC signal stays silent: 2-3 tutors add the
    half signal; 4+ triggers the prompt's 'stacking 4+ tutors
    auto-bumps' full step."""
    base = _raw(_deck(filler=30))
    two = _raw(_deck("Diabolic Tutor", "Green Sun's Zenith", filler=30))
    four = _raw(_deck("Diabolic Tutor", "Green Sun's Zenith",
                      "Chord of Calling", "Fabricate", filler=30))
    assert base < two < four


def test_fast_mana_pushes_up():
    base = _raw(_deck(filler=30))
    fast = _raw(_deck("Dark Ritual", "Lotus Petal", "Mox Opal", filler=30))
    assert fast > base


def test_combo_archetype_pushes_up_more_than_stax():
    base = _raw(_deck(filler=30))
    stax = _raw(_deck(filler=30), archetype="stax")
    combo = _raw(_deck(filler=30), archetype="combo")
    assert base < stax < combo


def test_avg_cmc_tight_up_high_down():
    neutral = _raw(_deck(filler=30), avg_cmc=3.0)
    tight = _raw(_deck(filler=30), avg_cmc=2.2)
    high = _raw(_deck(filler=30), avg_cmc=4.5)
    assert high < neutral < tight


def test_salt_signal_reads_offline_cache_only(monkeypatch, tmp_path):
    """5+ deck cards at/above salt 1.5 in the DISK cache add the salty
    signal. The cache file lives at CACHE_DIR.parent/edhrec_salt/
    top-salt.json (the path fetch_salt_list persists); no network."""
    import commander_builder.edhrec_client as ec
    monkeypatch.setattr(ec, "CACHE_DIR", tmp_path / "edhrec")
    salt_dir = tmp_path / "edhrec_salt"
    salt_dir.mkdir(parents=True)
    salty = ["salt card a", "salt card b", "salt card c",
             "salt card d", "salt card e"]
    (salt_dir / "top-salt.json").write_text(
        json.dumps({name: 2.0 for name in salty}), encoding="utf-8",
    )
    deck = _deck(*[s.title() for s in salty], filler=25)
    r = estimate_bracket(deck)
    assert r["signals"]["salt_count"] == 5
    assert any("salt" in reason for reason in r["reasons"])
    # And absent cache -> signal unavailable (None), never 0.
    monkeypatch.setattr(ec, "CACHE_DIR", tmp_path / "nonexistent" / "x")
    assert estimate_bracket(deck)["signals"]["salt_count"] is None


# ---------------------------------------------------------------------------
# Mismatch policy: >= 1 "check", >= 2 "mismatch"
# ---------------------------------------------------------------------------

def _four_gc_deck() -> str:
    """Estimate lands at exactly 4 (floor 4 via >3 GCs)."""
    return _deck("Rhystic Study", "Smothering Tithe", "Cyclonic Rift",
                 "Demonic Tutor", filler=30)


def test_mismatch_levels_against_declared():
    deck = _four_gc_deck()
    est = estimate_bracket(deck)["estimate"]
    assert est >= 4
    same = estimate_bracket(deck, declared=est)
    assert same["mismatch_level"] is None and same["mismatch"] is False
    off1 = estimate_bracket(deck, declared=est - 1)
    assert off1["mismatch_level"] == "check" and off1["mismatch"] is False
    off2 = estimate_bracket(deck, declared=est - 2)
    assert off2["mismatch_level"] == "mismatch" and off2["mismatch"] is True


def test_no_declared_no_mismatch_fields_set():
    r = estimate_bracket(_four_gc_deck(), declared=None)
    assert r["declared"] is None
    assert r["mismatch_level"] is None
    assert r["mismatch"] is False


# ---------------------------------------------------------------------------
# Confidence gate: low-confidence estimates are "low_signal", never a
# mismatch (the Atraxa/Chulane FP2 sweep case — a starved estimator
# defaults to the B2 baseline and must not accuse the declared tag)
# ---------------------------------------------------------------------------

def test_low_confidence_gap_reports_low_signal_not_mismatch():
    """Nothing fires (no GCs/tutors/fast mana/combos, no avg_cmc /
    archetype / salt context) -> B2 baseline at LOW confidence.
    Declared B4 (diff 2) must NOT flag a mismatch — it reports the
    distinct 'low_signal' level with mismatch False."""
    r = estimate_bracket(_deck(filler=30), declared=4)
    assert r["estimate"] == 2
    assert r["confidence"] == "low"
    assert r["mismatch"] is False
    assert r["mismatch_level"] == "low_signal"


def test_low_confidence_diff_1_is_low_signal_not_check():
    r = estimate_bracket(_deck(filler=30), declared=3)
    assert r["confidence"] == "low"
    assert r["mismatch_level"] == "low_signal"
    assert r["mismatch"] is False


def test_low_confidence_agreement_stays_clean():
    """Low confidence + declared == estimate: no level at all (the
    gate only rewrites disagreements, never invents one)."""
    r = estimate_bracket(_deck(filler=30), declared=2)
    assert r["confidence"] == "low"
    assert r["mismatch_level"] is None
    assert r["mismatch"] is False


def test_medium_confidence_mismatch_still_flags():
    """The gate is EXACTLY confidence == 'low': a medium-confidence
    estimate (1-2 weighted signals, no floor) keeps the original >= 2
    mismatch policy unchanged."""
    deck = _deck("Diabolic Tutor", "Green Sun's Zenith",
                 "Chord of Calling", "Fabricate", filler=30)
    r = estimate_bracket(deck, declared=1)
    assert r["confidence"] == "medium"
    assert r["estimate"] - 1 >= 2
    assert r["mismatch"] is True
    assert r["mismatch_level"] == "mismatch"


def test_mismatch_warning_low_confidence_gives_low_signal_note():
    """mismatch_warning on a starved deck declared 2+ off: a NOTE with
    the distinct unavailable/low-signal copy, never a WARN. Diff 1 at
    low confidence stays silent (parity with the medium/high rule)."""
    deck = _deck(filler=30)  # estimates B2 at low confidence
    note = mismatch_warning("Cold Deck [B4].dck", deck, 4)
    assert note is not None
    assert note.startswith("NOTE:")
    assert "WARN" not in note
    assert "unavailable/low-signal: B2?" in note
    assert "insufficient signal" in note
    assert mismatch_warning("Cold Deck [B3].dck", deck, 3) is None


def test_report_text_renders_low_signal_estimate():
    """commander-advise report line: low_signal renders the distinct
    unavailable/low-signal copy, no MISMATCH/check verdict."""
    from commander_builder.improvement_advisor import (
        AdviceReport, _format_report_text,
    )
    report = AdviceReport(
        deck_filename="x.dck", deck_id=None, bracket=4,
        commander_names=["Test Commander"],
    )
    est = estimate_bracket(_deck(filler=30), declared=4)
    assert est["mismatch_level"] == "low_signal"
    text = _format_report_text(report, bracket_estimate=est)
    assert "Estimated bracket: unavailable/low-signal: B2?" in text
    assert "insufficient signal" in text
    assert "MISMATCH" not in text
    # And a well-signaled mismatch keeps the legacy verdict line.
    est2 = estimate_bracket(_four_gc_deck(), declared=2)
    assert est2["mismatch_level"] == "mismatch"
    text2 = _format_report_text(report, bracket_estimate=est2)
    assert "MISMATCH vs declared" in text2


# ---------------------------------------------------------------------------
# Never-raises contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("weird", [
    "",                                    # empty
    "not a deck at all \x00\x01\x02",      # binary-ish garbage
    "[Main]\n",                            # sections but no cards
    "[metadata]\nName=x\n",                # no card sections
    "[Main]\n99 Forest\n",                 # all lands, no commander
    "1 Sol Ring\n1 Forest\n",              # cards outside any section
])
def test_never_raises_on_weird_decks(weird):
    r = estimate_bracket(weird, declared=3)
    assert isinstance(r, dict)
    assert 1 <= r["estimate"] <= 5
    assert 1 <= r["floor"] <= 5
    assert r["confidence"] in ("low", "medium", "high")
    assert isinstance(r["reasons"], list)


def test_small_lists_are_low_confidence():
    """< 20 distinct cards can never be high-confidence, even when a
    hard floor fired (a 3-card paste with a combo is still a guess)."""
    r = estimate_bracket(_deck("Mikaeus, the Unhallowed", "Triskelion",
                               lands=1, filler=0))
    assert r["floor"] == 4
    assert r["confidence"] == "low"


# ---------------------------------------------------------------------------
# Dashboard payload shape
# ---------------------------------------------------------------------------

def test_dashboard_payload_gains_bracket_estimate(tmp_path, monkeypatch):
    from commander_builder.deck_dashboard import build_dashboard

    deck = tmp_path / "deck.dck"
    deck.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Test Cmdr\n[Main]\n"
        "1 Rhystic Study\n1 Smothering Tithe\n"
        + "1 Forest\n" * 35
        + "".join(f"1 Filler {i}\n" for i in range(30)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda name, **_kw: {
            "type_line": "Artifact", "oracle_text": "", "cmc": 2.0,
            "color_identity": [], "prices": {"usd": "1.00"},
        },
    )
    payload = build_dashboard(deck, bracket=3).to_dict()
    est = payload["bracket_estimate"]
    assert est is not None
    assert est["declared"] == 3
    # 2 GCs -> floor 3 (B1/B2 allow zero).
    assert est["floor"] == 3
    assert est["estimate"] >= 3
    for key in ("estimate", "floor", "confidence", "reasons", "signals",
                "declared", "mismatch", "mismatch_level"):
        assert key in est


# ---------------------------------------------------------------------------
# Pool-hygiene warning helper + both callers
# ---------------------------------------------------------------------------

def test_mismatch_warning_fires_at_diff_2_not_1():
    deck = _four_gc_deck()
    est = estimate_bracket(deck)["estimate"]
    # diff >= 2 -> warning string (print-only contract, never a reject)
    warn = mismatch_warning("Foo [B2].dck", deck, est - 2)
    assert warn is not None and "WARN" in warn
    assert f"B{est - 2}" in warn and f"B{est}" in warn
    # diff == 1 -> silent (soft "check" is dashboard territory)
    assert mismatch_warning("Foo [B3].dck", deck, est - 1) is None
    # unknown/zero bracket ([B?] refs) -> silent
    assert mismatch_warning("Foo [B?].dck", deck, None) is None
    assert mismatch_warning("Foo [B?].dck", deck, 0) is None


def test_meta_test_import_warns_on_mismatched_reference(tmp_path, capsys):
    """_import_reference prints (never rejects) when the imported ref's
    estimate is >= 2 off its claimed bracket."""
    from commander_builder.meta_test import _import_reference

    def _deck_json(bracket):
        return {
            "name": "Ref Deck", "publicId": "mx-1", "bracket": bracket,
            "boards": {
                "commanders": {"cards": {
                    "k0": {"quantity": 1, "card": {"name": "Cmdr"}},
                }},
                "mainboard": {"cards": {
                    f"k{i}": {"quantity": 1, "card": {"name": name}}
                    for i, name in enumerate(
                        ["Rhystic Study", "Smothering Tithe",
                         "Cyclonic Rift", "Demonic Tutor"]
                        + [f"Filler {j}" for j in range(30)]
                    )
                }},
            },
        }

    # 4 GCs -> estimate >= 4; claimed B2 -> diff >= 2 -> WARN printed.
    ref = _import_reference(_deck_json(2), "moxfield_top_likes",
                            deck_dir=tmp_path)
    out = capsys.readouterr().out
    assert "WARN" in out and ref.deck_filename in out
    # Claimed B3 -> diff 1 -> import stays silent.
    _import_reference(_deck_json(3), "edhrec_avg", deck_dir=tmp_path)
    assert "WARN" not in capsys.readouterr().out


def test_pool_curator_main_warns_on_mislabeled_candidate(
    tmp_path, monkeypatch, capsys,
):
    """The candidate listing WARNs (print only) on decks whose estimate
    is >= 2 off their [Bn] tag, then curation proceeds normally."""
    import commander_builder.pool_curator as pc
    from commander_builder.pool_curator import InsufficientSurvivorsError

    hot = "Hot Deck [B2].dck"
    names = [hot] + [f"ok{i} [B2].dck" for i in range(3)]
    (tmp_path / hot).write_text(_four_gc_deck(), encoding="utf-8")
    for n in names[1:]:
        (tmp_path / n).write_text(_deck(filler=30), encoding="utf-8")

    monkeypatch.setattr(pc, "DECK_DIR", tmp_path)
    monkeypatch.setattr(
        pc, "_list_bracket_candidates", lambda bracket: list(names),
    )

    def _stop(*args, **kwargs):
        # Terminate main right after the hygiene pass — the sims
        # themselves are out of scope for this test.
        raise InsufficientSurvivorsError("stop", rejected=[])

    monkeypatch.setattr(pc, "curate_bracket", _stop)
    assert pc.main(["--bracket", "2"]) == 3
    out = capsys.readouterr().out
    assert "WARN" in out and hot in out
    # Correctly-labeled candidates must not be flagged.
    assert not any(
        "ok" in line for line in out.splitlines() if "WARN" in line
    )


def test_meta_test_import_low_signal_ref_notes_not_warns(tmp_path, capsys):
    """A starved reference (nothing classifiable) claiming B4 prints
    the low-signal NOTE — never the mismatch WARN — and the import
    proceeds."""
    from commander_builder.meta_test import _import_reference

    deck_json = {
        "name": "Cold Ref", "publicId": "mx-2", "bracket": 4,
        "boards": {
            "commanders": {"cards": {
                "k0": {"quantity": 1, "card": {"name": "Cmdr"}},
            }},
            "mainboard": {"cards": {
                f"k{i}": {"quantity": 1, "card": {"name": f"Filler {i}"}}
                for i in range(30)
            }},
        },
    }
    ref = _import_reference(deck_json, "moxfield_top_likes",
                            deck_dir=tmp_path)
    out = capsys.readouterr().out
    assert "WARN" not in out
    assert "NOTE" in out and ref.deck_filename in out
    assert "unavailable/low-signal" in out


def test_pool_curator_low_signal_candidate_notes_not_warns(
    tmp_path, monkeypatch, capsys,
):
    """Starved candidates tagged [B4]: the hygiene pass prints the
    low-signal NOTE instead of the mismatch WARN."""
    import commander_builder.pool_curator as pc
    from commander_builder.pool_curator import InsufficientSurvivorsError

    names = [f"cold{i} [B4].dck" for i in range(4)]
    for n in names:
        (tmp_path / n).write_text(_deck(filler=30), encoding="utf-8")

    monkeypatch.setattr(pc, "DECK_DIR", tmp_path)
    monkeypatch.setattr(
        pc, "_list_bracket_candidates", lambda bracket: list(names),
    )
    monkeypatch.setattr(
        pc, "curate_bracket",
        lambda *a, **kw: (_ for _ in ()).throw(
            InsufficientSurvivorsError("stop", rejected=[])
        ),
    )
    assert pc.main(["--bracket", "4"]) == 3
    out = capsys.readouterr().out
    assert "WARN" not in out
    assert "NOTE" in out and "unavailable/low-signal" in out

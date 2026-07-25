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


# ---------------------------------------------------------------------------
# derive_signals — the shared context helper
#
# estimate_bracket takes avg_cmc/archetype as optional pre-computed context
# because deriving them costs a Scryfall lookup per card, which its
# offline/never-blocks contract forbids. Only the dashboard ever passed them,
# so curve_tight (+0.5), curve_high (-0.5), archetype_combo (+1.0) and
# archetype_stax (+0.5) were dead weight in the CLI paths. These pin the one
# derivation helper all three callers now share.
# ---------------------------------------------------------------------------

def _cmc_lookup(default=None, **by_name):
    """Scryfall-shaped lookup. ``by_name`` keys use ``_`` for spaces."""
    table = {k.replace("_", " "): v for k, v in by_name.items()}

    def _lookup(name):
        if name in table:
            return table[name]
        return dict(default) if default else None
    return _lookup


def test_derive_signals_computes_avg_cmc_from_scryfall():
    from commander_builder.bracket_estimator import derive_signals
    deck = _deck("Cheap Spell", "Pricey Spell", lands=0)
    lookup = _cmc_lookup(
        Test_Commander={"cmc": 4.0, "type_line": "Legendary Creature"},
        Cheap_Spell={"cmc": 1.0, "type_line": "Instant"},
        Pricey_Spell={"cmc": 7.0, "type_line": "Sorcery"},
    )
    avg_cmc, _archetype = derive_signals(deck, lookup=lookup)
    assert avg_cmc == 4.0


def test_derive_signals_excludes_lands_from_avg_cmc():
    """Mirrors the dashboard's stat tile: lands are excluded, so a 35-land
    deck doesn't read as the tightest curve ever built."""
    from commander_builder.bracket_estimator import derive_signals
    deck = _deck("Big Spell", lands=35)
    lookup = _cmc_lookup(
        Test_Commander={"cmc": 4.0, "type_line": "Legendary Creature"},
        Big_Spell={"cmc": 6.0, "type_line": "Sorcery"},
        Forest={"cmc": 0.0, "type_line": "Basic Land — Forest"},
    )
    avg_cmc, _ = derive_signals(deck, lookup=lookup)
    assert avg_cmc == 5.0


def test_derive_signals_is_quantity_weighted():
    from commander_builder.bracket_estimator import derive_signals
    deck = "[Commander]\n1 Cheap Spell\n[Main]\n9 Cheap Spell\n1 Big Spell\n"
    lookup = _cmc_lookup(
        Cheap_Spell={"cmc": 1.0, "type_line": "Instant"},
        Big_Spell={"cmc": 11.0, "type_line": "Sorcery"},
    )
    avg_cmc, _ = derive_signals(deck, lookup=lookup)
    # 10 copies of the 1-drop (1 in the command zone + 9 main) + one 11-drop.
    assert avg_cmc == round(21 / 11, 2) == 1.91


def test_derive_signals_avg_cmc_is_none_not_zero_when_nothing_resolves():
    """A Scryfall outage must read as "signal unavailable", never as the
    tightest possible curve. 0.0 would fire curve_tight (+0.5)."""
    from commander_builder.bracket_estimator import derive_signals
    avg_cmc, _ = derive_signals(_deck(filler=30), lookup=lambda n: None)
    assert avg_cmc is None


def test_derive_signals_avg_cmc_is_none_on_an_all_lands_deck():
    from commander_builder.bracket_estimator import derive_signals
    deck = "[Main]\n40 Forest\n"
    lookup = _cmc_lookup(Forest={"cmc": 0.0, "type_line": "Basic Land — Forest"})
    avg_cmc, _ = derive_signals(deck, lookup=lookup)
    assert avg_cmc is None


def test_derive_signals_skips_unresolvable_cards_rather_than_zeroing_them():
    """A partial resolve averages what we know. Counting a 404 as CMC 0
    would drag the curve down and fire curve_tight on a battlecruiser."""
    from commander_builder.bracket_estimator import derive_signals
    deck = _deck("Known Spell", "Mystery Card", lands=0)
    lookup = _cmc_lookup(Known_Spell={"cmc": 5.0, "type_line": "Sorcery"})
    avg_cmc, _ = derive_signals(deck, lookup=lookup)
    assert avg_cmc == 5.0


def test_derive_signals_survives_a_raising_lookup():
    """Fail-quiet contract: a lookup that blows up yields None, not an
    exception escaping into the CLI."""
    from commander_builder.bracket_estimator import derive_signals

    def _boom(_name):
        raise RuntimeError("scryfall exploded")
    avg_cmc, archetype = derive_signals(_deck(filler=30), lookup=_boom)
    assert avg_cmc is None
    assert archetype in (None, "midrange", "aggro", "control", "combo", "stax")


def test_derive_signals_never_raises_on_garbage_input():
    from commander_builder.bracket_estimator import derive_signals
    for junk in ("", "\x00\xff not a deck", "[Main]\n"):
        assert derive_signals(junk, lookup=lambda n: None) == (None, None)


def test_derive_signals_uses_the_filename_hint_when_given_a_path(tmp_path):
    """With a path we use the canonical archetype.classify ladder, whose
    first rung is the filename — a deck the user named "Storm Combo" is
    telling us the strategy outright."""
    from commander_builder.bracket_estimator import derive_signals
    deck_path = tmp_path / "[USER] Storm Combo [B4].dck"
    deck_path.write_text(_deck(filler=30), encoding="utf-8")
    _avg, archetype = derive_signals(
        deck_path.read_text(encoding="utf-8"),
        deck_path=deck_path,
        lookup=lambda n: None,
    )
    assert archetype == "combo"


def test_derive_signals_falls_back_to_the_content_scan_without_a_path():
    """The steering loop scores rendered text that has no file yet."""
    from commander_builder.bracket_estimator import derive_signals
    deck = _deck("Winter Orb", "Static Orb", "Stasis", "Smokestack",
                 "Tangle Wire", "Trinisphere", filler=25)
    _avg, archetype = derive_signals(deck, lookup=lambda n: None)
    assert archetype == "stax"


def test_derive_signals_archetype_is_none_rather_than_fabricated():
    """No winner in the content scan -> None ("unavailable"), NOT the
    classifier's "midrange" default. A fabricated label would be a lie in
    the signals payload the UI renders."""
    from commander_builder.bracket_estimator import derive_signals
    _avg, archetype = derive_signals(_deck(filler=30), lookup=lambda n: None)
    assert archetype is None


def test_derive_signals_ignores_a_deck_path_that_does_not_exist(tmp_path):
    """classify() on a missing file silently returns its "midrange"
    default; we must not launder that into a real-looking signal."""
    from commander_builder.bracket_estimator import derive_signals
    _avg, archetype = derive_signals(
        _deck(filler=30),
        deck_path=tmp_path / "gone.dck",
        lookup=lambda n: None,
    )
    assert archetype is None


def test_derived_signals_actually_move_the_estimate():
    """The point of the whole fix: 1.5 points of signal that could never
    fire in the CLI paths. combo archetype (+1.0) + tight curve (+0.5)."""
    deck = _deck("Thassa's Oracle", "Isochron Scepter", "Dramatic Reversal",
                 "Underworld Breach", "Ad Nauseam", "Food Chain", filler=25)
    from commander_builder.bracket_estimator import derive_signals
    lookup = _cmc_lookup(default={"cmc": 2.0, "type_line": "Instant"})
    avg_cmc, archetype = derive_signals(deck, lookup=lookup)
    assert archetype == "combo" and avg_cmc == 2.0

    blind = estimate_bracket(deck)
    informed = estimate_bracket(deck, avg_cmc=avg_cmc, archetype=archetype)
    assert informed["signals"]["score_raw"] - blind["signals"]["score_raw"] == 1.5
    assert informed["estimate"] > blind["estimate"]


# ---------------------------------------------------------------------------
# Call sites — all three estimator callers must now get the same treatment
# ---------------------------------------------------------------------------

def test_advise_cli_passes_derived_signals_to_the_estimator(
    tmp_path, monkeypatch,
):
    """``commander-advise`` used to call estimate_bracket with the deck text
    and nothing else, so its curve/archetype weights were permanently
    silent. It must derive them — and hand over the PATH so the archetype
    classifier gets its filename hint."""
    import commander_builder.bracket_estimator as be
    from commander_builder.improvement_advisor import AdviceReport
    import commander_builder.improvement_advisor as ia

    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = deck_dir / "[USER] Storm Combo [B4].dck"
    deck.write_text(_deck("Rhystic Study", filler=30), encoding="utf-8")

    monkeypatch.setattr(ia, "DECK_DIR", deck_dir)
    monkeypatch.setattr(
        ia, "advise",
        lambda deck_path, bracket, **kw: AdviceReport(
            deck_filename=deck_path.name, deck_id=None, bracket=bracket,
            commander_names=["Test Commander"],
        ),
    )
    monkeypatch.setattr(
        "commander_builder.deck_health.compute_health_grade",
        lambda *a, **kw: None,
    )

    derived: dict = {}

    def _spy_derive(deck_text, deck_path=None, lookup=None):
        derived["deck_path"] = deck_path
        return 2.1, "combo"
    monkeypatch.setattr(be, "derive_signals", _spy_derive)

    seen: dict = {}

    def _spy_estimate(deck_text, declared=None, **kw):
        seen.update(kw)
        return real_estimate(deck_text, declared, **kw)
    real_estimate = be.estimate_bracket
    monkeypatch.setattr(be, "estimate_bracket", _spy_estimate)

    assert ia.main(["--user", str(deck), "--bracket", "4"]) == 0
    assert seen.get("avg_cmc") == 2.1
    assert seen.get("archetype") == "combo"
    # The path went along so archetype.classify can use the filename hint.
    assert derived["deck_path"] is not None
    assert derived["deck_path"].name == deck.name


def test_deck_builder_steering_loop_passes_derived_signals(monkeypatch):
    """The steering loop re-estimates after every swap — it is THE path
    where the curve/archetype weights matter, and it was passing neither.
    It must now feed derived context in, derive it ONCE (not per iteration),
    and route the derivation through the build's own injected lookup."""
    from commander_builder import deck_builder
    from commander_builder.edhrec_client import CardEntry
    from types import SimpleNamespace

    cards = {
        "Krenko, Mob Boss": {
            "type_line": "Legendary Creature — Goblin", "color_identity": ["R"],
            "mana_cost": "{2}{R}{R}", "cmc": 4.0, "oracle_text": "",
        },
        "Fast Rock": {
            "type_line": "Artifact", "color_identity": [], "mana_cost": "{1}",
            "cmc": 1.0, "oracle_text": "Add {R}.",
        },
    }
    lookup_calls: list[str] = []

    def _lookup(name):
        lookup_calls.append(name)
        if name in cards:
            return cards[name]
        if name.startswith("Goblin "):
            return {"type_line": "Creature — Goblin", "color_identity": ["R"],
                    "mana_cost": "{1}{R}", "cmc": 2.0, "oracle_text": ""}
        if name == "Mountain":
            return {"type_line": "Basic Land — Mountain", "color_identity": ["R"],
                    "mana_cost": "", "cmc": 0.0, "oracle_text": ""}
        return None

    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _lookup)

    seen: list[dict] = []

    def _spy_estimate(text, declared=None, **kw):
        seen.append(kw)
        return {"estimate": 2}  # always under target -> the loop keeps going
    monkeypatch.setattr(deck_builder, "estimate_bracket", _spy_estimate)

    derive_calls: list = []
    real_derive = deck_builder.derive_signals

    def _counting_derive(deck_text, deck_path=None, lookup=None):
        derive_calls.append(lookup)
        return real_derive(deck_text, deck_path=deck_path, lookup=lookup)
    monkeypatch.setattr(deck_builder, "derive_signals", _counting_derive)

    seed = ["Krenko, Mob Boss"] + [f"Goblin {i}" for i in range(80)]
    deck_builder._assemble(
        "Krenko, Mob Boss", 4, None,
        fetch_avg=lambda c, b: SimpleNamespace(
            cards=[CardEntry(name=n) for n in seed]),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "R",
        lookup=_lookup,
        name="Krenko",
        enable_lift=False,
        owned_bias=False,
        estimate_fn=None,          # <- the production path under test
        is_game_changer=lambda nm: False,
        is_fast_mana=lambda nm: deck_builder.name_key(nm) == "fast rock",
        power_pool=["Fast Rock"],
        owned_names=[],
    )

    assert seen, "the steering loop never called the estimator"
    # Every estimate carried the derived context...
    assert all("avg_cmc" in kw and "archetype" in kw for kw in seen)
    # ...the curve signal actually resolved, into the tight-curve band the
    # fake 2-drop-heavy DB implies (it was None on every call before)...
    assert seen[0]["avg_cmc"] is not None
    assert 0 < seen[0]["avg_cmc"] <= 2.6
    # ...it is stable across iterations (derived once, then reused)...
    assert len({kw["avg_cmc"] for kw in seen}) == 1
    assert len(derive_calls) == 1
    # ...and it reused the build's injected lookup instead of the network.
    assert derive_calls[0] is _lookup

"""deck_builder (FP-014.1) tests — offline via injected fake fetchers.

Every EDHREC/Scryfall touchpoint is either injected into ``build_deck`` /
``_assemble`` or monkeypatched at the module level, so nothing here reaches
the network. ``enforce_color_identity`` reaches for
``scryfall_client.lookup_card`` at call time, so tests that exercise the
color-identity filter monkeypatch that symbol with the same fake DB used for
the injected ``lookup``.
"""
from types import SimpleNamespace

import pytest

from commander_builder import deck_builder
from commander_builder import deck_builder_personalize as personalize
from commander_builder.deck_builder import (
    _distribute_basics_by_pips,
    _assemble,
    build_deck,
)
from commander_builder.dck_utils import (
    count_main_cards,
    main_card_quantities,
    section_card_names,
)
from commander_builder.edhrec_client import CardEntry, CommanderPage
from commander_builder.log_parser import _normalize


# --- Fake card DB ---------------------------------------------------------

_R_CREATURE = {
    "type_line": "Creature — Goblin", "color_identity": ["R"],
    "mana_cost": "{1}{R}",
}
_U_CREATURE = {
    "type_line": "Creature — Merfolk", "color_identity": ["U"],
    "mana_cost": "{1}{U}",
}
_FAKE_CARDS = {
    "Krenko, Mob Boss": {
        "type_line": "Legendary Creature — Goblin",
        "color_identity": ["R"], "mana_cost": "{2}{R}{R}",
    },
    "Command Tower": {
        "type_line": "Land", "color_identity": [], "mana_cost": "",
    },
    "Sol Ring": {
        "type_line": "Artifact", "color_identity": [], "mana_cost": "{1}",
    },
    "Cultivate": {  # green — off-color for a mono-red commander
        "type_line": "Sorcery", "color_identity": ["G"], "mana_cost": "{2}{G}",
    },
}


def _fake_lookup(name):
    if name in _FAKE_CARDS:
        return _FAKE_CARDS[name]
    if name.startswith(("Goblin ", "Top ", "Spell ")):
        return dict(_R_CREATURE)
    if name.startswith("Merfolk "):
        return dict(_U_CREATURE)
    return None


def _avg(cards):
    """Minimal AverageDeck stand-in — build_deck only reads ``.cards``."""
    return SimpleNamespace(cards=[CardEntry(name=n) for n in cards])


def _page(top=None, high_synergy=None):
    return CommanderPage(
        commander_name="Fake", slug="fake", fetched_at="now",
        top_cards=[CardEntry(name=n) for n in (top or [])],
        high_synergy_cards=[CardEntry(name=n) for n in (high_synergy or [])],
    )


# --- Seed path ------------------------------------------------------------


def test_average_deck_seed_builds_legal_99():
    # Commander + 40 nonland spells + a nonbasic land + a basic in the seed.
    cards = (
        ["Krenko, Mob Boss"]
        + [f"Goblin {i}" for i in range(40)]
        + ["Command Tower", "Mountain"]
    )
    text = build_deck(
        "Krenko, Mob Boss", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: None,  # pass-through CI filter (offline)
        lookup=_fake_lookup,
        name="Krenko",
    )
    # Exactly 99 main + commander in the command zone.
    assert count_main_cards(text) == 99
    assert section_card_names(text, "Commander") == ["Krenko, Mob Boss"]
    mains = main_card_quantities(text)
    # Commander never leaks into [Main].
    assert "Krenko, Mob Boss" not in mains
    # FP-014.2: the seed's own nonbasic land is KEPT (not discarded like
    # FP-014.1) — its tuned fixing survives into the output at singleton.
    assert mains.get("Command Tower") == 1
    # The seed's basic (Mountain) is dropped and recomputed by the manabase.
    # Singleton: every nonbasic (spell or land) sits at quantity 1.
    for name, qty in mains.items():
        if name != "Mountain":
            assert qty == 1, f"{name} broke singleton with qty {qty}"


def test_name_field_normalizes_to_stem():
    cards = ["Krenko, Mob Boss"] + [f"Goblin {i}" for i in range(60)]
    result = _assemble(
        "Krenko, Mob Boss", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: None,
        lookup=_fake_lookup,
        name="Krenko",
    )
    assert result.stem == "[USER] Krenko [B3]"
    name_field = next(
        line[len("Name="):]
        for line in result.text.splitlines()
        if line.startswith("Name=")
    )
    assert name_field == result.stem
    assert _normalize(name_field) == _normalize(result.stem)


def test_color_identity_drops_off_color_card(monkeypatch):
    # enforce_color_identity looks cards up via scryfall_client.lookup_card.
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake_lookup,
    )
    cards = (
        ["Krenko, Mob Boss"]
        + [f"Goblin {i}" for i in range(40)]
        + ["Cultivate"]  # green, illegal in a mono-red deck
    )
    result = _assemble(
        "Krenko, Mob Boss", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "R",
        lookup=_fake_lookup,
        name="Krenko",
    )
    mains = main_card_quantities(result.text)
    assert "Cultivate" not in mains
    assert "Cultivate" in result.dropped_off_color
    assert count_main_cards(result.text) == 99
    # Mono-red identity → basics are Mountains only.
    assert set(mains) - {f"Goblin {i}" for i in range(40)} == {"Mountain"}


def test_singleton_dedupes_repeated_seed_card():
    cards = (
        ["Krenko, Mob Boss", "Goblin 1", "Goblin 1"]
        + [f"Goblin {i}" for i in range(2, 40)]
    )
    text = build_deck(
        "Krenko, Mob Boss", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: None,
        lookup=_fake_lookup,
        name="Krenko",
    )
    assert main_card_quantities(text)["Goblin 1"] == 1
    assert count_main_cards(text) == 99


# --- Fallback path --------------------------------------------------------


def test_fallback_builds_from_commander_page():
    page = _page(top=[f"Top {i}" for i in range(50)],
                 high_synergy=[f"Spell {i}" for i in range(20)])
    result = _assemble(
        "Some Commander", 3,
        fetch_avg=lambda c, b: None,  # no average deck published
        fetch_page=lambda c: page,
        resolve_ci=lambda n: None,
        lookup=_fake_lookup,
        name="Some Commander",
    )
    assert result.source == "commander-page fallback"
    assert count_main_cards(result.text) == 99
    # Cards came from the page's top list.
    mains = main_card_quantities(result.text)
    assert any(n.startswith("Top ") for n in mains)


def test_neither_source_available_raises_clean_error():
    with pytest.raises(ValueError, match="cannot build: no EDHREC data"):
        build_deck(
            "Nobody Special", 3,
            fetch_avg=lambda c, b: None,
            fetch_page=lambda c: None,
            resolve_ci=lambda n: None,
            lookup=_fake_lookup,
        )


def test_empty_commander_page_raises_clean_error():
    with pytest.raises(ValueError, match="cannot build: no EDHREC data"):
        build_deck(
            "Nobody Special", 3,
            fetch_avg=lambda c, b: None,
            fetch_page=lambda c: _page(),  # a page with zero cards
            resolve_ci=lambda n: None,
            lookup=_fake_lookup,
        )


def test_invalid_bracket_raises():
    with pytest.raises(ValueError, match="bracket must be"):
        build_deck("Krenko, Mob Boss", 9,
                   fetch_avg=lambda c, b: None, fetch_page=lambda c: None,
                   resolve_ci=lambda n: None, lookup=_fake_lookup)


# --- Basic-land distribution ---------------------------------------------


def test_distribute_basics_sums_to_total_and_splits_by_pips():
    out = _distribute_basics_by_pips(["R", "G"], {"R": 30, "G": 10}, 20)
    assert sum(out.values()) == 20
    # Heavier red pip weight → more Mountains than Forests.
    assert out["Mountain"] > out["Forest"]
    # Every identity color keeps at least one source.
    assert out["Forest"] >= 1


def test_distribute_basics_colorless_is_all_wastes():
    assert _distribute_basics_by_pips([], {}, 5) == {"Wastes": 5}


def test_distribute_basics_zero_pips_even_split():
    out = _distribute_basics_by_pips(
        ["W", "U", "B"], {"W": 0, "U": 0, "B": 0}, 3,
    )
    assert out == {"Plains": 1, "Island": 1, "Swamp": 1}


def test_two_color_build_splits_basics_by_pips():
    cards = (
        ["Some Commander"]
        + [f"Goblin {i}" for i in range(30)]   # 30 red pips
        + [f"Merfolk {i}" for i in range(10)]  # 10 blue pips
    )
    result = _assemble(
        "Some Commander", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "UR",
        lookup=_fake_lookup,
        name="Izzet Test",
    )
    mains = main_card_quantities(result.text)
    assert count_main_cards(result.text) == 99
    # 40 nonlands seeded, default land target not read from a landless seed
    # → 99 - 40 = 59 basics, split red-heavy by pip weight.
    assert result.land_count == 59
    assert mains["Mountain"] > mains["Island"] >= 1


# --- CLI smoke ------------------------------------------------------------


def test_main_cli_smoke(tmp_path, monkeypatch):
    cards = ["Krenko, Mob Boss"] + [f"Goblin {i}" for i in range(60)]
    monkeypatch.setattr(
        deck_builder, "fetch_average_deck", lambda c, b: _avg(cards),
    )
    monkeypatch.setattr(deck_builder, "fetch_commander_page", lambda c: None)
    monkeypatch.setattr(deck_builder, "lookup_card", _fake_lookup)
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake_lookup,
    )
    rc = deck_builder.main([
        "--commander", "Krenko, Mob Boss",
        "--bracket", "3",
        "--deck-dir", str(tmp_path),
    ])
    assert rc == 0
    files = list(tmp_path.glob("*.dck"))
    assert len(files) == 1
    assert files[0].name == "[USER] Krenko, Mob Boss Build [B3].dck"
    assert count_main_cards(files[0].read_text(encoding="utf-8")) == 99


def test_main_cli_reports_clean_error_on_no_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deck_builder, "fetch_average_deck", lambda c, b: None)
    monkeypatch.setattr(deck_builder, "fetch_commander_page", lambda c: None)
    monkeypatch.setattr(deck_builder, "lookup_card", _fake_lookup)
    rc = deck_builder.main([
        "--commander", "Nobody Special",
        "--deck-dir", str(tmp_path),
    ])
    assert rc == 1
    assert "cannot build: no EDHREC data" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.dck"))


# ==========================================================================
# FP-014.3 — personalization stages
# ==========================================================================
#
# The three stages are exercised both as UNITS (the pure transforms in
# deck_builder_personalize, driven by injected role/estimate/collection
# fakes — no Scryfall, no corpus) and end-to-end through ``_assemble`` (real
# classify_role over fake oracle text, asserting the exactly-99 / singleton /
# color-identity invariants hold on the FINAL deck after all three run).


# --- Lift-matrix fixtures -------------------------------------------------


def _lift_matrix(counts, pairs, names, n_decks=20):
    """Minimal lift-matrix dict in the shape lift_analysis emits/consumes."""
    return {
        "too_small": False, "n_decks": n_decks,
        "counts": counts, "pairs": pairs, "names": names, "bands": {},
    }


# A candidate ("goblin bombardment") that pairs above-chance with the
# commander and one in-deck ramp card, so lift_candidates surfaces it; the
# two ramp cards in the deck have NO deck-internal pairs (synergy 0), so the
# marginal one is the swap target.
_LIFT_NAMES = {
    "goblin bombardment": "Goblin Bombardment",
    "good ramp": "Good Ramp",
    "marginal ramp": "Marginal Ramp",
    "a draw": "A Draw",
    "krenko, mob boss": "Krenko, Mob Boss",
}
_LIFT_COUNTS = {
    "goblin bombardment": 4, "good ramp": 5, "marginal ramp": 3,
    "a draw": 5, "krenko, mob boss": 10,
}
# a < b keys; co >= SUPPORT_FLOOR (3). Candidate links to commander + good
# ramp (>= 2 supporting pairs). good ramp also links to the commander so it
# carries a little synergy — i.e. it is NOT the marginal ramp card.
_LIFT_PAIRS = {
    "goblin bombardment": {"good ramp": 3, "krenko, mob boss": 3},
    "good ramp": {"krenko, mob boss": 3},
}

_LIFT_ROLES = {
    "Goblin Bombardment": "ramp", "Good Ramp": "ramp",
    "Marginal Ramp": "ramp", "A Draw": "draw",
}


def test_lift_swap_replaces_marginal_same_role():
    nonlands = ["Good Ramp", "Marginal Ramp", "A Draw"]
    matrix = _lift_matrix(_LIFT_COUNTS, _LIFT_PAIRS, _LIFT_NAMES)
    out, notes, skipped = personalize.lift_swaps(
        nonlands, commander="Krenko, Mob Boss", bracket=3, matrix=matrix,
        reserved_keys=set(), role_of=lambda nm: _LIFT_ROLES.get(nm, "other"),
        ci_ok=lambda nm: True,
    )
    assert skipped is None
    # The marginal ramp card (no in-deck synergy) is swapped out; the
    # better-connected ramp card stays — role counts unchanged (still 2 ramp,
    # 1 draw).
    assert "Goblin Bombardment" in out
    assert "Marginal Ramp" not in out
    assert "Good Ramp" in out
    roles = [_LIFT_ROLES.get(n, "other") for n in out]
    assert roles.count("ramp") == 2 and roles.count("draw") == 1
    assert len(out) == len(nonlands)  # net-zero: exactly-99 preserved.
    assert notes and notes[0].startswith("swapped Marginal Ramp for Goblin")
    assert "lift" in notes[0].lower()


def test_lift_swap_too_small_corpus_skips_cleanly():
    nonlands = ["Good Ramp", "Marginal Ramp"]
    matrix = {"too_small": True, "n_decks": 4}
    out, notes, skipped = personalize.lift_swaps(
        nonlands, commander="Krenko, Mob Boss", bracket=3, matrix=matrix,
        reserved_keys=set(), role_of=lambda nm: "ramp", ci_ok=lambda nm: True,
    )
    assert out == nonlands  # untouched
    assert notes == []
    assert skipped and "too small" in skipped


def test_lift_swap_no_same_role_slot_preserves_counts():
    # Candidate is ramp, but the deck has no ramp card to trade like-for-like
    # (only a draw card) → no swap, role counts stay intact.
    nonlands = ["A Draw"]
    matrix = _lift_matrix(_LIFT_COUNTS, _LIFT_PAIRS, _LIFT_NAMES)
    out, notes, skipped = personalize.lift_swaps(
        nonlands, commander="Krenko, Mob Boss", bracket=3, matrix=matrix,
        reserved_keys=set(),
        role_of=lambda nm: "ramp" if "Ramp" in nm or "Bombardment" in nm
        else "draw",
        ci_ok=lambda nm: True,
    )
    assert out == nonlands
    assert notes == []


# --- Bracket steering -----------------------------------------------------

_GC_KEYS = {"gc a", "gc b", "gc c", "gc d", "gc e"}


def _gc(nm):
    return deck_builder.name_key(nm) in _GC_KEYS


def _estimate_by_gc(text):
    # Fake estimator: B2 baseline + 1 per Game Changer present (capped B5).
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    n = sum(1 for l in lines if l.lower() in _GC_KEYS)
    return {"estimate": min(5, 2 + n)}


def test_steer_raises_low_estimate_toward_target():
    nonlands = [f"Filler {i}" for i in range(10)]
    out, notes, est = personalize.steer_bracket(
        nonlands, target=4,
        render_fn=lambda nl: "\n".join(nl),
        estimate_fn=_estimate_by_gc, is_game_changer=_gc,
        is_fast_mana=lambda nm: False,
        candidate_pool=["GC A", "GC B", "GC C"],
        reserved_keys=set(), ci_ok=lambda nm: True,
        gc_cap=personalize.GC_CAP_BY_BRACKET[4],
    )
    assert est == 4  # reached the target
    assert len(out) == len(nonlands)  # net-zero
    n_gc = sum(1 for n in out if _gc(n))
    assert n_gc == 2  # two GCs took B2 -> B4


def test_steer_never_exceeds_gc_cap():
    # Target B3 (cap = 3 GCs), estimator that never reaches target, unlimited
    # GC candidates: the loop must stop at the cap, not pile GCs past it.
    nonlands = [f"Filler {i}" for i in range(10)]
    out, notes, est = personalize.steer_bracket(
        nonlands, target=3,
        render_fn=lambda nl: "\n".join(nl),
        estimate_fn=lambda text: {"estimate": 2},  # stuck below target
        is_game_changer=_gc, is_fast_mana=lambda nm: False,
        candidate_pool=["GC A", "GC B", "GC C", "GC D", "GC E"],
        reserved_keys=set(), ci_ok=lambda nm: True,
        gc_cap=personalize.GC_CAP_BY_BRACKET[3], max_iters=10,
    )
    n_gc = sum(1 for n in out if _gc(n))
    assert n_gc == 3  # exactly the B3 cap — never a 4th
    assert any("no in-cap power" in note for note in notes)


def test_steer_lowers_over_bracket_deck():
    # Deck opens with 3 GCs (est B5); target B2 → soften down to zero power.
    nonlands = ["GC A", "GC B", "GC C"] + [f"Filler {i}" for i in range(7)]
    out, notes, est = personalize.steer_bracket(
        nonlands, target=2,
        render_fn=lambda nl: "\n".join(nl),
        estimate_fn=_estimate_by_gc, is_game_changer=_gc,
        is_fast_mana=lambda nm: False,
        candidate_pool=["Benign X", "Benign Y", "Benign Z"],
        reserved_keys=set(), ci_ok=lambda nm: True,
        gc_cap=personalize.GC_CAP_BY_BRACKET[2],
    )
    assert est == 2
    assert sum(1 for n in out if _gc(n)) == 0  # all power trimmed away
    assert len(out) == len(nonlands)


# --- Collection bias ------------------------------------------------------


def test_collection_bias_prefers_owned_near_equivalent():
    coll = frozenset({"owned ramp"})
    out, notes = personalize.apply_collection_bias(
        ["Unowned Ramp"], collection=coll, owned_pool=["Owned Ramp"],
        ci_ok=lambda nm: True, role_of=lambda nm: "ramp",
        mv_of=lambda nm: 2.0, quality_of=lambda nm: 0.0,
        reserved_keys=set(),
    )
    assert out == ["Owned Ramp"]  # owned near-equivalent swapped in
    assert notes and "owned-bias" in notes[0]


def test_collection_bias_refuses_strict_downgrade():
    # Same role + cost, but the owned card is strictly worse (lower quality)
    # → the swap is refused; we never trade quality for ownership.
    coll = frozenset({"owned ramp"})
    quality = {"Unowned Ramp": 1.0, "Owned Ramp": 0.0}
    out, notes = personalize.apply_collection_bias(
        ["Unowned Ramp"], collection=coll, owned_pool=["Owned Ramp"],
        ci_ok=lambda nm: True, role_of=lambda nm: "ramp",
        mv_of=lambda nm: 2.0, quality_of=lambda nm: quality[nm],
        reserved_keys=set(),
    )
    assert out == ["Unowned Ramp"]  # untouched
    assert notes == []


def test_collection_bias_protects_power_cards():
    # An unowned power card (protected) is NOT traded away, even for a same-
    # role owned near-equivalent — steering's work must survive stage 3.
    coll = frozenset({"owned ramp"})
    out, notes = personalize.apply_collection_bias(
        ["GC A"], collection=coll, owned_pool=["Owned Ramp"],
        ci_ok=lambda nm: True, role_of=lambda nm: "ramp",
        mv_of=lambda nm: 2.0, quality_of=lambda nm: 0.0,
        reserved_keys=set(), protect=_gc,
    )
    assert out == ["GC A"]
    assert notes == []


# --- Integration through _assemble (all three stages, real classify_role) -

# Fake cards with oracle text that classify_role resolves to real roles.
_RAMP = {"type_line": "Sorcery", "color_identity": ["R"],
         "mana_cost": "{1}{R}", "oracle_text": "Add {R}{R}."}
_DRAW = {"type_line": "Sorcery", "color_identity": ["R"],
         "mana_cost": "{1}{R}", "oracle_text": "Draw a card."}
_REMOVAL = {"type_line": "Instant", "color_identity": ["R"],
            "mana_cost": "{1}{R}", "oracle_text": "Destroy target creature."}
_THREAT = {"type_line": "Creature — Goblin", "color_identity": ["R"],
           "mana_cost": "{1}{R}", "oracle_text": "Haste."}

_P13_CARDS = {
    "Krenko, Mob Boss": {
        "type_line": "Legendary Creature — Goblin",
        "color_identity": ["R"], "mana_cost": "{2}{R}{R}",
        "oracle_text": "Tap: create Goblins.",
    },
    "Command Tower": {"type_line": "Land", "color_identity": [],
                      "mana_cost": "", "oracle_text": ""},
    "Goblin Bombardment": dict(_RAMP),   # ramp (lift candidate)
    "Fast GC": dict(_RAMP),              # power add (steer)
    "Owned Draw": dict(_DRAW),           # owned near-equivalent (collection)
}


def _p13_lookup(name):
    if name in _P13_CARDS:
        return _P13_CARDS[name]
    if name.startswith("Ramp "):
        return dict(_RAMP)
    if name.startswith("Draw "):
        return dict(_DRAW)
    if name.startswith("Removal "):
        return dict(_REMOVAL)
    if name.startswith("Threat "):
        return dict(_THREAT)
    return None


def _p13_seed():
    # commander + 40 role-typed nonlands + a nonbasic land the seed keeps.
    return (
        ["Krenko, Mob Boss"]
        + [f"Ramp {i}" for i in range(10)]
        + [f"Draw {i}" for i in range(10)]
        + [f"Removal {i}" for i in range(10)]
        + [f"Threat {i}" for i in range(10)]
        + ["Command Tower"]
    )


def _estimate_by_fastgc(text):
    # Integration estimator: B2 baseline + 1 per "Fast GC" (the power card
    # the steer stage sources from ``power_pool``), so the estimate actually
    # moves when steering adds it. Capped at B5.
    lines = [l.strip().lower() for l in text.splitlines() if l.strip()]
    n = sum(1 for l in lines if l == "1 fast gc")
    return {"estimate": min(5, 2 + n)}


def _p13_matrix():
    names = {"goblin bombardment": "Goblin Bombardment",
             "ramp 0": "Ramp 0", "ramp 1": "Ramp 1",
             "krenko, mob boss": "Krenko, Mob Boss"}
    counts = {"goblin bombardment": 4, "ramp 0": 5, "ramp 1": 5,
              "krenko, mob boss": 10}
    pairs = {"goblin bombardment": {"krenko, mob boss": 3, "ramp 0": 3,
                                    "ramp 1": 3}}
    return _lift_matrix(counts, pairs, names)


def _assert_invariants(text, ci_letters="R"):
    """The three hard invariants on a rendered deck."""
    assert count_main_cards(text) == 99
    mains = main_card_quantities(text)
    # Singleton: every nonbasic sits at qty 1 (basics may stack).
    basics = {"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"}
    for nm, qty in mains.items():
        if nm not in basics:
            assert qty == 1, f"{nm} broke singleton ({qty})"
    # Commander never leaks into the mainboard.
    assert "Krenko, Mob Boss" not in mains


def test_assemble_all_stages_preserve_invariants(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _p13_lookup,
    )
    coll_file = tmp_path / "collection.txt"
    coll_file.write_text("Owned Draw\n", encoding="utf-8")

    result = _assemble(
        "Krenko, Mob Boss", 4, coll_file,
        fetch_avg=lambda c, b: _avg(_p13_seed()),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "R",
        lookup=_p13_lookup,
        name="Krenko",
        lift_matrix=_p13_matrix(),
        estimate_fn=_estimate_by_fastgc,
        is_game_changer=lambda nm: deck_builder.name_key(nm) == "fast gc",
        is_fast_mana=lambda nm: False,
        power_pool=["Fast GC"],
        owned_names=["Owned Draw"],
    )
    # Invariants hold on the FINAL deck (after lift + steer + collection).
    _assert_invariants(result.text)
    mains = main_card_quantities(result.text)

    # Stage 1 (lift): the marginal ramp card was traded for the candidate.
    assert result.lift_swaps
    assert "Goblin Bombardment" in mains
    assert "Ramp 0" not in mains

    # Stage 2 (steer): a power card was added toward the B4 target.
    assert "Fast GC" in mains
    assert result.bracket_estimate == 3  # B2 + one power add
    assert result.bracket_target == 4

    # Stage 3 (collection): an owned near-equivalent replaced an unowned draw.
    assert result.owned_swaps
    assert "Owned Draw" in mains
    # Buy-list reports the still-unowned cards and excludes the owned one.
    assert result.buy_list
    assert "Owned Draw" not in result.buy_list


def test_assemble_toggles_disable_each_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _p13_lookup,
    )
    coll_file = tmp_path / "collection.txt"
    coll_file.write_text("Owned Draw\n", encoding="utf-8")

    result = _assemble(
        "Krenko, Mob Boss", 4, coll_file,
        fetch_avg=lambda c, b: _avg(_p13_seed()),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "R",
        lookup=_p13_lookup,
        name="Krenko",
        lift_matrix=_p13_matrix(),
        estimate_fn=_estimate_by_fastgc,
        is_game_changer=lambda nm: deck_builder.name_key(nm) == "fast gc",
        is_fast_mana=lambda nm: False,
        power_pool=["Fast GC"],
        owned_names=["Owned Draw"],
        enable_lift=False,
        enable_steer=False,
        owned_bias=False,
    )
    _assert_invariants(result.text)
    mains = main_card_quantities(result.text)
    # Every stage disabled → none of their effects present.
    assert result.lift_swaps == [] and result.lift_skipped is None
    assert "Goblin Bombardment" not in mains
    assert result.steer_notes == [] and result.bracket_estimate is None
    assert "Fast GC" not in mains
    assert result.owned_swaps == []
    assert "Owned Draw" not in mains
    assert result.buy_list == []  # owned-bias off → no buy-list either


def test_assemble_lift_skips_when_corpus_too_small(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _p13_lookup,
    )
    result = _assemble(
        "Krenko, Mob Boss", 3,
        fetch_avg=lambda c, b: _avg(_p13_seed()),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "R",
        lookup=_p13_lookup,
        name="Krenko",
        lift_matrix={"too_small": True, "n_decks": 5},
        enable_steer=False,
    )
    _assert_invariants(result.text)
    assert result.lift_swaps == []
    assert result.lift_skipped and "too small" in result.lift_skipped

"""corpus_themes tests — offline fixtures, stubbed oracle lookups.

Everything runs against a synthetic corpus written into tmp dirs and a
canned card DB injected through the ``lookup`` seams (the same offline
pattern as test_deck_builder). No network, no real deck dir, no real
oracle snapshots — ``default_lookup`` is monkeypatched wherever a code
path would otherwise reach for the on-disk snapshot store.
"""
import json
import random

import pytest

from commander_builder import corpus_themes as ct
from commander_builder import deck_builder
from commander_builder.dck_utils import count_main_cards, main_card_quantities


# --- Fake card DB ---------------------------------------------------------
#
# Oracle texts are crafted against staples._ROLE_PATTERNS / the
# _THEME_MOTIFS table so each family lands in a known role bucket and
# theme motif.

_FAKE_CARDS: dict[str, dict] = {
    "Grix, Goblin Boss": {
        "type_line": "Legendary Creature — Goblin Warrior",
        "oracle_text": "Other Goblins you control get +1/+1.",
        "color_identity": ["R"], "mana_cost": "{2}{R}{R}", "cmc": 4.0,
    },
    "Command Tower": {
        "type_line": "Land", "oracle_text": "", "color_identity": [],
        "mana_cost": "", "cmc": 0.0,
    },
}


def _add(name, type_line, oracle, ci, cost, cmc):
    _FAKE_CARDS[name] = {
        "type_line": type_line, "oracle_text": oracle,
        "color_identity": ci, "mana_cost": cost, "cmc": cmc,
    }


for _i in range(45):
    _add(f"Goblin Grunt {_i}", "Creature — Goblin", "Haste.",
         ["R"], "{1}{R}", 2.0)                      # role: threat
for _i in range(12):
    _add(f"Test Signet {_i}", "Artifact",
         "{T}: Add one mana of any color.", [], "{2}", 2.0)   # role: ramp
for _i in range(12):
    _add(f"Divine Study {_i}", "Enchantment",
         "Whenever you cast a spell, draw a card.",
         ["U"], "{2}{U}", 3.0)                      # role: draw
for _i in range(12):
    _add(f"Zap {_i}", "Instant", "Destroy target creature.",
         ["R"], "{1}{R}", 2.0)                      # role: removal
for _i in range(6):
    _add(f"Spell Prof {_i}", "Creature — Human Wizard",
         "Whenever you cast an instant or sorcery spell, copy it.",
         ["U"], "{2}{U}", 3.0)                      # motif: spellslinger
for _i in range(12):
    _add(f"Sorcery Lesson {_i}", "Sorcery",
         "Copy target instant or sorcery spell.",
         ["U"], "{1}{U}", 2.0)                      # motif: spellslinger
for _i in range(12):
    _add(f"Grave Call {_i}", "Sorcery",
         "Return target creature card from your graveyard to the "
         "battlefield.",
         ["B"], "{3}{B}", 4.0)                      # motif: graveyard
# Cluster-signature candidates for the builder-steering tests.
_add("Sig Draw Engine", "Enchantment",
     "At the beginning of your upkeep, draw a card.", ["R"], "{2}{R}", 3.0)
_add("Sig Ramp Rock", "Artifact", "{T}: Add one mana of any color.",
     [], "{2}", 2.0)
_add("Off Color Sig", "Enchantment",
     "At the beginning of your upkeep, draw a card.", ["G"], "{2}{G}", 3.0)


def _fake_lookup(name):
    card = _FAKE_CARDS.get(name)
    return dict(card) if card else None


# --- Deck-file helpers ----------------------------------------------------


def _write_deck(path, commander, mains):
    lines = ["[metadata]", f"Name={path.stem}", "[Commander]",
             f"1 {commander}", "[Main]"]
    lines += [f"1 {nm}" for nm in mains]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _tribal_main(n_goblins=20):
    return (
        [f"Goblin Grunt {i}" for i in range(n_goblins)]
        + [f"Test Signet {i}" for i in range(8)]
        + [f"Zap {i}" for i in range(4)]
        + ["Command Tower"] + ["Mountain"] * 34
    )


def _spellslinger_main():
    # Mostly noncreature (the realistic spellslinger shape) — the 6 Wizards
    # stay below TRIBAL_MIN so the motif rung gets to speak.
    return (
        [f"Sorcery Lesson {i}" for i in range(12)]
        + [f"Spell Prof {i}" for i in range(6)]
        + [f"Divine Study {i}" for i in range(10)]
        + [f"Test Signet {i}" for i in range(6)]
        + ["Island"] * 36
    )


def _plain_main():
    return (
        [f"Goblin Grunt {i}" for i in range(6)]
        + [f"Test Signet {i}" for i in range(6)]
        + [f"Divine Study {i}" for i in range(6)]
        + [f"Zap {i}" for i in range(6)]
        + ["Mountain"] * 35
    )


# --- Profile extraction ---------------------------------------------------


def test_profile_extracts_roles_curve_lands_and_tribes(tmp_path):
    deck = _write_deck(tmp_path / "Gob [B3].dck", "Grix, Goblin Boss",
                       _tribal_main())
    p = ct.profile_deck(deck, _fake_lookup)
    assert p is not None
    # 34 Mountains + Command Tower = 35 lands.
    assert p.land_count == 35
    # 20 grunts are threats; 8 signets ramp; 4 zaps removal.
    assert p.role_counts.get("threat") == 20
    assert p.role_counts.get("ramp") == 8
    assert p.role_counts.get("removal") == 4
    # Tribe counts include the commander's own Goblin subtype.
    assert p.tribes.get("Goblin") == 21
    # Curve: the 99's nonlands are all cmc 2; the commander is EXCLUDED
    # from structural counts (norms describe the 99, not the zone).
    assert p.curve["2"] == 32
    assert p.curve["4"] == 0
    assert p.cmc_mean == pytest.approx(2.0, abs=0.01)
    assert p.color_count == 1  # mono-R fixture (Signets/Tower colorless)


def test_profile_unresolved_cards_counted_not_fatal(tmp_path):
    deck = _write_deck(tmp_path / "Mystery.dck", "Grix, Goblin Boss",
                       ["Totally Unknown Card", "Goblin Grunt 0",
                        "Mountain"])
    p = ct.profile_deck(deck, _fake_lookup)
    assert p.unresolved == 1
    assert p.role_counts.get("threat") == 1  # commander excluded from roles
    assert p.land_count == 1  # basics resolve without any lookup


def test_file_role_prefixes():
    assert ct.file_role("[USER] Mine [B3].dck") == "user"
    assert ct.file_role("[CONTROL] Blank.dck") == "control"
    assert ct.file_role("[PREMADE] Popular [B3].dck") == "premade"
    assert ct.file_role("[REF] Import.dck") == "ref"
    assert ct.file_role("Harvested Pool Deck [B3].dck") == "pool"


def test_scan_corpus_skips_user_and_control_by_default(tmp_path):
    _write_deck(tmp_path / "Pool [B3].dck", "Grix, Goblin Boss",
                _tribal_main())
    _write_deck(tmp_path / "[PREMADE] Pop [B3].dck", "Grix, Goblin Boss",
                _tribal_main())
    _write_deck(tmp_path / "[USER] Mine [B3].dck", "Grix, Goblin Boss",
                _tribal_main())
    _write_deck(tmp_path / "[CONTROL] Blank.dck", "Grix, Goblin Boss",
                _tribal_main())
    profiles = ct.scan_corpus(tmp_path, lookup=_fake_lookup)
    assert sorted(p.filename for p in profiles) == [
        "Pool [B3].dck", "[PREMADE] Pop [B3].dck",
    ]


# --- Clustering -----------------------------------------------------------


def test_tribal_cluster_and_reason(tmp_path):
    deck = _write_deck(tmp_path / "Gob.dck", "Grix, Goblin Boss",
                       _tribal_main())
    p = ct.profile_deck(deck, _fake_lookup)
    label, reason = ct._classify(p)
    assert label == "tribal-goblin"
    assert "Goblin" in reason


def test_spellslinger_cluster(tmp_path):
    deck = _write_deck(tmp_path / "Slinger.dck", "Grix, Goblin Boss",
                       _spellslinger_main())
    p = ct.profile_deck(deck, _fake_lookup)
    label, _ = ct._classify(p)
    assert label == "spellslinger"


def test_goodstuff_fallback(tmp_path):
    deck = _write_deck(tmp_path / "Plain.dck", "Grix, Goblin Boss",
                       _plain_main())
    p = ct.profile_deck(deck, _fake_lookup)
    label, reason = ct._classify(p)
    assert label == "goodstuff-midrange"
    assert reason == "no dominant theme signal"


def test_cluster_assignment_is_deterministic(tmp_path):
    """Same cards, any order, repeated runs → identical cluster."""
    mains = _tribal_main()
    labels = set()
    rng = random.Random(7)
    for i in range(3):
        shuffled = list(mains)
        rng.shuffle(shuffled)
        deck = _write_deck(tmp_path / f"Gob{i}.dck", "Grix, Goblin Boss",
                           shuffled)
        p = ct.profile_deck(deck, _fake_lookup)
        labels.add(ct._classify(p)[0])
    assert labels == {"tribal-goblin"}


# --- Norms math -----------------------------------------------------------


def _clustered_profiles(tmp_path, n_tribal=4, n_plain=2):
    for i in range(n_tribal):
        # Vary land counts so the median is a real median: 33,35,37,...
        mains = _tribal_main()[:-34] + ["Mountain"] * (33 + 2 * i - 1)
        _write_deck(tmp_path / f"Gob {i} [B3].dck", "Grix, Goblin Boss",
                    mains)
    for i in range(n_plain):
        _write_deck(tmp_path / f"Plain {i} [B3].dck", "Grix, Goblin Boss",
                    _plain_main())
    profiles = ct.scan_corpus(tmp_path, lookup=_fake_lookup)
    return ct.cluster_profiles(profiles)


def test_norms_medians_and_cluster_sizes(tmp_path):
    profiles = _clustered_profiles(tmp_path)
    norms = ct.compute_norms(profiles, deck_dir=str(tmp_path))
    assert norms["n_decks"] == 6
    tribal = norms["clusters"]["tribal-goblin"]
    assert tribal["n_decks"] == 4
    # Lands: 33, 35, 37, 39 (Command Tower + varying Mountains) → median 36.
    assert tribal["land_median"] == 36
    assert tribal["role_medians"]["ramp"] == 8
    assert tribal["role_medians"]["removal"] == 4
    plain = norms["clusters"]["goodstuff-midrange"]
    assert plain["n_decks"] == 2


def test_signature_cards_high_in_cluster_low_global(tmp_path):
    profiles = _clustered_profiles(tmp_path, n_tribal=4, n_plain=4)
    norms = ct.compute_norms(profiles)
    sigs = {s["card"] for s in
            norms["clusters"]["tribal-goblin"]["signature_cards"]}
    # Goblin Grunt 10+ appear in every tribal deck (freq 1.0) and no plain
    # deck (global 0.5) → ratio 2.0, kept. Test Signet 0 appears in ALL
    # decks (ratio 1.0) → excluded: ubiquity is not signature.
    assert "Goblin Grunt 10" in sigs
    assert "Test Signet 0" not in sigs


def test_thin_cluster_gets_no_signatures(tmp_path):
    profiles = _clustered_profiles(tmp_path, n_tribal=2, n_plain=4)
    norms = ct.compute_norms(profiles)
    tribal = norms["clusters"]["tribal-goblin"]
    assert tribal["n_decks"] == 2  # below SIGNATURE_MIN_DECKS
    assert tribal["signature_cards"] == []


def test_norms_artifact_roundtrip(tmp_path):
    profiles = _clustered_profiles(tmp_path)
    norms = ct.compute_norms(profiles)
    out = tmp_path / "artifact" / "norms.json"
    ct.write_norms(norms, out)
    loaded = ct.load_norms(out)
    assert loaded == json.loads(json.dumps(norms))  # JSON-stable


def test_load_norms_absent_or_corrupt_is_none(tmp_path):
    assert ct.load_norms(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ct.load_norms(bad) is None
    not_norms = tmp_path / "not_norms.json"
    not_norms.write_text('{"clusters": 3}', encoding="utf-8")
    assert ct.load_norms(not_norms) is None


# --- Blending math --------------------------------------------------------


def test_blended_role_targets_average_template_and_empirical():
    cluster = {"role_medians": {"ramp": 6, "draw": 12}}
    blended = ct.blended_role_targets(cluster)
    assert blended["ramp"] == 8       # (10 + 6) / 2
    assert blended["draw"] == 11      # (10 + 12) / 2
    # Roles the cluster didn't measure keep the template number.
    assert blended["removal"] == 8


def test_blended_land_target_blends_and_clamps():
    assert ct.blended_land_target(35, {"land_median": 31}) == 33
    assert ct.blended_land_target(35, {"land_median": None}) == 35
    assert ct.blended_land_target(35, {}) == 35
    # Degenerate medians clamp to the sane band.
    assert ct.blended_land_target(35, {"land_median": 5}) == ct.LAND_TARGET_MIN


# --- norms_steer (unit) ---------------------------------------------------


def _role_of(nm):
    card = _fake_lookup(nm)
    if not card:
        return "unknown"
    from commander_builder.staples import classify_role_extended
    return classify_role_extended(card["oracle_text"], card["type_line"])


def _mv_of(nm):
    card = _fake_lookup(nm)
    return card["cmc"] if card else None


def test_norms_steer_fills_deficit_from_signatures():
    nonlands = [f"Goblin Grunt {i}" for i in range(20)] + [
        f"Test Signet {i}" for i in range(8)
    ]
    cluster = {
        "role_medians": {"draw": 10},
        "cmc_mean_median": 2.0,
        "signature_cards": [{"card": "Sig Draw Engine"}],
    }
    new, notes = ct.norms_steer(
        nonlands, label="tribal-goblin", cluster=cluster,
        role_of=_role_of, ci_ok=lambda nm: True, reserved_keys=set(),
        mv_of=_mv_of,
    )
    assert "Sig Draw Engine" in new
    assert len(new) == len(nonlands)               # net-zero swap
    assert len(set(new)) == len(new)               # singleton preserved
    assert notes and "Sig Draw Engine" in notes[0]
    assert "tribal-goblin" in notes[0]
    # Donor was the surplus threat bucket, not the ramp bucket.
    assert all(f"Test Signet {i}" in new for i in range(8))


def test_norms_steer_respects_ci_and_singleton_and_budget():
    nonlands = [f"Goblin Grunt {i}" for i in range(20)] + ["Sig Draw Engine"]
    cluster = {
        "role_medians": {"draw": 10},
        "cmc_mean_median": 2.0,
        "signature_cards": [
            {"card": "Sig Draw Engine"},   # already in deck → skipped
            {"card": "Off Color Sig"},     # fails ci_ok → skipped
        ],
    }
    new, notes = ct.norms_steer(
        nonlands, label="x", cluster=cluster, role_of=_role_of,
        ci_ok=lambda nm: "Off Color" not in nm, reserved_keys=set(),
        mv_of=_mv_of,
    )
    assert new == nonlands
    assert notes == []


def test_norms_steer_never_overshoots_target():
    # Deck already at the blended draw target → no swap even with a
    # signature draw card on offer.
    nonlands = [f"Divine Study {i}" for i in range(12)] + [
        f"Goblin Grunt {i}" for i in range(10)
    ]
    cluster = {
        "role_medians": {"draw": 10},   # blended target = (10+10)/2 = 10
        "cmc_mean_median": 3.0,
        "signature_cards": [{"card": "Sig Draw Engine"}],
    }
    new, notes = ct.norms_steer(
        nonlands, label="x", cluster=cluster, role_of=_role_of,
        ci_ok=lambda nm: True, reserved_keys=set(), mv_of=_mv_of,
    )
    assert new == nonlands and notes == []


def test_norms_steer_no_signatures_is_noop():
    nonlands = [f"Goblin Grunt {i}" for i in range(5)]
    new, notes = ct.norms_steer(
        nonlands, label="x", cluster={"role_medians": {"draw": 10}},
        role_of=_role_of, ci_ok=lambda nm: True, reserved_keys=set(),
        mv_of=_mv_of,
    )
    assert new == nonlands and notes == []


# --- cluster_for_shell gating --------------------------------------------


def _norms_with(label, n_decks, **extra):
    cluster = {"n_decks": n_decks, "role_medians": {}, "land_median": 35,
               "cmc_mean_median": 2.5, "signature_cards": []}
    cluster.update(extra)
    return {"version": 1, "n_decks": n_decks, "clusters": {label: cluster}}


def test_cluster_for_shell_matches_measured_cluster():
    shell = [f"Goblin Grunt {i}" for i in range(20)]
    norms = _norms_with("tribal-goblin", 5)
    label, cluster = ct.cluster_for_shell(
        norms, ["Grix, Goblin Boss"], shell, _fake_lookup,
    )
    assert label == "tribal-goblin"
    assert cluster["n_decks"] == 5


def test_cluster_for_shell_thin_or_absent_cluster_is_none():
    shell = [f"Goblin Grunt {i}" for i in range(20)]
    # Cluster measured over too few decks → no authority.
    label, cluster = ct.cluster_for_shell(
        _norms_with("tribal-goblin", 2), ["Grix, Goblin Boss"], shell,
        _fake_lookup,
    )
    assert (label, cluster) == (None, None)
    # Shell's cluster absent from the artifact entirely.
    label, cluster = ct.cluster_for_shell(
        _norms_with("spellslinger", 9), ["Grix, Goblin Boss"], shell,
        _fake_lookup,
    )
    assert (label, cluster) == (None, None)


def test_cluster_for_shell_goodstuff_never_matches():
    shell = ([f"Goblin Grunt {i}" for i in range(5)]
             + [f"Divine Study {i}" for i in range(5)])
    norms = _norms_with("goodstuff-midrange", 50)
    label, cluster = ct.cluster_for_shell(
        norms, ["Grix, Goblin Boss"], shell, _fake_lookup,
    )
    assert (label, cluster) == (None, None)


# --- Builder integration --------------------------------------------------


@pytest.fixture()
def _offline_ci(monkeypatch):
    """Route ``enforce_color_identity``'s call-time Scryfall resolution
    through the fake DB (the same isolation move test_deck_builder makes
    for every CI-filter test) — the ``ci_ok`` closures inside
    ``_personalize`` reach for ``scryfall_client.lookup_card`` directly."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake_lookup,
    )


def _seed_cards():
    # 69 nonlands (> the 66-slot budget at a 33-land target) so the land
    # count is decided by the target, not by seed scarcity.
    return (
        ["Grix, Goblin Boss"]
        + [f"Goblin Grunt {i}" for i in range(45)]
        + [f"Test Signet {i}" for i in range(12)]
        + [f"Zap {i}" for i in range(12)]
        + ["Command Tower", "Mountain", "Mountain"]
    )


def _build(**kwargs):
    from types import SimpleNamespace
    from commander_builder.edhrec_client import CardEntry
    avg = SimpleNamespace(cards=[CardEntry(name=n) for n in _seed_cards()])
    return deck_builder._assemble(
        "Grix, Goblin Boss", 3,
        fetch_avg=lambda c, b: avg,
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "R",
        lookup=_fake_lookup,
        name="Grix",
        enable_lift=False,
        enable_steer=False,
        **kwargs,
    )


def _steering_norms():
    return {
        "version": 1,
        "n_decks": 10,
        "clusters": {
            "tribal-goblin": {
                "n_decks": 6,
                "role_medians": {"draw": 10, "ramp": 8},
                "land_median": 31,
                "cmc_mean_median": 2.0,
                "signature_cards": [
                    {"card": "Sig Draw Engine"},
                    {"card": "Off Color Sig"},   # green: must be CI-dropped
                ],
            },
        },
    }


def test_builder_steers_toward_injected_norms(_offline_ci):
    result = _build(corpus_norms=_steering_norms())
    assert result.corpus_cluster == "tribal-goblin"
    assert any("matched corpus cluster" in n for n in result.corpus_notes)
    # Land target blending: model says 35 (mv 2.06), cluster median 31 →
    # blended 33; the note records the move.
    assert any("land target 35 -> 33" in n for n in result.corpus_notes)
    assert result.manabase.land_count == 33
    # Role steer: shell has zero draw → the cluster's signature draw
    # engine is swapped in from the threat surplus...
    mains = main_card_quantities(result.text)
    assert mains.get("Sig Draw Engine") == 1
    assert any("Sig Draw Engine" in n for n in result.corpus_notes)
    # ...but the off-color signature card never enters a mono-R deck.
    assert "Off Color Sig" not in mains
    # Invariants hold end to end.
    assert count_main_cards(result.text) == 99


def test_builder_without_norms_is_clean_noop(_offline_ci):
    baseline = _build(enable_corpus_norms=False)
    default = _build()  # flag off (conftest strips the env), no artifact
    assert default.text == baseline.text
    assert default.corpus_cluster is None
    assert default.corpus_notes == []


def test_builder_flag_on_but_artifact_absent_is_noop(_offline_ci, tmp_path, monkeypatch):
    monkeypatch.setenv(ct.FLAG_ENV, "1")
    monkeypatch.setattr(ct, "DEFAULT_NORMS_PATH",
                        tmp_path / "no_such_norms.json")
    baseline = _build(enable_corpus_norms=False)
    result = _build()
    assert result.text == baseline.text
    assert result.corpus_cluster is None


def test_builder_flag_on_reads_default_artifact(_offline_ci, tmp_path, monkeypatch):
    monkeypatch.setenv(ct.FLAG_ENV, "1")
    path = tmp_path / "norms.json"
    ct.write_norms(_steering_norms(), path)
    monkeypatch.setattr(ct, "DEFAULT_NORMS_PATH", path)
    result = _build()
    assert result.corpus_cluster == "tribal-goblin"


# --- Report + CLI ---------------------------------------------------------


def test_report_smoke(tmp_path):
    profiles = _clustered_profiles(tmp_path)
    report = ct.format_report(ct.compute_norms(profiles,
                                               deck_dir=str(tmp_path)))
    assert "Corpus theme mining — 6 decks" in report
    assert "tribal-goblin" in report
    assert "goodstuff-midrange" in report
    assert "signature cards" in report
    assert "lands" in report


def test_cli_writes_artifact_and_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ct, "default_lookup", _fake_lookup)
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    for i in range(4):
        _write_deck(deck_dir / f"Gob {i} [B3].dck", "Grix, Goblin Boss",
                    _tribal_main())
    _write_deck(deck_dir / "[USER] Mine [B3].dck", "Grix, Goblin Boss",
                _tribal_main())
    out = tmp_path / "norms.json"
    rc = ct.main(["--deck-dir", str(deck_dir), "--out", str(out),
                  "--report"])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "Wrote" in printed and "tribal-goblin" in printed
    norms = ct.load_norms(out)
    assert norms["n_decks"] == 4  # [USER] excluded by default


def test_cli_empty_dir_exits_1(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = ct.main(["--deck-dir", str(empty), "--no-write"])
    assert rc == 1
    assert "No corpus decks" in capsys.readouterr().out


def test_cli_rejects_unknown_roles(tmp_path):
    with pytest.raises(SystemExit):
        ct.main(["--deck-dir", str(tmp_path), "--roles", "pool,bogus"])

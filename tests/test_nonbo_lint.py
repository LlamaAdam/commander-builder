"""nonbo_lint unit tests — FP-019.5 §14 self-conflict table.

All card data injected via a stub lookup; fully offline.
"""
from commander_builder.nonbo_lint import (
    NONBO_RULES,
    lint_cards,
    lint_deck_text,
)


def _card(type_line="Instant", oracle="", toughness=None):
    out = {"type_line": type_line, "oracle_text": oracle}
    if toughness is not None:
        out["toughness"] = toughness
    return out


STUB = {
    "Skullclamp": _card("Artifact — Equipment",
                        "Equipped creature gets +1/-1."),
    "Glorious Anthem": _card("Enchantment",
                             "Creatures you control get +1/+1."),
    "Cursed Totem": _card("Artifact",
                          "Activated abilities of creatures can't be "
                          "activated."),
    "Llanowar Elves": _card("Creature — Elf Druid", "{T}: Add {G}.", "1"),
    "Thassa's Oracle": _card("Creature — Merfolk Wizard",
                             "When Thassa's Oracle enters..."),
    "Esper Sentinel": _card("Artifact Creature — Human Soldier",
                            "Whenever an opponent casts...", "1"),
    "Heartless Summoning": _card("Enchantment",
                                 "Creature spells you cast cost {2} less "
                                 "to cast. Creatures you control get "
                                 "-1/-1."),
    "Coat of Arms": _card("Artifact", "Each creature gets +1/+1 for..."),
    "Shield of Shrouds": _card("Enchantment",
                               "Creatures you control have shroud."),
    "Giant Growth": _card("Instant",
                          "Target creature you control gets +3/+3."),
    "Divination": _card("Sorcery", "Draw two cards."),
}


def _lookup(name):
    return STUB.get(name)


# --- pair rules --------------------------------------------------------------

def test_skullclamp_anthem_pair_fires():
    findings = lint_cards(["Skullclamp", "Glorious Anthem"], lookup=_lookup)
    (f,) = [x for x in findings if x["rule"] == "skullclamp_vs_anthems"]
    assert f["severity"] == "warn"
    assert f["cards_a"] == ["Skullclamp"]
    assert f["cards_b"] == ["Glorious Anthem"]
    assert f["source"] == "Lathril primer"


def test_pair_rules_need_both_sides():
    assert lint_cards(["Skullclamp", "Divination"], lookup=_lookup) == []
    assert lint_cards(["Glorious Anthem"], lookup=_lookup) == []


def test_cursed_totem_flags_own_dorks():
    findings = lint_cards(["Cursed Totem", "Llanowar Elves"], lookup=_lookup)
    (f,) = [x for x in findings if x["rule"] == "cursed_totem_vs_own_dorks"]
    assert f["cards_b"] == ["Llanowar Elves"]


def test_heartless_summoning_flags_one_toughness_only():
    hit = lint_cards(["Heartless Summoning", "Llanowar Elves"],
                     lookup=_lookup)
    assert any(f["rule"] == "heartless_summoning_vs_x1_creatures"
               for f in hit)
    miss = lint_cards(["Heartless Summoning", "Divination"], lookup=_lookup)
    assert not any(f["rule"] == "heartless_summoning_vs_x1_creatures"
                   for f in miss)


def test_forced_draw_vs_oracle_name_pair():
    findings = lint_cards(["Thassa's Oracle", "Esper Sentinel"],
                          lookup=_lookup)
    (f,) = [x for x in findings
            if x["rule"] == "forced_draw_vs_thassas_oracle"]
    assert "Silence" in f["why"]


def test_shroud_vs_own_targeting_is_pattern_based():
    findings = lint_cards(["Shield of Shrouds", "Giant Growth"],
                          lookup=_lookup)
    (f,) = [x for x in findings if x["rule"] == "own_shroud_vs_own_targeting"]
    assert f["severity"] == "note"


def test_card_cannot_satisfy_both_sides_alone():
    # A hypothetical card that is both a shroud-granter and a targeter
    # must not pair with itself.
    both = {"Weird Aura": _card(
        "Enchantment",
        "Creatures you control have shroud. Target creature you control "
        "gets +1/+1.")}
    findings = lint_cards(["Weird Aura"], lookup=lambda n: both.get(n))
    assert findings == []


# --- single-selector rules ---------------------------------------------------

def test_symmetric_effects_fire_alone_as_notes():
    findings = lint_cards(["Coat of Arms"], lookup=_lookup)
    (f,) = findings
    assert f["rule"] == "coat_of_arms_symmetry"
    assert f["severity"] == "note"
    assert f["cards_b"] == []


# --- robustness --------------------------------------------------------------

def test_unresolvable_cards_match_nothing():
    findings = lint_cards(["Skullclamp", "Unknown Card X"],
                          lookup=lambda n: STUB.get(n))
    assert findings == []  # pattern side never resolves -> no pair


def test_lookup_exception_degrades_to_silence():
    def boom(name):
        raise RuntimeError("scryfall down")
    assert lint_cards(["Skullclamp", "Glorious Anthem"], lookup=boom) == []


def test_warns_sort_before_notes():
    findings = lint_cards(
        ["Coat of Arms", "Skullclamp", "Glorious Anthem"], lookup=_lookup)
    assert [f["severity"] for f in findings] == ["warn", "note"]


def test_lint_deck_text_reads_both_sections():
    deck = ("[Commander]\n1 Thassa's Oracle\n"
            "[Main]\n1 Esper Sentinel\n1 Divination\n")
    findings = lint_deck_text(deck, lookup=_lookup)
    assert any(f["rule"] == "forced_draw_vs_thassas_oracle"
               for f in findings)


def test_rule_table_is_well_formed():
    seen = set()
    for rule in NONBO_RULES:
        assert rule["id"] not in seen
        seen.add(rule["id"])
        assert rule["severity"] in ("warn", "note")
        assert rule["why"] and rule["source"]
        assert rule["a"] is not None


# --- deck_health wiring ------------------------------------------------------

def test_deck_health_exposes_nonbo_tile(monkeypatch):
    import commander_builder.deck_health as dh
    import commander_builder.nonbo_lint as nl

    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_kw: {"name": name, "type_line": "Creature",
                             "mana_cost": "{1}", "oracle_text": "", "cmc": 1.0},
    )
    monkeypatch.setattr(
        nl, "lint_deck_text",
        lambda text, lookup=None: [{"rule": "x", "severity": "warn",
                                    "cards_a": ["A"], "cards_b": ["B"],
                                    "why": "w", "source": "s"}],
    )
    out = dh.compute_deck_health("[Main]\n1 Forest\n")
    assert out["nonbos"][0]["rule"] == "x"


def test_deck_health_nonbo_signal_degrades_to_none(monkeypatch):
    import commander_builder.deck_health as dh
    import commander_builder.nonbo_lint as nl

    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_kw: {"name": name, "type_line": "Creature",
                             "mana_cost": "{1}", "oracle_text": "", "cmc": 1.0},
    )

    def boom(*a, **k):
        raise RuntimeError("outage")

    monkeypatch.setattr(nl, "lint_deck_text", boom)
    out = dh.compute_deck_health("[Main]\n1 Forest\n")
    assert out["nonbos"] is None

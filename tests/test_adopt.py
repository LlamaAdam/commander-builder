"""FP-018.3 — ``commander adopt``: grounded explanation + gentle personalization.

OFFLINE ONLY: every test injects ``lookup`` (an in-memory oracle table)
and ``matrix`` (the same minimal lift-matrix shape test_deck_builder
uses); nothing touches Scryfall, EDHREC, or the network.

The two load-bearing pins:

* ``test_rebuild_tier_is_structurally_unreachable`` — the polish cap
  holds against an explicit bigger ask AND the rebuild env opt-in
  (there must be no code path that widens the budget);
* ``test_only_explicit_protect_lines_prevent_primer_linked_cuts`` —
  primer links remain evidence while only the user's explicit
  ``Protect=`` metadata locks a card.
"""
from __future__ import annotations

import json
from pathlib import Path

from commander_builder import adopt, primer
from commander_builder.adopt import (
    POLISH_MAX_SWAPS,
    adopt_deck,
    explain_deck,
    personalize_suggestions,
    render_adoption,
)
from commander_builder.change_budget import TIER_CAPS


# --------------------------------------------------------------------------- #
# Offline doubles
# --------------------------------------------------------------------------- #

#: In-memory oracle table (constructed test data, not captures — the
#: shape is scryfall_client's documented card dict).
ORACLES: dict[str, dict] = {
    "krenko, mob boss": {
        "type_line": "Legendary Creature — Goblin Warrior",
        "oracle_text": "{T}: Create X 1/1 red Goblin creature tokens.",
        "color_identity": ["R"],
    },
    "good ramp": {
        "type_line": "Sorcery",
        "oracle_text": "Search your library for a basic land card.",
        "color_identity": [],
    },
    "marginal trinket": {
        # classify_role_extended reads this as "other" — the same role
        # the Goblin Bombardment candidate lands in, so the
        # like-for-like pass has a slot to trade.
        "type_line": "Artifact",
        "oracle_text": "{T}: This does very little.",
        "color_identity": [],
    },
    "a draw": {
        "type_line": "Instant",
        "oracle_text": "Draw two cards.",
        "color_identity": [],
    },
    "goblin bombardment": {
        "type_line": "Enchantment",
        # Token wording on purpose: the preference scorer must see the
        # candidate as on-theme for "tokens".
        "oracle_text": "Sacrifice a creature: create a 1/1 red Goblin "
                       "creature token.",
        "color_identity": ["R"],
    },
    "impact tremors": {
        "type_line": "Enchantment",
        "oracle_text": "Whenever a creature you control enters, deal 1 "
                       "damage to each opponent.",
        "color_identity": ["R"],
    },
    "mountain": {
        "type_line": "Basic Land — Mountain",
        "oracle_text": "",
        "color_identity": ["R"],
    },
}


def _lookup(name):
    return ORACLES.get(name.strip().lower())


def _deck_text(main=("Good Ramp", "Marginal Trinket", "A Draw"),
               protect=()) -> str:
    lines = ["[metadata]", "Name=[USER] Krenko [B3]"]
    lines += [f"Protect={p}" for p in protect]
    lines += ["[Commander]", "1 Krenko, Mob Boss", "[Main]"]
    lines += [f"1 {c}" for c in main]
    lines += ["4 Mountain"]
    return "\n".join(lines) + "\n"


def _deck_file(tmp_path: Path, **kw) -> Path:
    p = tmp_path / "[USER] Krenko [B3].dck"
    p.write_text(_deck_text(**kw), encoding="utf-8")
    return p


def _matrix() -> dict:
    """Minimal lift-matrix (same shape test_deck_builder's fixtures use):
    'Goblin Bombardment' pairs above chance with the commander and
    Good Ramp, so the lift pass surfaces it and ousts the synergy-less
    marginal card."""
    return {
        "too_small": False, "n_decks": 20,
        "names": {
            "goblin bombardment": "Goblin Bombardment",
            "good ramp": "Good Ramp",
            "marginal trinket": "Marginal Trinket",
            "a draw": "A Draw",
            "krenko, mob boss": "Krenko, Mob Boss",
        },
        "counts": {
            "goblin bombardment": 4, "good ramp": 5, "marginal trinket": 3,
            "a draw": 5, "krenko, mob boss": 10,
        },
        "pairs": {
            "goblin bombardment": {"good ramp": 3, "krenko, mob boss": 3},
            "good ramp": {"krenko, mob boss": 3},
        },
        "bands": {},
    }


def _delta_with_links(*names, prose="This deck wins with an infinite "
                      "combo. ") -> str:
    ops = [{"insert": prose}]
    for n in names:
        ops.append({"insert": {"card-link": n}})
        ops.append({"insert": " "})
    return json.dumps({"ops": ops})


# --------------------------------------------------------------------------- #
# UNDERSTAND — the grounded explanation
# --------------------------------------------------------------------------- #

def test_explanation_cross_checks_card_links_against_the_list():
    exp = explain_deck(
        _deck_text(),
        "This deck wins with Good Ramp and Chatterfang. ",
        ["Good Ramp", "Chatterfang, Squirrel General"],
        lookup=_lookup,
    )
    assert exp["primer"]["linked_present"] == ["Good Ramp"]
    assert exp["primer"]["linked_absent"] == ["Chatterfang, Squirrel General"]
    # A linked-absent card = primer/list disagreement, reported with the
    # pointer at the improve loop (adopt does not overhaul).
    assert any("commander improve" in n for n in exp["notes"])


def test_explanation_quotes_the_primers_win_line_verbatim():
    primer_text = ("Intro paragraph.\n\n"
                   "A game winning combo is Krenko plus haste — tap, "
                   "double, repeat.\n\nOutro.")
    exp = explain_deck(_deck_text(), primer_text, [], lookup=_lookup)
    assert exp["primer"]["win_lines"] == [
        "A game winning combo is Krenko plus haste — tap, double, repeat."
    ]


def test_explanation_matches_prose_mentions_of_known_deck_names():
    """Prose matching runs KNOWN list names into the text (lookup, not
    NLP) — a typo'd mention simply doesn't match, it is never guessed."""
    exp = explain_deck(
        _deck_text(),
        "I love a draw and my goood ramp.",  # 'A Draw' hits; typo misses
        [],
        lookup=_lookup,
    )
    assert exp["primer"]["prose_mentions"] == ["A Draw"]


def test_explanation_without_a_primer_is_the_common_case_not_an_error():
    """~75% of harvested decks had no usable primer: the list-grounded
    sections must still be produced, with absence stated."""
    exp = explain_deck(_deck_text(), None, None, lookup=_lookup)
    assert exp["primer"]["present"] is False
    assert exp["roles"]  # roles/themes still computed from the list
    assert exp["main_count"] == 7
    text = render_adoption({
        "deck": "X.dck", "explanation": exp,
        "personalize": {"suggestions": [], "skipped": "no corpus",
                        "max_swaps": POLISH_MAX_SWAPS, "tier": "polish",
                        "preference_slugs": [], "protected": [],
                        "protection_note": "n/a"},
    })
    assert "no primer sidecar found" in text


def test_explanation_reports_the_roles_and_wincons_from_the_list():
    exp = explain_deck(_deck_text(), None, None, lookup=_lookup)
    assert exp["roles"].get("ramp") == 1
    assert exp["roles"].get("other") == 1
    assert exp["roles"].get("land") == 1  # 4x Mountain = one name
    assert exp["unresolved"] == 0


def test_explanation_includes_quantity_aware_rules_warning():
    exp = explain_deck(_deck_text(), None, None, lookup=_lookup)

    assert exp["main_count"] == 7
    assert exp["legality"]["card_count"] == 8
    assert exp["legality"]["status"] == "illegal"
    assert "DECK_SIZE" in {
        violation["code"] for violation in exp["legality"]["violations"]
    }


# --------------------------------------------------------------------------- #
# PERSONALIZE — polish-capped, preference-steered, protection-honoring
# --------------------------------------------------------------------------- #

def test_suggestions_reuse_the_lift_pass_like_for_like():
    out = personalize_suggestions(
        _deck_text(), preferences=None, protected=[],
        lookup=_lookup, matrix=_matrix(),
    )
    assert out["skipped"] is None
    assert len(out["suggestions"]) == 1
    s = out["suggestions"][0]
    assert s["out"] == "Marginal Trinket" and s["in"] == "Goblin Bombardment"
    assert s["rationale"].startswith("swapped Marginal Trinket for Goblin")
    assert "role balance" in s["preserves"]


def test_suggestions_say_which_preference_they_serve():
    out = personalize_suggestions(
        _deck_text(), preferences="I love token swarms",
        protected=[], lookup=_lookup, matrix=_matrix(),
    )
    assert out["preference_slugs"] == ["tokens"]
    s = out["suggestions"][0]
    assert "your stated preference: tokens" in s["serves"]


def test_only_explicit_protect_lines_prevent_primer_linked_cuts(tmp_path):
    """A card link is exact-name primer evidence, not user consent to
    lock that card against otherwise valid polish suggestions."""
    deck = _deck_file(tmp_path)
    primer.write_primer_sidecar(
        deck, _delta_with_links("Marginal Trinket"))
    payload = adopt_deck(deck, preferences=None,
                         lookup=_lookup, matrix=_matrix())
    per = payload["personalize"]
    assert payload["explanation"]["primer"]["linked_present"] == [
        "Marginal Trinket"
    ]
    assert per["protected"] == []
    assert any(s["out"] == "Marginal Trinket" for s in per["suggestions"])


def test_existing_protect_lines_are_honored_too(tmp_path):
    deck = _deck_file(tmp_path, protect=("Marginal Trinket",))
    payload = adopt_deck(deck, preferences=None,
                         lookup=_lookup, matrix=_matrix())
    per = payload["personalize"]
    assert "Marginal Trinket" in per["protected"]
    assert per["suggestions"] == []


def test_prose_only_primer_explains_that_no_explicit_locks_exist(tmp_path):
    deck = _deck_file(tmp_path)
    primer.write_primer_sidecar(
        deck, json.dumps({"ops": [{"insert":
                                   "A long prose primer, no links."}]}))
    payload = adopt_deck(deck, preferences=None,
                         lookup=_lookup, matrix=_matrix())
    note = payload["personalize"]["protection_note"]
    assert note is not None
    assert "no explicit Protect=" in note
    assert "primer" in note.casefold() and "references only" in note
    assert note in render_adoption(payload)


def test_no_primer_at_all_still_explains_explicit_locks(tmp_path):
    deck = _deck_file(tmp_path)
    payload = adopt_deck(deck, preferences=None,
                         lookup=_lookup, matrix=_matrix())
    note = payload["personalize"]["protection_note"]
    assert note is not None and "no explicit Protect=" in note


def test_adopt_renders_legality_problems_as_warning_only(tmp_path):
    deck = _deck_file(tmp_path)
    payload = adopt_deck(
        deck, preferences=None, lookup=_lookup, matrix=_matrix(),
    )

    assert payload["personalize"]["suggestions"], (
        "a rules warning must not block advisory personalization"
    )
    rendered = render_adoption(payload)
    assert "Rules check (warning only)" in rendered
    assert "exactly 100 cards" in rendered


def test_adopt_warns_when_the_command_zone_card_is_ineligible(tmp_path):
    deck = _deck_file(tmp_path)

    def ineligible_lookup(name):
        card = _lookup(name)
        if name.casefold() == "krenko, mob boss":
            return {
                **card,
                "type_line": "Creature — Goblin Warrior",
                "oracle_text": "Create two 1/1 red Goblin tokens.",
            }
        return card

    payload = adopt_deck(
        deck, preferences=None, lookup=ineligible_lookup, matrix=_matrix(),
    )
    codes = {
        item["code"]
        for item in payload["explanation"]["legality"]["violations"]
    }

    assert "COMMANDER_INELIGIBLE" in codes
    rendered = render_adoption(payload)
    assert "legendary creature" in rendered
    assert "Krenko, Mob Boss" in rendered


def test_no_corpus_skips_suggestions_with_the_honest_reason(tmp_path):
    deck = _deck_file(tmp_path)
    payload = adopt_deck(deck, preferences=None, lookup=_lookup,
                         matrix={"too_small": True, "n_decks": 3})
    per = payload["personalize"]
    assert per["suggestions"] == []
    assert "too small" in per["skipped"]


# --------------------------------------------------------------------------- #
# The structural cap — rebuild is unreachable, not defaulted off
# --------------------------------------------------------------------------- #

def test_rebuild_tier_is_structurally_unreachable(tmp_path, monkeypatch):
    """The load-bearing pin. Even with (a) the rebuild env opt-in set —
    which widens change_budget's AUTO escalation elsewhere — and (b) an
    explicit ask for a rebuild-sized budget, adopt's budget is the
    polish cap: there is no parameter, flag, or env read in the module
    that can exceed POLISH_MAX_SWAPS."""
    monkeypatch.setenv("COMMANDER_BUILDER_REBUILD_TIER", "1")
    deck = _deck_file(tmp_path)
    payload = adopt_deck(deck, preferences=None, lookup=_lookup,
                         matrix=_matrix(), max_swaps=999)
    per = payload["personalize"]
    assert per["tier"] == "polish"
    assert per["max_swaps"] == POLISH_MAX_SWAPS
    assert len(per["suggestions"]) <= POLISH_MAX_SWAPS


def test_polish_cap_matches_change_budgets_polish_tier():
    """One source of truth: the cap IS the polish tier's add budget, so
    the two numbers cannot drift apart silently."""
    assert POLISH_MAX_SWAPS == TIER_CAPS["polish"][0]


def test_adopt_module_never_touches_the_rebuild_machinery():
    """Belt and suspenders for 'structurally unreachable': adopt.py's
    CODE holds no name that could widen the budget — no resolve_tier
    call, no rebuild flag, no environ read. AST-scanned (not substring)
    so the module docstring is still allowed to EXPLAIN the rule."""
    import ast

    src = Path(adopt.__file__).read_text(encoding="utf-8")
    banned = {"resolve_tier", "rebuild_tier_enabled",
              "REBUILD_TIER_ENV_VAR", "rebuild", "environ", "getenv"}
    seen: set = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name):
            seen.add(node.id)
        elif isinstance(node, ast.Attribute):
            seen.add(node.attr)
        elif isinstance(node, ast.alias):
            seen.add(node.name)
    hits = banned & seen
    assert not hits, f"adopt.py code references {sorted(hits)}"
    assert 'TIER_CAPS["polish"]' in src  # the one blessed constant read


def test_lower_max_swaps_is_respected(tmp_path):
    deck = _deck_file(tmp_path)
    payload = adopt_deck(deck, preferences=None, lookup=_lookup,
                         matrix=_matrix(), max_swaps=0)
    assert payload["personalize"]["max_swaps"] == 0
    assert payload["personalize"]["suggestions"] == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_main_renders_the_report_offline(tmp_path, capsys, monkeypatch):
    deck = _deck_file(tmp_path)
    primer.write_primer_sidecar(deck, _delta_with_links("Good Ramp"))
    monkeypatch.setattr(adopt, "_lookup_cache_only", _lookup)
    monkeypatch.setattr(
        "commander_builder.lift_analysis.load_or_build_matrix",
        lambda _dir: _matrix(),
    )
    rc = adopt.main([str(deck), "--preferences", "tokens please"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== ADOPT:" in out
    assert "polish tier" in out
    assert "commander improve" in out  # the no-overhaul pointer


def test_main_json_mode_round_trips(tmp_path, capsys, monkeypatch):
    deck = _deck_file(tmp_path)
    monkeypatch.setattr(adopt, "_lookup_cache_only", _lookup)
    monkeypatch.setattr(
        "commander_builder.lift_analysis.load_or_build_matrix",
        lambda _dir: _matrix(),
    )
    rc = adopt.main([str(deck), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deck"] == deck.name
    assert payload["personalize"]["tier"] == "polish"


def test_main_missing_deck_is_a_clean_error(tmp_path, capsys):
    rc = adopt.main([str(tmp_path / "nope.dck")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_preferences_file_is_read(tmp_path, capsys, monkeypatch):
    deck = _deck_file(tmp_path)
    prefs = tmp_path / "prefs.txt"
    prefs.write_text("token swarms forever", encoding="utf-8")
    monkeypatch.setattr(adopt, "_lookup_cache_only", _lookup)
    monkeypatch.setattr(
        "commander_builder.lift_analysis.load_or_build_matrix",
        lambda _dir: _matrix(),
    )
    rc = adopt.main([str(deck), "--preferences-file", str(prefs), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["personalize"]["preference_slugs"] == ["tokens"]

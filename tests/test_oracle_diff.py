"""Tests for ``oracle_diff`` — errata-drift detector between Forge's
``Oracle:`` field and Scryfall's ``oracle_text`` (AGENT_BACKLOG #019).

Three layers:

1. ``normalize_oracle`` — pure text normalization (literal ``\\n``,
   CARDNAME/NICKNAME placeholders, whitespace).
2. ``compare_card_oracle`` — pulls oracle text out of a parsed
   ``CardScript`` + Scryfall payload, normalizes both, returns a
   structured diff.
3. Real-world end-to-end fixture: Underground River, which has a
   known live errata (``Underground River`` → ``This land`` in
   recent WotC text) — pins the detector's ability to flag the
   exact errata pattern it was built for.
"""
from __future__ import annotations

from commander_builder.forge_script_parser import parse_card_script
from commander_builder.oracle_diff import (
    OracleDiffResult,
    compare_card_oracle,
    normalize_oracle,
)


# ---------------------------------------------------------------------------
# normalize_oracle
# ---------------------------------------------------------------------------

def test_normalize_unescapes_forge_literal_backslash_n():
    """Forge stores paragraph breaks as literal ``\\n`` (two
    characters) in the .txt source. After normalization they're
    actual newlines, matching Scryfall's representation."""
    raw = "{T}: Add {C}.\\n{T}: Add {U} or {B}."
    out = normalize_oracle(raw)
    assert out == "{T}: Add {C}.\n{T}: Add {U} or {B}."


def test_normalize_substitutes_cardname_placeholder():
    """Forge uses ``CARDNAME`` as a self-reference placeholder.
    Substituted with the actual card name."""
    raw = "When CARDNAME enters, you gain 2 life."
    out = normalize_oracle(raw, card_name="Kabira Crossroads")
    assert out == "When Kabira Crossroads enters, you gain 2 life."


def test_normalize_substitutes_nickname_placeholder():
    """``NICKNAME`` is the short-form variant Forge uses for cards
    with long full names (Sab-Sunen for Sab-Sunen, Luxa Embodied)."""
    raw = "NICKNAME can't attack or block."
    out = normalize_oracle(raw, card_name="Sab-Sunen")
    assert out == "Sab-Sunen can't attack or block."


def test_normalize_collapses_whitespace_runs():
    """Multiple spaces between words → single space; trailing
    whitespace on lines stripped."""
    raw = "Foo   bar\nbaz \nqux"
    out = normalize_oracle(raw)
    assert out == "Foo bar\nbaz\nqux"


def test_normalize_strips_blob_edges():
    """Leading + trailing whitespace on the whole blob → gone."""
    assert normalize_oracle("   foo   ") == "foo"


def test_normalize_empty_string_round_trips():
    assert normalize_oracle("") == ""
    assert normalize_oracle("   ") == ""


def test_normalize_no_card_name_leaves_placeholders():
    """Without a ``card_name`` arg the substitution is skipped —
    placeholders survive to the output. Better than crashing on a
    missing name."""
    out = normalize_oracle("CARDNAME taps for {G}.")
    assert "CARDNAME" in out


def test_normalize_collapses_unicode_minus_to_ascii_hyphen():
    """Scryfall uses U+2212 in planeswalker loyalty costs; Forge
    uses ASCII hyphen. Cosmetic difference that produced 263 false
    positives in the first oracle-diff smoke run before this
    normalization landed (caught 2026-05-19)."""
    scryfall_form = "−2: Look at the top four cards."
    forge_form = "-2: Look at the top four cards."
    assert normalize_oracle(scryfall_form) == normalize_oracle(forge_form)


def test_normalize_strips_planeswalker_loyalty_brackets():
    """Forge wraps planeswalker loyalty costs in square brackets
    (``[-2]: ...``); Scryfall emits bare (``-2: ...``). Pure
    rendering convention; strip the brackets so cards like Narset
    don't show as drifted on this alone."""
    assert normalize_oracle("[-2]: Look at the top four cards.") == \
           normalize_oracle("-2: Look at the top four cards.")
    assert normalize_oracle("[+1]: Foo.") == normalize_oracle("+1: Foo.")
    assert normalize_oracle("[0]: Bar.") == normalize_oracle("0: Bar.")


# ---------------------------------------------------------------------------
# compare_card_oracle — happy paths
# ---------------------------------------------------------------------------

def test_compare_match_when_forge_and_scryfall_align():
    """Krenko, Mob Boss is one of the easy cases: Forge and Scryfall
    agree byte-for-byte after normalization."""
    krenko_forge = parse_card_script(
        "Name:Krenko, Mob Boss\n"
        "ManaCost:2 R R\n"
        "Types:Legendary Creature Goblin Warrior\n"
        "PT:3/3\n"
        "A:AB$ Token | Cost$ T\n"
        "Oracle:{T}: Create X 1/1 red Goblin creature tokens, where "
        "X is the number of Goblins you control.\n"
    )
    scryfall = {
        "name": "Krenko, Mob Boss",
        "oracle_text": (
            "{T}: Create X 1/1 red Goblin creature tokens, where X "
            "is the number of Goblins you control."
        ),
    }
    result = compare_card_oracle("Krenko, Mob Boss", krenko_forge, scryfall)
    assert result.match is True
    assert result.status == "match"
    assert result.diff_lines == []


def test_compare_detects_real_underground_river_errata():
    """The real bug this module exists to catch: WotC errata'd
    Underground River's self-reference from the card name to the
    generic ``This land``, but Forge still has the old text. The
    diff result should flag the mismatch and surface ``This land``
    vs ``Underground River`` in the diff_lines output."""
    forge_card = parse_card_script(
        "Name:Underground River\n"
        "ManaCost:no cost\n"
        "Types:Land\n"
        "A:AB$ Mana | Cost$ T | Produced$ C\n"
        "A:AB$ Mana | Cost$ T | Produced$ Combo U B\n"
        "Oracle:{T}: Add {C}.\\n{T}: Add {U} or {B}. Underground "
        "River deals 1 damage to you.\n"
    )
    scryfall = {
        "name": "Underground River",
        "oracle_text": (
            "{T}: Add {C}.\n{T}: Add {U} or {B}. This land deals 1 "
            "damage to you."
        ),
    }
    result = compare_card_oracle(
        "Underground River", forge_card, scryfall,
    )
    assert result.match is False
    assert result.status == "differ"
    # Diff content: the old text references "Underground River";
    # the new text references "This land".
    diff_blob = "\n".join(result.diff_lines)
    assert "Underground River deals" in diff_blob
    assert "This land deals" in diff_blob


def test_compare_normalizes_before_comparing():
    """Cosmetic differences (Forge's literal \\n vs Scryfall's actual
    newline) shouldn't trigger a false diff. The normalizer
    smooths these out."""
    forge_card = parse_card_script(
        "Name:Test\n"
        "ManaCost:1\n"
        "Types:Artifact\n"
        "Oracle:Line one.\\nLine two.\n"
    )
    scryfall = {
        "name": "Test",
        "oracle_text": "Line one.\nLine two.",
    }
    result = compare_card_oracle("Test", forge_card, scryfall)
    assert result.match is True


# ---------------------------------------------------------------------------
# compare_card_oracle — missing-data cases
# ---------------------------------------------------------------------------

def test_compare_missing_forge_when_card_script_unavailable():
    """Forge doesn't ship every card; missing scripts get a
    distinct status so the report can bucket them separately
    from real text mismatches."""
    result = compare_card_oracle(
        "Some Custom Card", None,
        {"oracle_text": "Draw a card."},
    )
    assert result.status == "missing_forge"
    assert result.match is False


def test_compare_missing_scryfall_when_lookup_returns_none():
    """Scryfall sometimes 404s (typos, just-released cards).
    Same separate-bucket treatment as missing_forge."""
    forge_card = parse_card_script(
        "Name:Foo\nManaCost:1\nTypes:Artifact\nOracle:Foo does foo.\n"
    )
    result = compare_card_oracle("Foo", forge_card, None)
    assert result.status == "missing_scryfall"


def test_compare_missing_both_when_neither_has_oracle():
    result = compare_card_oracle("Mystery", None, None)
    assert result.status == "missing_both"


# ---------------------------------------------------------------------------
# DFC handling
# ---------------------------------------------------------------------------

def test_compare_concatenates_dfc_faces_for_comparison():
    """For DFCs Forge stores both faces' Oracle lines under the
    parent CardScript and faces[]; Scryfall puts each face's
    oracle_text on its card_faces[]. The comparator concatenates
    both sides with a ``//`` separator so the per-face oracles
    line up symmetrically."""
    forge_card = parse_card_script(
        "Name:Bala Ged Recovery\n"
        "ManaCost:2 G\n"
        "Types:Sorcery\n"
        "Oracle:Return target card from your graveyard to your hand.\n"
        "AlternateMode:DoubleFaced\n"
        "Name:Bala Ged Sanctuary\n"
        "ManaCost:no cost\n"
        "Types:Land\n"
        "Oracle:This land enters tapped.\\n{T}: Add {G}.\n"
    )
    scryfall = {
        "name": "Bala Ged Recovery // Bala Ged Sanctuary",
        "oracle_text": "",
        "card_faces": [
            {"oracle_text": "Return target card from your graveyard to your hand."},
            {"oracle_text": "This land enters tapped.\n{T}: Add {G}."},
        ],
    }
    result = compare_card_oracle(
        "Bala Ged Recovery", forge_card, scryfall,
    )
    assert result.match is True
    assert "//" in result.normalized_forge


def test_compare_detects_dfc_back_face_drift():
    """If only the BACK face differs (front matches), it's still a
    diff — the concatenated text won't equal."""
    forge_card = parse_card_script(
        "Name:Bala Ged Recovery\n"
        "ManaCost:2 G\n"
        "Types:Sorcery\n"
        "Oracle:Return target card from your graveyard to your hand.\n"
        "AlternateMode:DoubleFaced\n"
        "Name:Bala Ged Sanctuary\n"
        "ManaCost:no cost\n"
        "Types:Land\n"
        "Oracle:Bala Ged Sanctuary enters tapped.\\n{T}: Add {G}.\n"
    )
    scryfall = {
        "oracle_text": "",
        "card_faces": [
            {"oracle_text": "Return target card from your graveyard to your hand."},
            {"oracle_text": "This land enters tapped.\n{T}: Add {G}."},
        ],
    }
    result = compare_card_oracle(
        "Bala Ged Recovery", forge_card, scryfall,
    )
    assert result.match is False
    assert result.status == "differ"


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Data-driven bucketing (#020)
# ---------------------------------------------------------------------------

def test_load_diff_buckets_reads_shipped_default():
    """The shipped JSON file loads cleanly and contains the
    canonical ``this-X errata`` rule set."""
    from commander_builder.oracle_diff import (
        DEFAULT_BUCKET_RULES_PATH, load_diff_buckets,
    )
    buckets = load_diff_buckets()
    labels = [b.label for b in buckets]
    # Sanity check: at least the four big errata categories from
    # the 2026-05-19 smoke run are present.
    for expected in (
        "this-land errata", "this-creature errata",
        "this-artifact errata", "this-enchantment errata",
    ):
        assert expected in labels
    assert DEFAULT_BUCKET_RULES_PATH.exists()


def test_load_diff_buckets_accepts_custom_path(tmp_path):
    """Operators can ship their own rules file via ``--bucket-rules``;
    the loader takes the path arg."""
    from commander_builder.oracle_diff import load_diff_buckets
    p = tmp_path / "custom.json"
    p.write_text(
        '{"buckets": [{"label": "custom-rule", '
        '"scryfall_contains": "foo"}]}',
        encoding="utf-8",
    )
    buckets = load_diff_buckets(p)
    assert len(buckets) == 1
    assert buckets[0].label == "custom-rule"
    assert buckets[0].scryfall_contains == "foo"


def test_load_diff_buckets_rejects_missing_label(tmp_path):
    """Misconfigured rule (no label) is a fatal error — no
    sensible default label could match the rule's intent."""
    from commander_builder.oracle_diff import load_diff_buckets
    import pytest as _pytest
    p = tmp_path / "bad.json"
    p.write_text(
        '{"buckets": [{"scryfall_contains": "foo"}]}',
        encoding="utf-8",
    )
    with _pytest.raises(ValueError, match="missing 'label'"):
        load_diff_buckets(p)


def test_diff_bucket_matches_substring_logic():
    """Substring match: both ``scryfall_contains`` and
    ``forge_not_contains`` must hold."""
    from commander_builder.oracle_diff import DiffBucket, OracleDiffResult
    rule = DiffBucket(
        label="t",
        scryfall_contains="this land",
        forge_not_contains="this land",
    )
    matching = OracleDiffResult(
        card_name="X", match=False, status="differ",
        normalized_scryfall="...this land deals 1 damage...",
        normalized_forge="...Underground River deals 1 damage...",
    )
    assert rule.matches(matching) is True
    # Forge ALSO mentions "this land" → not a drift; rule misses.
    both = OracleDiffResult(
        card_name="X", match=False, status="differ",
        normalized_scryfall="this land foo",
        normalized_forge="this land bar",
    )
    assert rule.matches(both) is False


def test_diff_bucket_case_insensitive():
    """Match should fold case so a maintainer doesn't have to
    spell out every capitalization variant."""
    from commander_builder.oracle_diff import DiffBucket, OracleDiffResult
    rule = DiffBucket(label="t", scryfall_contains="THIS LAND")
    result = OracleDiffResult(
        card_name="X", match=False, status="differ",
        normalized_scryfall="this land xyz", normalized_forge="",
    )
    assert rule.matches(result) is True


def test_diff_bucket_empty_rule_does_not_match_everything():
    """Guard: a rule with NO constraints would otherwise match
    every diff and silently swallow the bucketing. matches()
    returns False for an all-None rule so a typo'd config can't
    collapse the buckets."""
    from commander_builder.oracle_diff import DiffBucket, OracleDiffResult
    rule = DiffBucket(label="empty")
    result = OracleDiffResult(
        card_name="X", match=False, status="differ",
        normalized_scryfall="anything", normalized_forge="anything",
    )
    assert rule.matches(result) is False


def test_categorize_diff_returns_first_matching_label():
    """Order matters: ``categorize_diff`` walks the list and
    returns the FIRST matching label so overlapping rules can be
    prioritized (the more-specific rule should be listed before
    the more-general one)."""
    from commander_builder.oracle_diff import (
        DiffBucket, OracleDiffResult, categorize_diff,
    )
    rules = [
        DiffBucket(label="specific", scryfall_contains="this red land"),
        DiffBucket(label="general", scryfall_contains="this land"),
    ]
    result = OracleDiffResult(
        card_name="X", match=False, status="differ",
        normalized_scryfall="this red land taps", normalized_forge="",
    )
    assert categorize_diff(result, rules) == "specific"


def test_categorize_diff_returns_other_when_no_match():
    from commander_builder.oracle_diff import (
        DiffBucket, OracleDiffResult, categorize_diff,
    )
    rules = [DiffBucket(label="x", scryfall_contains="this land")]
    result = OracleDiffResult(
        card_name="X", match=False, status="differ",
        normalized_scryfall="just some other text",
        normalized_forge="and forge text",
    )
    assert categorize_diff(result, rules) == "other"


def test_oracle_diff_result_to_dict_json_safe():
    """``to_dict`` shape is fully primitive — usable directly with
    ``json.dumps`` from the CLI wrapper."""
    import json
    result = OracleDiffResult(
        card_name="X", match=True, status="match",
        normalized_forge="a", normalized_scryfall="a",
        raw_forge="a", raw_scryfall="a",
    )
    blob = json.dumps(result.to_dict())
    payload = json.loads(blob)
    assert payload["card_name"] == "X"
    assert payload["match"] is True
    assert payload["diff_lines"] == []

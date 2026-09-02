"""primer_kb unit tests — FP-019.1 bundled knowledge base + loader.

Covers: bundled-asset integrity, commander lookup (case-insensitive,
partner pairs, multi-profile commanders), fail-quiet loading (missing /
corrupt file, malformed records), the flattened budget-swap table, and
the clipped prompt block.
"""
import json

from commander_builder import primer_kb
from commander_builder.primer_kb import (
    BudgetSwap,
    PrimerProfile,
    WinLine,
    budget_swap_table,
    load_profiles,
    profiles_for_commander,
    prompt_block_for_commander,
)


# --- bundled asset -----------------------------------------------------------

def test_bundled_kb_loads_and_is_substantial():
    profiles = load_profiles()
    assert len(profiles) >= 30
    for p in profiles:
        assert p.name
        assert p.commanders, f"{p.id} has no commanders"
        assert p.url.startswith("http")


def test_bundled_kb_keeps_multiple_profiles_per_commander():
    # §13 of the heuristics doc: encode PROFILES per commander, not one
    # truth. The harvest contains two Gitrog and two Winota builds.
    for cmdr in ("The Gitrog Monster", "Winota, Joiner of Forces"):
        assert len(profiles_for_commander(cmdr)) >= 2, cmdr


def test_bundled_kb_separates_list_presence_from_rules_status():
    profiles = load_profiles()
    lines = [w for p in profiles for w in p.win_lines]
    assert lines, "bundled KB should contain win lines"
    present = [w for w in lines if w.all_cards_present]
    assert present, "at least some lines should use cards in the current list"
    for w in lines:
        assert len(w.cards_present) == len(w.cards)
        assert w.rules_status in {
            "author_claimed", "conditional", "engine", "rules_verified",
        }


# --- lookup semantics --------------------------------------------------------

def test_profiles_for_commander_is_case_insensitive():
    a = profiles_for_commander("the gitrog monster")
    b = profiles_for_commander("The Gitrog Monster")
    assert a and [p.id for p in a] == [p.id for p in b]


def test_profiles_for_commander_matches_either_partner():
    # Hell's Bells runs the Erinis / Street Urchin partner pair.
    by_partner = profiles_for_commander("Street Urchin")
    assert any("Erinis, Gloom Stalker" in p.commanders for p in by_partner)


def test_profiles_for_commander_unknown_returns_empty():
    assert profiles_for_commander("Norin the Wary, Who Is Not Here") == ()


# --- fail-quiet loading ------------------------------------------------------

def test_missing_file_yields_empty(tmp_path):
    assert load_profiles(path=tmp_path / "absent.json") == ()


def test_corrupt_json_yields_empty(tmp_path):
    p = tmp_path / "kb.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_profiles(path=p) == ()


def test_malformed_records_are_skipped_good_ones_kept(tmp_path):
    p = tmp_path / "kb.json"
    p.write_text(json.dumps({"decks": [
        {"id": "ok", "name": "Good", "url": "https://x", "commanders": ["A"],
         "archetype": "aggro", "bracket": "3", "gameplan": "win",
         "win_lines": [{"cards": ["B", "C"], "needs": "both", "note": "",
                        "verified": [True, False],
                        "rules_status": "conditional"}]},
        "not-a-dict",
        {"id": "no-commanders", "name": "Bad", "url": "https://y"},
    ]}), encoding="utf-8")
    profiles = load_profiles(path=p)
    assert [q.id for q in profiles] == ["ok"]
    (w,) = profiles[0].win_lines
    assert w.cards == ("B", "C")
    assert w.cards_present == (True, False)
    assert w.verified == (True, False)  # legacy read-only alias
    assert not w.all_cards_present
    assert w.rules_status == "conditional"


def test_load_is_cached_per_path(tmp_path):
    p = tmp_path / "kb.json"
    p.write_text(json.dumps({"decks": [
        {"id": "one", "name": "N", "url": "https://x", "commanders": ["A"]},
    ]}), encoding="utf-8")
    first = load_profiles(path=p)
    p.write_text(json.dumps({"decks": []}), encoding="utf-8")
    assert load_profiles(path=p) is first  # cache hit, no re-read


# --- derived views -----------------------------------------------------------

def test_budget_swap_table_flattens_with_context():
    rows = budget_swap_table()
    assert rows, "bundled KB contains budget swaps"
    for r in rows:
        assert isinstance(r, BudgetSwap)
        assert r.out_card and r.in_card
        assert r.commander  # context for the advisor
    assert any("Lotus Petal" == r.in_card for r in rows)


def test_prompt_block_renders_and_is_clipped():
    block = prompt_block_for_commander("The Gitrog Monster", cap=600)
    assert "Gitrog" in block
    # clip_for_prompt contract: never longer than cap + marker line.
    assert len(block) <= 600 + primer_kb._CLIP_MARKER_ALLOWANCE


def test_prompt_block_unknown_commander_is_empty():
    assert prompt_block_for_commander("Norin the Wary, Who Is Not Here") == ""


def test_prompt_block_discloses_name_presence_and_rules_provenance():
    block = prompt_block_for_commander("Street Urchin", cap=8_000)

    assert "all named cards confirmed in harvested deck" in block
    assert "rules not independently verified" in block


def test_prompt_recognizes_commanders_excluded_from_mainboard_flags():
    block = prompt_block_for_commander("Etali, Primal Conqueror", cap=8_000)
    etali_line = next(
        line for line in block.splitlines()
        if "Food Chain + Squee, the Immortal" in line
    )

    assert "all named cards confirmed in harvested deck" in etali_line


def test_prompt_reports_missing_presence_evidence_as_unknown_not_absent():
    profile = PrimerProfile(
        id="test", name="Test", url="https://example.com", commanders=("A",),
        win_lines=(WinLine(cards=("Unknown Card",)),),
    )

    block = primer_kb._render_profile(profile)

    assert "not confirmed in harvested mainboard or command zone" in block
    assert "absent" not in block


def test_hells_bells_lines_do_not_overstate_engines_as_two_card_wins():
    profile = next(
        p for p in profiles_for_commander("Street Urchin")
        if p.id == "mox--hell-s-bells"
    )
    lines = {w.cards: w for w in profile.win_lines}

    kodama = lines[("Kodama of the East Tree", "Gruul Turf")]
    assert kodama.rules_status == "conditional"
    assert "permanent-producing landfall" in kodama.needs

    analyst = lines[("Six", "Aftermath Analyst", "Gruul Turf")]
    assert analyst.rules_status == "engine"
    assert "activate" in analyst.needs.casefold()
    assert "{3}{G}" in analyst.needs
    assert "street urchin" not in analyst.needs.casefold()
    assert "not infinite" in analyst.note.casefold()

    world_shaper = lines[("Six", "World Shaper", "Gruul Turf")]
    assert world_shaper.rules_status == "engine"
    assert "die" in world_shaper.needs.casefold()
    assert "not infinite" in world_shaper.note.casefold()

    crevasses = lines[(
        "Glacial Crevasses",
        "Snow-Covered Mountain",
        "Valakut, the Molten Pinnacle",
    )]
    assert crevasses.rules_status == "engine"
    assert "five other Mountains" in crevasses.needs
    assert "resolves" in crevasses.needs
    assert "not a deterministic win" in crevasses.note.casefold()


def test_profile_is_immutable():
    (p,) = load_profiles()[0:1]
    try:
        p.name = "mutated"  # type: ignore[misc]
        raised = False
    except AttributeError:
        raised = True
    assert raised


def test_dataclass_shapes_are_tuples():
    p = profiles_for_commander("The Gitrog Monster")[0]
    assert isinstance(p, PrimerProfile)
    assert isinstance(p.commanders, tuple)
    assert isinstance(p.win_lines, tuple)
    assert all(isinstance(w, WinLine) for w in p.win_lines)
    assert isinstance(p.construction_rules, tuple)


def test_win_line_keeps_the_legacy_verified_constructor_keyword():
    line = WinLine(cards=("A",), verified=(True,))

    assert line.cards_present == (True,)
    assert line.all_cards_present

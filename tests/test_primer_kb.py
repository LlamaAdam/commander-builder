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


def test_bundled_kb_win_lines_carry_verification_flags():
    profiles = load_profiles()
    lines = [w for p in profiles for w in p.win_lines]
    assert lines, "bundled KB should contain win lines"
    verified = [w for w in lines if w.all_verified]
    assert verified, "at least some win lines were mainboard-verified"
    for w in lines:
        assert len(w.verified) == len(w.cards)


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
                        "verified": [True, False]}]},
        "not-a-dict",
        {"id": "no-commanders", "name": "Bad", "url": "https://y"},
    ]}), encoding="utf-8")
    profiles = load_profiles(path=p)
    assert [q.id for q in profiles] == ["ok"]
    (w,) = profiles[0].win_lines
    assert w.cards == ("B", "C")
    assert w.verified == (True, False)
    assert not w.all_verified


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

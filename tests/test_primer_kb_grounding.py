"""FP-019.6 — primer-KB grounding of the judge prompt and the advisor.

Uses the real bundled KB (offline package data) for commander matching;
Scryfall-shaped lookups are stubbed.
"""
from commander_builder._deck_judge_prompt import build_judge_prompt
from commander_builder.improvement_advisor import (
    kb_budget_swap_recommendations,
)
from commander_builder.primer_kb import budget_swaps_for_deck


def _lookup(name):
    return {"name": name, "type_line": "Legendary Creature",
            "oracle_text": "", "mana_cost": "{2}", "cmc": 2.0,
            "color_identity": ["B", "G"]}


def _deck(commander, mains):
    return (f"[Commander]\n1 {commander}\n[Main]\n"
            + "\n".join(f"1 {m}" for m in mains) + "\n")


# --- budget_swaps_for_deck ---------------------------------------------------

def test_budget_swaps_match_commander_and_deck_membership():
    # The Gitrog $50 primer documents Harrow -> Lotus Petal.
    swaps = budget_swaps_for_deck(
        ["The Gitrog Monster"], ["Harrow", "Dakmor Salvage"])
    assert any(s.out_card == "Harrow" and s.in_card == "Lotus Petal"
               for s in swaps)


def test_budget_swaps_skip_when_target_already_in_deck():
    swaps = budget_swaps_for_deck(
        ["The Gitrog Monster"], ["Harrow", "Lotus Petal"])
    assert not any(s.out_card == "Harrow" for s in swaps)


def test_budget_swaps_empty_for_uncovered_commander():
    assert budget_swaps_for_deck(
        ["Norin the Wary, Who Is Not Here"], ["Harrow"]) == ()


# --- advisor recommendations -------------------------------------------------

def test_kb_swap_recommendations_pair_add_and_cut():
    recs = kb_budget_swap_recommendations(
        ["The Gitrog Monster"], ["Harrow"])
    adds = [r for r in recs if r.action == "add"]
    cuts = [r for r in recs if r.action == "cut"]
    assert len(adds) == len(cuts) >= 1
    petal = next(r for r in adds if r.card == "Lotus Petal")
    assert petal.evidence["source"] == "primer_kb"
    assert petal.evidence["replaces"] == "Harrow"
    assert "Harrow" in petal.reason
    harrow = next(r for r in cuts if r.card == "Harrow")
    assert harrow.evidence["replaced_by"] == "Lotus Petal"


def test_kb_swap_recommendations_empty_without_coverage():
    assert kb_budget_swap_recommendations(["Nobody"], ["Harrow"]) == []


# --- judge prompt block ------------------------------------------------------

def test_judge_prompt_gains_primer_context_for_covered_commander():
    a = _deck("The Gitrog Monster", ["Harrow", "Swamp"])
    b = _deck("The Gitrog Monster", ["Dakmor Salvage", "Swamp"])
    prompt = build_judge_prompt(
        deck_a_text=a, deck_b_text=b, lookup=_lookup)
    assert "COMMUNITY PRIMER CONTEXT" in prompt
    assert "Gitrog" in prompt
    # grounding rule restated inside the block
    assert "oracle text" in prompt
    # existing sections untouched
    assert "STATED INTENT" in prompt


def test_judge_prompt_unchanged_for_uncovered_commander():
    a = _deck("Norin the Wary, Who Is Not Here", ["Harrow", "Swamp"])
    b = _deck("Norin the Wary, Who Is Not Here", ["Dakmor Salvage", "Swamp"])
    prompt = build_judge_prompt(
        deck_a_text=a, deck_b_text=b, lookup=_lookup)
    assert "COMMUNITY PRIMER CONTEXT" not in prompt


def test_judge_prompt_block_is_identical_for_both_orderings():
    a = _deck("The Gitrog Monster", ["Harrow", "Swamp"])
    b = _deck("The Gitrog Monster", ["Dakmor Salvage", "Swamp"])
    p_ab = build_judge_prompt(deck_a_text=a, deck_b_text=b, lookup=_lookup)
    p_ba = build_judge_prompt(deck_a_text=b, deck_b_text=a, lookup=_lookup)

    def _block(p):
        start = p.index("COMMUNITY PRIMER CONTEXT")
        end = p.index("ONLY IN DECK A")
        return p[start:end]

    assert _block(p_ab) == _block(p_ba)

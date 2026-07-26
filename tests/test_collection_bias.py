"""Direct unit tests for deck_builder_personalize.apply_collection_bias.

OPTIMIZATION_AUDIT 2026-07-25 P4 flagged this hot spot as having no
direct coverage. The behavioral tests pin the documented
near-equivalent contract; the call-counting test pins the perf fix
(per-owned-card attributes derived once, not once per deck card).
All injectable callables — fully offline.
"""

from __future__ import annotations

from commander_builder.collection import name_key
from commander_builder.deck_builder_personalize import apply_collection_bias


def coll(*names):
    return frozenset(name_key(n) for n in names)


def make_bias_kwargs(**overrides):
    """Baseline: one unowned removal spell in deck; owned near-equivalent
    available. Individual tests override single knobs."""
    roles = {"Unowned Removal": "removal", "Owned Removal": "removal",
             "Owned Ramp": "ramp", "Second Owned Removal": "removal"}
    quality = {"Unowned Removal": 1.0, "Owned Removal": 1.0,
               "Owned Ramp": 1.0, "Second Owned Removal": 1.0}
    mv = {"Unowned Removal": 2.0, "Owned Removal": 2.0,
          "Owned Ramp": 2.0, "Second Owned Removal": 2.0}
    kwargs = dict(
        collection=coll("Owned Removal", "Owned Ramp",
                        "Second Owned Removal"),
        owned_pool=["Owned Removal", "Second Owned Removal", "Owned Ramp"],
        ci_ok=lambda n: True,
        role_of=lambda n: roles.get(n, "other"),
        mv_of=lambda n: mv.get(n),
        quality_of=lambda n: quality.get(n, 0.0),
        reserved_keys=set(),
    )
    kwargs.update(overrides)
    return kwargs


def test_swaps_in_owned_same_role_equivalent():
    out, notes = apply_collection_bias(
        ["Unowned Removal"], **make_bias_kwargs())
    assert out == ["Owned Removal"]
    assert len(notes) == 1 and "owned-bias" in notes[0]


def test_first_matching_pool_entry_wins():
    out, _ = apply_collection_bias(
        ["Unowned Removal"],
        **make_bias_kwargs(owned_pool=["Second Owned Removal",
                                       "Owned Removal"]))
    assert out == ["Second Owned Removal"]


def test_role_mismatch_never_swaps():
    kw = make_bias_kwargs(owned_pool=["Owned Ramp"])
    out, notes = apply_collection_bias(["Unowned Removal"], **kw)
    assert out == ["Unowned Removal"] and notes == []


def test_refuses_quality_downgrade_beyond_tolerance():
    kw = make_bias_kwargs()
    kw["quality_of"] = lambda n: 0.2 if n == "Owned Removal" else 1.0
    kw["owned_pool"] = ["Owned Removal"]
    out, _ = apply_collection_bias(["Unowned Removal"], **kw,)
    assert out == ["Unowned Removal"]
    out2, _ = apply_collection_bias(
        ["Unowned Removal"], **{**kw, "tolerance": 1.0})
    assert out2 == ["Owned Removal"]


def test_refuses_wide_cost_gap():
    kw = make_bias_kwargs()
    kw["mv_of"] = lambda n: 6.0 if n == "Owned Removal" else 2.0
    kw["owned_pool"] = ["Owned Removal"]
    out, _ = apply_collection_bias(["Unowned Removal"], **kw)
    assert out == ["Unowned Removal"]


def test_skips_owned_and_protected_deck_cards():
    kw = make_bias_kwargs(
        collection=coll("Already Owned", "Owned Removal"),
        protect=lambda n: n == "Protected Card",
    )
    out, notes = apply_collection_bias(
        ["Already Owned", "Protected Card"], **kw)
    assert out == ["Already Owned", "Protected Card"] and notes == []


def test_each_owned_card_used_at_most_once():
    out, _ = apply_collection_bias(
        ["Unowned Removal", "Unowned Removal"],
        **make_bias_kwargs(owned_pool=["Owned Removal"]))
    # Only one copy can be swapped; the second stays.
    assert out.count("Owned Removal") == 1


def test_reserved_and_live_keys_excluded():
    kw = make_bias_kwargs(reserved_keys={name_key("Owned Removal")})
    kw["owned_pool"] = ["Owned Removal"]
    out, _ = apply_collection_bias(["Unowned Removal"], **kw)
    assert out == ["Unowned Removal"]
    # An owned card already IN the deck is never swapped in again.
    kw2 = make_bias_kwargs(owned_pool=["Owned Removal"])
    out2, _ = apply_collection_bias(
        ["Owned Removal", "Unowned Removal"], **kw2)
    assert out2.count("Owned Removal") == 1


def test_no_collection_or_pool_is_identity():
    out, notes = apply_collection_bias(
        ["Unowned Removal"], **make_bias_kwargs(collection=None))
    assert out == ["Unowned Removal"] and notes == []
    out2, notes2 = apply_collection_bias(
        ["Unowned Removal"], **make_bias_kwargs(owned_pool=[]))
    assert out2 == ["Unowned Removal"] and notes2 == []


def test_pool_attributes_derived_once_per_owned_card():
    """The P4 perf pin: ci_ok / role_of / mv_of / quality_of run at most
    once per DISTINCT owned-pool card, no matter how many deck cards the
    outer loop visits (was O(deck x collection) with an uncached lookup
    inside ci_ok — 9.6 s / 38,744 lookups measured on a 5k collection)."""
    calls = {"ci_ok": [], "role_of": [], "mv_of": [], "quality_of": []}
    kw = make_bias_kwargs()

    def counting(fn_name, inner):
        def wrapper(n):
            if n.startswith(("Owned", "Second")):
                calls[fn_name].append(n)
            return inner(n)
        return wrapper

    # Pool entries that NEVER match (role mismatch) are the hot case:
    # they were re-screened for every deck card. Owned cards are ramp,
    # deck cards are removal, so no swap ever consumes a pool entry.
    kw["role_of"] = counting(
        "role_of",
        lambda n: "ramp" if n.startswith(("Owned", "Second")) else "removal")
    for f in ("ci_ok", "mv_of", "quality_of"):
        kw[f] = counting(f, kw[f])
    deck = [f"Unowned Card {i}" for i in range(10)]
    apply_collection_bias(deck, **kw)
    for f, seen in calls.items():
        per_card = {}
        for n in seen:
            per_card[n] = per_card.get(n, 0) + 1
        assert all(c <= 1 for c in per_card.values()), (
            f"{f} re-derived per deck card: {per_card}")

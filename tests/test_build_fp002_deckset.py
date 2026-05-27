"""Tests for the FP-002 deck-set builder's naming contract.

The soak (`soak_pool --mode gauntlet`) pairs a base deck with its v2 by
keying on the literal "[USER]" prefix and " v2 " inserted before the
"[B<n>]" tag. If the base/v2 names drift apart, the pair silently stops
being recognized, so these names are a contract worth pinning.
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "build_fp002_deckset",
    Path(__file__).resolve().parent.parent / "scripts" / "build_fp002_deckset.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def test_short_strips_title_and_punctuation():
    assert mod.short("Korvold, Fae-Cursed King") == "Korvold"
    assert mod.short("Yuriko, the Tiger's Shadow") == "Yuriko"
    assert mod.short("The Ur-Dragon") == "The Ur-Dragon"


def test_base_and_v2_names_are_a_soak_pair():
    d = Path("decks")
    base = mod._base_path(d, "Korvold, Fae-Cursed King", 5)
    v2 = mod._v2_path(d, "Korvold, Fae-Cursed King", 5)
    assert base.name == "[USER] Korvold FP2 [B5].dck"
    assert v2.name == "[USER] Korvold FP2 v2 [B5].dck"
    # the soak's pairing rule: v2 == base with " v2 " inserted, "[USER]" kept
    assert v2.name.startswith("[USER]")
    assert " v2 " in v2.name
    assert v2.name.replace(" v2 ", " ") == base.name


def test_commander_list_is_diverse_and_sized():
    # ~30 commanders, all with an explicit bracket in {3,4,5}, names unique.
    assert 25 <= len(mod.COMMANDERS) <= 35
    assert all(br in (3, 4, 5) for _, br in mod.COMMANDERS)
    assert len({c for c, _ in mod.COMMANDERS}) == len(mod.COMMANDERS)

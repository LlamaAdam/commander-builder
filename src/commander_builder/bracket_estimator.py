"""Explainable Commander-bracket estimator (ManaFoundry parity).

Estimates a deck's WotC Commander bracket (1-5) from its list alone and
surfaces estimated-vs-declared so mislabeled decks get flagged before
they poison sim pools or mislead the dashboard.

WHERE THE RULES COME FROM — this module deliberately invents nothing.
Every hard bound and weighted signal cites an existing encoding of the
official bracket rules already in this repo, read against the
2025-10-21 WotC bracket update (and the 2026-02-09 B&R follow-up):

  * ``prompts/moxfield_audit_v3.md`` "BRACKET RULES" table — the
    repo's canonical transcription of WotC's per-bracket Game Changer
    caps: B1/B2 allow ZERO Game Changers, B3 allows a MAX of 3,
    B4/B5 are unlimited.
  * The same prompt's "Auto-Bracket Bumper Heuristic" reference data —
    the LAND DESTRUCTION/MLD and EXTRA TURNS seed lists reproduced
    (and since extended) below. NOTE the prompt's "stacking 4+
    tutors auto-bumps" rule transcribed a pre-Oct-2025 beta
    restriction that the 2025-10-21 update REPEALED — tutor caps left
    the official bracket rules entirely; the Game Changers list is
    what carries the efficient tutors now. Tutor density therefore
    survives here only as a clearly-labeled power-level HEURISTIC
    (weighted signal), never a rule citation — see DEFAULT_WEIGHTS.
  * ``game_changers.py`` — the official Game Changers list
    (``load_game_changers``), dynamic-fetch + offline fallback.
  * ``combo_detection.py`` — ``combo_bracket_floor``: the 2025-10-21
    rules gate two-card game-ending combos on SPEED, not bare count —
    an early-assembling pair floors at B4 while a late-assembling
    (~turn 6+) pair is B3-legal; a 3+-card game-ending combo floors
    at B3. This module defers to that per-combo floor verbatim
    (combo_detection still floors B4 when the speed can't be resolved
    offline — conservative on missing data). See the combo floor
    comment in ``_estimate_bracket_inner``.
  * ``deck_dashboard._power_bracket`` — the pre-existing nudge
    heuristic whose curve bands (<=2.6 tight / >3.4 high) and
    combo/stax archetype nudges the weighted signals mirror.
  * ``web/deck_insights._SALT_WARN_THRESHOLD`` (1.5) — the salt
    cut-off reused for the salt signal (redefined locally because a
    core module must not import from the web layer).

DESIGN — hard bounds first, weighted signals inside them:

  1. HARD FLOORS. Rule violations that make a lower bracket
     impossible BY DEFINITION set a floor. Nothing sets a hard
     ceiling: a precon-level list with one Game Changer is still a
     B3 deck by rule, so the floor is the only bound.
  2. WEIGHTED SIGNALS. Inside the bounds, a score starting at the
     B2/"Core" precon baseline accumulates per-signal contributions
     (weights live in ``DEFAULT_WEIGHTS``, one documented dict).
     The rounded score, clamped to ``[floor, 5]``, is the estimate.

Everything is OFFLINE-SAFE: card lists are name-based frozensets, the
GC list degrades to its bundled fallback, salt comes from the EDHREC
disk cache only (never fetched here), and the whole estimator is
wrapped so it NEVER raises on weird decks (empty, no commander,
all-lands, binary garbage) — it degrades to a low-confidence B2 guess.

MODULE PLACEMENT — this is a core-layer module (sibling of
``combo_detection`` / ``game_changers``), NOT part of
``web/deck_insights``: pool_curator and meta_test are non-web callers,
and importing the web package from them would invert the repo's
layering (web imports core, never the reverse).

Public API:

    from commander_builder.bracket_estimator import (
        derive_signals, estimate_bracket, mismatch_warning,
    )

    avg_cmc, archetype = derive_signals(deck_text, deck_path)
    result = estimate_bracket(deck_text, declared=3,
                              avg_cmc=avg_cmc, archetype=archetype)
    # -> {"estimate": 3, "floor": 3, "confidence": "high",
    #     "reasons": [...], "signals": {...}, "declared": 3,
    #     "mismatch": False, "mismatch_level": None}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

# Resolves a card name to its Scryfall dict (or None when unknown). The
# injectable seam every derivation here routes through, so callers with an
# existing cached lookup reuse it and tests stay offline.
CardLookup = Callable[[str], Optional[dict]]

# ---------------------------------------------------------------------------
# Rule data — name-based card lists, each citing its repo source
# ---------------------------------------------------------------------------

# Mass land denial. Seed source: prompts/moxfield_audit_v3.md,
# "Auto-Bracket Bumper Heuristic" -> "LAND DESTRUCTION/MLD" list;
# extended 2026-08 with the well-known misses (Sunder through Death
# Cloud below). WotC's bracket guidance prohibits mass land denial in
# brackets 1-3, so ANY of these is a hard B4 floor — see the floors in
# ``_estimate_bracket_inner``. This is the OFFLINE FALLBACK: the
# refresh tooling (scripts/refresh_card_lists.py --only mld, backed by
# _card_list_refresh.mld_names_from_snapshots) diffs it against the
# local oracle snapshots so a maintainer can fold in new printings.
_MLD_CARDS = frozenset(c.lower() for c in (
    # Audit-prompt seed list
    "Armageddon", "Ravages of War", "Catastrophe", "Cataclysm",
    "Wildfire", "Obliterate", "Jokulhaups", "Decree of Annihilation",
    # 2026-08 extension — long-standing MLD staples the seed missed
    "Sunder", "Fall of the Thran", "Global Ruin", "Impending Disaster",
    "Keldon Firebombers", "Epicenter", "Burning of Xinye", "Death Cloud",
))

# Extra-turn spells. Seed source: prompts/moxfield_audit_v3.md "EXTRA
# TURNS" list; extended 2026-08 with the well-known misses (Alrund's
# Epiphany through Savor the Moment below). Refresh tooling:
# scripts/refresh_card_lists.py --only extra-turns, backed by
# _card_list_refresh.extra_turn_names_from_snapshots ("take(s) an/N
# extra turn(s)" oracle text over the local snapshot store).
#
# BRACKET READING (2025-10-21 rules): B1/B2 no extra turns; B3 allows
# them in LOW QUANTITIES so long as they are not CHAINED or looped.
# The bare count-of-two proxy this module used to floor on encoded the
# stricter pre-Oct-2025 beta reading; the chaining operationalization
# now lives in ``_estimate_bracket_inner`` (see the extra-turn floor
# comment there and ``_EXTRA_TURN_REPEATABLE_ENABLERS`` /
# ``_EXTRA_TURN_ONESHOT_REBUYS``).
_EXTRA_TURN_CARDS = frozenset(c.lower() for c in (
    # Audit-prompt seed list
    "Time Warp", "Temporal Manipulation", "Walk the Aeons",
    "Time Stretch", "Nexus of Fate", "Expropriate",
    # 2026-08 extension — widely-played extra-turn spells the seed missed
    "Alrund's Epiphany", "Temporal Mastery", "Part the Waterveil",
    "Capture of Jingzhou", "Temporal Trespass",
    "Karn's Temporal Sundering", "Savor the Moment",
))

# Recursion / copy pieces that turn a "low quantity" of extra-turn
# spells into a CHAIN: rebuy the spell from the graveyard or copy it
# on the stack. Curated (oracle text can't cleanly separate "returns
# instants/sorceries" recursion aimed at time magic from generic value
# recursion — human judgment stays in the loop, same stance as the
# tutor list).
#
# SPLIT BY REPEATABILITY (round-2 bracket-floor correctness pass). The
# 2025-10-21 WotC bracket language restricts extra turns below B4 when
# they are "CHAINED OR LOOPED"; B3 allows them in low quantities
# otherwise. A chain/loop needs an engine that rebuys or copies turn
# spells REPEATEDLY — a ONE-SHOT rebuy (Eternal Witness returning one
# Time Warp, Fork copying one) yields exactly one additional turn and
# then is spent: that is "low quantities plus value", not a chain, and
# hard-flooring it at B4 slammed ordinary B3 lists. The one axis that
# separates the buckets is: after the enabler does its thing once, can
# it do it again without outside help?
#
#   * REPEATABLE (hard B4 floor at 2+ extra-turn cards): the enabler
#     survives its own use or rebuys spells en masse — buyback returns
#     the copy engine to hand every cast (Reiterate); a permanent
#     copies every turn spell you cast, forever (Mirari); a fetchable
#     land re-tops the turn spell each time it re-enters (Mystic
#     Sanctuary); a mass rebuy hands the whole graveyard back
#     (Timetwister, Underworld Breach, Past in Flames).
#   * ONE-SHOT (weighted signal, NO hard floor at 2): single rebuys and
#     single copies — Eternal Witness / Regrowth / Mystic Retrieval
#     (one card back, once), Snapcaster Mage / Torrential Gearhulk
#     (one flashback / one free cast), Twincast / Fork (one copy),
#     Narset's Reversal (copies AND rebounds the turn spell, but
#     spends ITSELF — the loop dies without a second Reversal),
#     Dualcaster Mage (one ETB copy), and the bare ETB-recursion
#     creatures Archaeomancer / Mnemonic Wall / Scholar of the Ages:
#     repeating those requires a blink engine, which is out of static
#     name-list reach, so the bare creatures stay one-shot.
#
# Used by the extra-turn chaining floor below: 2+ extra-turn spells
# PLUS a REPEATABLE engine is a credible loop (hard B4 floor); 2 with
# only one-shot rebuys is the "low quantities" B3 allowance carrying an
# extra weighted nudge (see DEFAULT_WEIGHTS["extra_turn_oneshot_rebuy"]).
# The 3+ raw-density floor is unchanged and independent of this split.
_EXTRA_TURN_REPEATABLE_ENABLERS = frozenset(c.lower() for c in (
    # Buyback / permanent copy engines
    "Reiterate", "Mirari",
    # Fetchable, recurring graveyard-to-library rebuy
    "Mystic Sanctuary",
    # Mass rebuy
    "Timetwister", "Underworld Breach", "Past in Flames",
))

_EXTRA_TURN_ONESHOT_REBUYS = frozenset(c.lower() for c in (
    # Single graveyard rebuy
    "Archaeomancer", "Mnemonic Wall", "Scholar of the Ages",
    "Eternal Witness", "Regrowth", "Mystic Retrieval",
    "Snapcaster Mage", "Torrential Gearhulk",
    # Single stack copy
    "Narset's Reversal", "Twincast", "Fork", "Dualcaster Mage",
))

# Tutors, for the density signal. HEURISTIC ONLY as of the 2025-10-21
# rules update: WotC removed tutor restrictions from the brackets (the
# audit prompt's "stacking 4+ tutors auto-bumps" rule transcribed the
# repealed beta text), and the Game Changers list is the official
# carrier of the efficient tutors now. Tutor density is still a real
# consistency/power signal, so the list stays — as a weighted
# heuristic, never a rule citation. Kept CURATED by hand: oracle text
# can't cleanly separate tutors from fetches/ramp, so no snapshot
# refresh path exists for this list. Sources:
#   * the Game-Changer tutors from game_changers._FALLBACK (Demonic /
#     Vampiric / Mystical / Worldly / Enlightened Tutor, Imperial Seal,
#     Gamble) — they also count in the GC signal, which is correct:
#     they carry both kinds of bracket pressure;
#   * the prompt's "TUTORS (mass)" list (Diabolic Intent, Grim Tutor,
#     Personal Tutor, Sylvan Tutor);
#   * a handful of ubiquitous non-GC tutors so a tutor-dense deck
#     that avoids the GC list still reads as tutor-dense. Name-based
#     (not oracle-text ``classify_role``) so the count is deterministic
#     offline — staples.classify_role needs a Scryfall lookup per card.
_TUTOR_CARDS = frozenset(c.lower() for c in (
    # Game-Changer tutors (game_changers.py fallback list)
    "Demonic Tutor", "Vampiric Tutor", "Imperial Seal",
    "Mystical Tutor", "Worldly Tutor", "Enlightened Tutor", "Gamble",
    # prompt "TUTORS (mass)" auto-bumper list
    "Diabolic Intent", "Grim Tutor", "Personal Tutor", "Sylvan Tutor",
    # Common non-GC tutors (widely-played, name-stable)
    "Diabolic Tutor", "Green Sun's Zenith", "Chord of Calling",
    "Finale of Devastation", "Eladamri's Call", "Idyllic Tutor",
    "Fabricate", "Whir of Invention", "Tinker", "Solve the Equation",
    "Steelshaper's Gift", "Open the Armory", "Fauna Shaman",
    "Sidisi, Undead Vizier", "Rune-Scarred Demon",
))

# Fast mana, for the density signal. Restricted to NON-Game-Changer
# entries: GC-listed fast mana (Mana Vault, Grim Monolith, Chrome Mox,
# Mox Diamond, Lion's Eye Diamond, Ancient Tomb — see
# game_changers._FALLBACK) is already counted by the GC signal, and
# counting it twice would double-charge one card. Mana Crypt / Jeweled
# Lotus / Dockside are Commander-BANNED (web/routes_decks._CORE_BANS)
# but old deck files still carry them — if present they are exactly the
# power signal this estimator exists to catch, so they stay listed.
_FAST_MANA_CARDS = frozenset(c.lower() for c in (
    "Mana Crypt", "Jeweled Lotus", "Lotus Petal", "Mox Opal",
    "Mox Amber", "Dark Ritual", "Cabal Ritual", "Rite of Flame",
    "Pyretic Ritual", "Desperate Ritual", "Seething Song",
    "Simian Spirit Guide", "Elvish Spirit Guide", "Culling the Weak",
))

# Salt threshold: mirrors web/deck_insights._SALT_WARN_THRESHOLD
# ("noticeable salt" on EDHREC's 0..5 color scale). Redefined here
# because core modules must not import from the web layer.
_SALT_THRESHOLD = 1.5

# How many salt-listed cards it takes before the deck "leans salty".
# Mirrors the audit prompt's stance that salt is a lower-bracket
# mismatch signal in AGGREGATE (one Rhystic Study is normal; a pile of
# top-salt picks reads as a tuned table-unfriendly list).
_SALT_COUNT_TRIGGER = 5

# ---------------------------------------------------------------------------
# Weighted-signal weights — ONE documented dict (the tuning surface)
# ---------------------------------------------------------------------------

# The score starts at 2.0 = B2 "Core" (the precon baseline per the
# prompt's bracket table: a stock precon is the definitional B2 deck).
# Each signal ADDS its weight x its (capped) count; the rounded sum,
# clamped to [hard floor, 5], is the estimate.
DEFAULT_WEIGHTS: dict[str, float] = {
    # Per Game Changer (capped at 5 counted). 0.4 x 3 GCs = +1.2 puts a
    # 3-GC deck at ~B3 even before the floor — consistent with the
    # prompt table (B3 = "Max 3" GCs). Mirrors the dominant role the GC
    # count plays in deck_dashboard._power_bracket.
    "game_changer": 0.4,
    # Tutor density — power-level HEURISTIC, not a rule (the official
    # "stacking 4+ tutors auto-bumps" step was repealed 2025-10-21;
    # the GC list carries efficient tutors now). The step shape is
    # kept from the old rule because it still models consistency well:
    # 4+ tutors = full signal, 2-3 = half.
    "tutors_4_plus": 1.0,
    "tutors_2_3": 0.5,
    # Per non-GC fast-mana rock/ritual (capped at 4 counted). Fast mana
    # compresses the early game the same direction a tight curve does.
    "fast_mana": 0.3,
    # Archetype nudges — same direction and spirit as
    # deck_dashboard._power_bracket ("combo decks are almost always at
    # least bracket 3"; stax pressures the table up a bracket).
    "archetype_combo": 1.0,
    "archetype_stax": 0.5,
    # Per detected game-ending combo beyond what the floor already
    # charges (capped at 2 counted) — a deck with several combo lines
    # is more committed than one accidental pairing.
    "combo_line": 0.25,
    # Per NON-CHAINING extra-turn card, capped at 2 counted (B3 allows
    # "low quantities" of un-chained extra turns — 2025-10-21 rules).
    # A chaining count/setup is a hard B4 floor instead, so this
    # weight only ever fires for 1-2 cards without a repeatable chain
    # engine.
    "extra_turn_single": 0.25,
    # Per ONE-SHOT rebuy/copy piece (capped at 2 counted) alongside 2+
    # extra-turn cards. One-shot rebuys (Eternal Witness, Fork, ...)
    # can't sustain the "chained or looped" line the 2025-10-21 rules
    # floor on, so they are a power nudge — same magnitude per card as
    # extra_turn_single — never a floor. Fires only at 2+ extra-turn
    # cards: a single Time Warp plus Snapcaster is generic value, not
    # extra-turn pressure.
    "extra_turn_oneshot_rebuy": 0.25,
    # Curve bands from _power_bracket: <=2.6 avg CMC reads "tuned low
    # curve"; >=3.8 reads "casual battlecruiser" and pulls DOWN.
    "curve_tight": 0.5,
    "curve_high": -0.5,
    # Salt pile (>= _SALT_COUNT_TRIGGER cards over threshold, from the
    # OFFLINE EDHREC cache only).
    "salty_pile": 0.5,
}

# Mismatch policy (documented choice): |estimate - declared| >= 1 flags
# a soft "check" (brackets are fuzzy; one step of disagreement is
# normal heuristic noise), >= 2 flags a hard "mismatch" (two steps
# means the deck is playing a different game than its label — the
# pool-poisoning case pool_curator/meta_test warn about). The boolean
# ``mismatch`` field is True only at the hard >= 2 level.
#
# CONFIDENCE GATE: both levels require the estimate to be better than
# "low" confidence. A low-confidence estimate means NOTHING fired — the
# deck hit none of the name lists and no context signal (avg_cmc /
# archetype / salt cache) was available — so the "estimate" is just the
# untouched B2 baseline. That is evidence of signal starvation, not of
# mislabeling: a genuinely casual list and an un-classifiable powerful
# list look identical to it. Such estimates report the distinct
# "low_signal" level instead (mismatch stays False) so consumers can
# say "insufficient signal" rather than accuse the declared tag.
_MISMATCH_HARD_DIFF = 2


# ---------------------------------------------------------------------------
# Signal collection helpers (each fails soft — see estimate_bracket)
# ---------------------------------------------------------------------------

def _deck_card_names(deck_text: str) -> list[str]:
    """Lowercase card names from [Commander] + [Main] (dedup'd, order
    kept). Reuses deck_library_analyzer.iter_deck_cards — the same
    parser combo_detection trusts — so section/edition-tail handling
    stays single-sourced."""
    from .deck_library_analyzer import iter_deck_cards
    seen: set[str] = set()
    out: list[str] = []
    for _qty, name in iter_deck_cards(deck_text or ""):
        low = name.lower()
        if low not in seen:
            seen.add(low)
            out.append(low)
    return out


def _count_game_changers(names_lc: set[str]) -> tuple[int, list[str]]:
    """Count Game Changers in the deck, minus universal staples.

    Mirrors deck_dashboard._count_game_changers exactly: the GC list
    comes from game_changers.load_game_changers (bundled fallback on
    any failure) and UNIVERSAL_STAPLES_LC entries (Sol Ring, Arcane
    Signet, ...) are excluded because they're baseline ramp in
    essentially every deck, not "this deck is powered up" signal.
    """
    try:
        from .game_changers import load_game_changers
        gc_set = load_game_changers()
    except Exception:  # noqa: BLE001 — estimator must not raise
        return 0, []
    try:
        from .staples import UNIVERSAL_STAPLES_LC
        staples_lc = set(UNIVERSAL_STAPLES_LC)
    except Exception:  # noqa: BLE001
        staples_lc = set()
    hits = sorted(
        g for g in gc_set
        if g.lower() in names_lc and g.lower() not in staples_lc
    )
    return len(hits), hits


def _detect_game_ending_combos(deck_text: str) -> list[dict]:
    """Game-ending combos present in the deck, each annotated with its
    bracket floor. Thin wrapper over combo_detection (offline: cached
    data/combos.json or the hand-curated fallback)."""
    try:
        from .combo_detection import (
            combo_bracket_floor, detect_combos_in_deck, is_game_ending,
        )
        found = detect_combos_in_deck(deck_text)
        return [
            {**c, "bracket_floor": combo_bracket_floor(c)}
            for c in found if is_game_ending(c)
        ]
    except Exception:  # noqa: BLE001 — estimator must not raise
        return []


def _offline_salt_count(names_lc: set[str]) -> Optional[int]:
    """Count deck cards at/above the salt threshold using ONLY the
    EDHREC disk cache (.cache/edhrec_salt/top-salt.json — the file
    edhrec_client.fetch_salt_list persists). Never fetches: this runs
    inside pool_curator loops over dozens of decks, where a per-deck
    network timeout would be unacceptable. Returns None when the cache
    is absent/unreadable so the signal reads "unavailable", not 0.
    """
    try:
        from .edhrec_client import CACHE_DIR
        cache_path = CACHE_DIR.parent / "edhrec_salt" / "top-salt.json"
        if not cache_path.exists():
            return None
        salt_map = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(salt_map, dict) or not salt_map:
            return None
        return sum(
            1 for n in names_lc
            if float(salt_map.get(n, 0) or 0) >= _SALT_THRESHOLD
        )
    except Exception:  # noqa: BLE001 — estimator must not raise
        return None


# ---------------------------------------------------------------------------
# Context derivation — the shared entry point for callers WITHOUT context
# ---------------------------------------------------------------------------

def derive_signals(
    deck_text: str,
    deck_path: Optional[Path] = None,
    lookup: Optional[CardLookup] = None,
) -> tuple[Optional[float], Optional[str]]:
    """Derive ``(avg_cmc, archetype)`` for a deck — the two context signals
    :func:`estimate_bracket` cannot compute for itself.

    WHY THIS EXISTS: ``estimate_bracket`` takes ``avg_cmc`` / ``archetype``
    as OPTIONAL pre-computed context because deriving them costs a Scryfall
    lookup per card, which the estimator's offline/never-blocks contract
    forbids it from doing implicitly. The consequence was that only the
    dashboard — which happens to compute both for its own stat tiles — ever
    passed them; ``commander-advise`` and ``commander-build``'s bracket
    STEERING LOOP passed nothing, so ``curve_tight`` (+0.5),
    ``curve_high`` (-0.5), ``archetype_combo`` (+1.0) and
    ``archetype_stax`` (+0.5) could never fire in the very code path whose
    job is to steer a deck to a target bracket. Up to 1.5 points of signal
    silently missing is a whole bracket. This helper is the one place that
    derivation lives so all three call sites get identical treatment.

    ``deck_path`` is optional. When given, archetype comes from the repo's
    canonical ``archetype.classify`` (filename hint first — a deck the user
    named "Storm Combo" is telling us the strategy outright). When absent —
    the steering loop scores rendered text that has no file yet — we fall
    back to the same module's card-NAME content scan.

    ``lookup`` resolves a card name to its Scryfall dict (needs ``cmc`` and
    ``type_line``); defaults to ``scryfall_client.lookup_card`` for parity
    with the dashboard's own avg-CMC computation, and is injectable both for
    tests and so callers that already have a cached/instrumented lookup
    (deck_builder does) reuse it instead of doubling the traffic.

    FAIL-QUIET, AND NEVER FABRICATE. Either element is ``None`` when it
    could not be derived — an unreadable deck, a Scryfall outage, an
    all-lands list, an archetype scan with no winner. ``None`` means "signal
    unavailable" and leaves the corresponding weight silent. In particular
    avg_cmc is NEVER returned as ``0.0`` on failure: ``0.0`` is a real curve
    value that would look like the tightest possible deck. (The weight logic
    also guards with ``avg_cmc > 0``; this helper keeps that guard
    redundant rather than load-bearing.) This function never raises.

    HOT LOOPS: derivation is O(deck) Scryfall lookups. Callers that
    re-estimate repeatedly over near-identical lists (the steering loop)
    should derive ONCE and reuse — a handful of swapped cards moves avg CMC
    by hundredths, far less than the 2.6/3.8 band edges.
    """
    avg_cmc: Optional[float] = None
    archetype: Optional[str] = None
    try:
        avg_cmc = _derive_avg_cmc(deck_text, lookup)
    except Exception:  # noqa: BLE001 — context is a bonus, never a blocker
        avg_cmc = None
    try:
        archetype = _derive_archetype(deck_text, deck_path)
    except Exception:  # noqa: BLE001
        archetype = None
    return avg_cmc, archetype


def _derive_avg_cmc(
    deck_text: str, lookup: Optional[CardLookup] = None,
) -> Optional[float]:
    """Average mana value of the deck's NON-LAND cards, or None.

    Mirrors ``deck_dashboard``'s stat-tile computation (``round(sum/len,
    2)``, quantity-weighted, lands excluded) so the same deck produces the
    same number on both surfaces and the shared 2.6/3.8 curve bands mean the
    same thing. Cards Scryfall can't resolve are SKIPPED, not counted as 0 —
    a partial resolve yields the average of what we know, and a total
    failure yields None rather than a fabricated 0.0.
    """
    from .deck_library_analyzer import iter_deck_cards
    if lookup is None:
        from .scryfall_client import lookup_card as lookup
    cmcs: list[float] = []
    for qty, name in iter_deck_cards(deck_text or ""):
        try:
            data = lookup(name)
        except Exception:  # noqa: BLE001 — one bad card must not sink the lot
            continue
        if not data:
            continue
        if "land" in str(data.get("type_line") or "").lower():
            continue
        cmc = data.get("cmc")
        if cmc is None:
            continue
        try:
            cmc_val = float(cmc)
        except (TypeError, ValueError):
            continue
        cmcs.extend([cmc_val] * max(1, qty))
    if not cmcs:
        return None
    return round(sum(cmcs) / len(cmcs), 2)


def _derive_archetype(
    deck_text: str, deck_path: Optional[Path] = None,
) -> Optional[str]:
    """Archetype label for the deck, or None when unclassifiable.

    Path present -> ``archetype.classify`` (the canonical ladder: filename
    hint, then content scan, then its ``"midrange"`` default). Path absent
    or missing on disk -> the content scan alone, and NO default: a scan
    with no winner returns None ("unavailable") rather than inventing
    ``"midrange"``. Only ``combo`` / ``stax`` carry weight in the estimator,
    so a missing label costs nothing while a fabricated one would be a lie
    in the ``signals`` payload the UI renders.
    """
    if deck_path is not None:
        p = Path(deck_path)
        if p.exists():
            from .archetype import classify
            return classify(p)
    from . import dck_utils
    from .archetype import _content_scan
    winner, _score = _content_scan(dck_utils.main_card_names(deck_text or ""))
    return winner


# ---------------------------------------------------------------------------
# The estimator
# ---------------------------------------------------------------------------

def estimate_bracket(
    deck_text: str,
    declared: Optional[int] = None,
    *,
    avg_cmc: Optional[float] = None,
    archetype: Optional[str] = None,
    weights: Optional[dict[str, float]] = None,
) -> dict:
    """Estimate a deck's Commander bracket from its list.

    ``declared`` is the user's declared bracket (the ``[Bn]`` filename
    tag / dashboard query param); pass None when unknown. ``avg_cmc``
    and ``archetype`` are optional pre-computed context; the
    corresponding signals simply stay silent when absent. They are never
    recomputed HERE because that would need per-card Scryfall lookups,
    which would break this function's offline/never-blocks contract —
    callers that don't already have them should derive them once via
    :func:`derive_signals` and pass the result in.

    Returns (always — this function NEVER raises)::

        {
          "estimate": int,        # 1..5
          "floor": int,           # 1..5, the hard rule-derived bound
          "confidence": str,      # "low" | "medium" | "high"
          "reasons": [str],       # every rule/signal that fired, with
                                  # its contribution — the explainable
                                  # part of "explainable estimator"
          "signals": {..},        # raw signal values for programmatic
                                  # consumers / the UI details pane
          "declared": int|None,
          "mismatch": bool,       # |est - declared| >= 2 AND the
                                  # confidence is better than "low"
          "mismatch_level": None | "check" | "mismatch" | "low_signal",
        }

    ``"low_signal"`` replaces both other levels whenever the estimate
    is low-confidence (nothing fired / tiny list): the estimator has
    insufficient signal to dispute the declared bracket, so consumers
    should render "estimate unavailable/low-signal", never a mismatch
    warning.
    """
    try:
        return _estimate_bracket_inner(
            deck_text, declared,
            avg_cmc=avg_cmc, archetype=archetype,
            weights=weights,
        )
    except Exception:  # noqa: BLE001 — the never-raise contract
        # Degenerate fallback: B2 precon baseline, zero confidence.
        # Reached only if the inner pipeline has a bug — every signal
        # helper already fails soft individually.
        return {
            "estimate": declared if declared in (1, 2, 3, 4, 5) else 2,
            "floor": 1,
            "confidence": "low",
            "reasons": ["estimator error — defaulted to declared/baseline"],
            "signals": {},
            "declared": declared,
            "mismatch": False,
            "mismatch_level": None,
        }


def _estimate_bracket_inner(
    deck_text: str,
    declared: Optional[int],
    *,
    avg_cmc: Optional[float],
    archetype: Optional[str],
    weights: Optional[dict[str, float]],
) -> dict:
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    names = _deck_card_names(deck_text)
    names_set = set(names)

    reasons: list[str] = []

    # --- signal collection --------------------------------------------------
    n_gc, gc_names = _count_game_changers(names_set)
    combos = _detect_game_ending_combos(deck_text)
    n_two_card_combos = sum(
        1 for c in combos if len(c.get("cards") or []) <= 2
    )
    mld_hits = sorted(names_set & _MLD_CARDS)
    extra_turn_hits = sorted(names_set & _EXTRA_TURN_CARDS)
    extra_turn_repeatable_hits = sorted(
        names_set & _EXTRA_TURN_REPEATABLE_ENABLERS
    )
    extra_turn_oneshot_hits = sorted(names_set & _EXTRA_TURN_ONESHOT_REBUYS)
    tutor_hits = sorted(names_set & _TUTOR_CARDS)
    fast_mana_hits = sorted(names_set & _FAST_MANA_CARDS)
    salt_count = _offline_salt_count(names_set)

    # --- HARD FLOORS (official rules; floors only, never ceilings) ----------
    floor = 1
    if n_gc >= 1:
        # prompts/moxfield_audit_v3.md bracket table: B1/B2 GC limit is
        # ZERO — any Game Changer makes the deck at least B3 by rule.
        floor = max(floor, 3)
        reasons.append(
            f"floor B3: {n_gc} Game Changer(s) present "
            f"(B1/B2 allow zero — bracket rules table)"
        )
    if n_gc >= 4:
        # Same table: B3 caps at "Max 3" GCs. NOTE deliberately 4+, not
        # the 3+ that _power_bracket guesses at — 3 GCs is still a
        # legal B3 deck; the prompt table is authoritative here.
        floor = max(floor, 4)
        reasons.append(
            f"floor B4: {n_gc} Game Changers exceeds B3's max of 3 "
            f"(bracket rules table)"
        )
    if combos:
        # Defer to combo_detection.combo_bracket_floor per combo (the
        # ``bracket_floor`` annotation each entry already carries).
        # That is the repo's encoding of the OFFICIAL 2025-10-21 rule:
        # only CHEAP/EARLY-assembling two-card game-ending combos are
        # prohibited below B4; a late-assembling (~turn 6+, proxied by
        # summed mana value) two-card combo is B3-legal, as is any
        # 3+-card game-ending combo. This module used to hard-floor
        # EVERY two-card combo at B4 (the stricter pre-Oct-2025 beta
        # reading) — that slammed canonical B3 lists like Exquisite
        # Blood + Sanguine Bond up to Optimized. Cache-state wobble is
        # accepted: combo_bracket_floor already returns the STRICT B4
        # floor when a piece's mana value can't be resolved offline
        # (cold cache), so uncertainty degrades conservative, never
        # permissive.
        combo_floor = max(
            int(c.get("bracket_floor") or 1) for c in combos
        )
        n_early = sum(
            1 for c in combos if int(c.get("bracket_floor") or 1) >= 4
        )
        if combo_floor >= 4:
            floor = max(floor, 4)
            reasons.append(
                f"floor B4: {n_early} cheap/early two-card game-ending "
                f"combo(s) detected (combo_detection speed-refined "
                f"bracket floor; B1-B3 prohibit early two-card combos)"
            )
        elif combo_floor >= 3:
            floor = max(floor, 3)
            reasons.append(
                f"floor B3: {len(combos)} game-ending combo(s), all "
                f"late-game two-card or 3+-card lines (combo_detection "
                f"bracket floor; B3-legal per the 2025-10-21 rules)"
            )
    if mld_hits:
        # Mass land denial is prohibited below B4 (WotC guidance; the
        # audit prompt's MLD auto-bumper list is the repo encoding).
        floor = max(floor, 4)
        reasons.append(
            f"floor B4: mass land denial present ({', '.join(mld_hits)})"
        )
    # Extra turns: the 2025-10-21 rules target extra turns that are
    # "CHAINED OR LOOPED"; B3 explicitly allows "low quantities" of
    # non-chaining ones, so a bare count of 2 is no longer a floor
    # (that was the pre-Oct-2025 beta reading). OPERATIONALIZATION of
    # "chained or looped", from a static list: (a) 3+ extra-turn cards
    # — at that density the turns realistically cast back-to-back,
    # which is the chained experience the rule targets regardless of
    # intent; or (b) 2+ extra-turn cards alongside a REPEATABLE
    # rebuy/copy engine (_EXTRA_TURN_REPEATABLE_ENABLERS) that can
    # recur or duplicate them turn after turn — a credible loop.
    # ONE-SHOT rebuys (_EXTRA_TURN_ONESHOT_REBUYS — Eternal Witness,
    # Fork, bare Archaeomancer, ...) buy back a single turn once and
    # are spent: that cannot sustain a chain, so they contribute the
    # extra_turn_oneshot_rebuy weighted nudge instead of a floor (see
    # the repeatability split comment on the two frozensets). A single
    # extra-turn card never floors, even with recursion present (one
    # Time Warp + Archaeomancer is a slow 2-card value engine
    # combo_detection would flag separately if it were game-ending).
    extra_turns_chain = (
        len(extra_turn_hits) >= 3
        or (len(extra_turn_hits) >= 2 and extra_turn_repeatable_hits)
    )
    if extra_turns_chain:
        floor = max(floor, 4)
        if len(extra_turn_hits) >= 3:
            reasons.append(
                f"floor B4: {len(extra_turn_hits)} extra-turn cards — "
                f"density reads as chaining (B3 allows only low "
                f"quantities of non-chaining extra turns)"
            )
        else:
            reasons.append(
                f"floor B4: {len(extra_turn_hits)} extra-turn cards "
                f"plus repeatable recursion/copy engine "
                f"({', '.join(extra_turn_repeatable_hits[:3])}) — "
                f"chaining potential (the 2025-10-21 rules floor "
                f"chained-or-looped extra turns at B4)"
            )

    # --- WEIGHTED SIGNALS inside the bounds ---------------------------------
    # Base = 2.0: the stock-precon B2 "Core" baseline (prompt table).
    score = 2.0
    fired = 0  # distinct weighted signals that contributed

    if n_gc:
        pts = w["game_changer"] * min(n_gc, 5)
        score += pts
        fired += 1
        reasons.append(
            f"+{pts:.1f}: {n_gc} Game Changer(s) ({', '.join(gc_names[:5])}"
            f"{'…' if len(gc_names) > 5 else ''})"
        )
    if len(tutor_hits) >= 4:
        score += w["tutors_4_plus"]
        fired += 1
        reasons.append(
            f"+{w['tutors_4_plus']:.1f}: {len(tutor_hits)} tutors — "
            f"tutor-dense (consistency heuristic; not an official "
            f"bracket rule since the 2025-10-21 update)"
        )
    elif len(tutor_hits) >= 2:
        score += w["tutors_2_3"]
        fired += 1
        reasons.append(
            f"+{w['tutors_2_3']:.1f}: {len(tutor_hits)} tutors"
        )
    if fast_mana_hits:
        pts = w["fast_mana"] * min(len(fast_mana_hits), 4)
        score += pts
        fired += 1
        reasons.append(
            f"+{pts:.1f}: {len(fast_mana_hits)} fast-mana card(s) "
            f"({', '.join(fast_mana_hits[:4])})"
        )
    arch = (archetype or "").lower()
    if "combo" in arch:
        score += w["archetype_combo"]
        fired += 1
        reasons.append(
            f"+{w['archetype_combo']:.1f}: combo archetype "
            f"(_power_bracket nudge: combo decks are at least B3)"
        )
    elif "stax" in arch:
        score += w["archetype_stax"]
        fired += 1
        reasons.append(f"+{w['archetype_stax']:.1f}: stax archetype")
    if combos:
        pts = w["combo_line"] * min(len(combos), 2)
        score += pts
        fired += 1
        reasons.append(
            f"+{pts:.1f}: {len(combos)} game-ending combo line(s)"
        )
    if extra_turn_hits and not extra_turns_chain:
        # Non-chaining low quantities (1-2 cards): B3-legal by rule,
        # so a weighted nudge per card rather than a floor.
        pts = w["extra_turn_single"] * min(len(extra_turn_hits), 2)
        score += pts
        fired += 1
        reasons.append(
            f"+{pts:.1f}: {len(extra_turn_hits)} non-chaining "
            f"extra-turn card(s) ({', '.join(extra_turn_hits[:2])})"
        )
        if len(extra_turn_hits) >= 2 and extra_turn_oneshot_hits:
            # One-shot rebuys/copies alongside 2 extra-turn cards: a
            # single extra rebuy each, not a chain — the demoted form
            # of the old hard floor (see the repeatability split).
            pts = w["extra_turn_oneshot_rebuy"] * min(
                len(extra_turn_oneshot_hits), 2
            )
            score += pts
            fired += 1
            reasons.append(
                f"+{pts:.1f}: {len(extra_turn_oneshot_hits)} one-shot "
                f"extra-turn rebuy/copy piece(s) "
                f"({', '.join(extra_turn_oneshot_hits[:2])}) — single "
                f"rebuys, not a chain (no B4 floor)"
            )
    if avg_cmc is not None and avg_cmc > 0:
        # Curve bands lifted from deck_dashboard._power_bracket:
        # <=2.6 = tight/tuned, >3.4 = high-curve casual (we use >=3.8
        # for the penalty so the 3.4-3.8 middle stays neutral).
        if avg_cmc <= 2.6:
            score += w["curve_tight"]
            fired += 1
            reasons.append(
                f"+{w['curve_tight']:.1f}: tight curve "
                f"(avg CMC {avg_cmc:.2f} <= 2.6)"
            )
        elif avg_cmc >= 3.8:
            score += w["curve_high"]
            fired += 1
            reasons.append(
                f"{w['curve_high']:.1f}: high curve "
                f"(avg CMC {avg_cmc:.2f} >= 3.8)"
            )
    if salt_count is not None and salt_count >= _SALT_COUNT_TRIGGER:
        score += w["salty_pile"]
        fired += 1
        reasons.append(
            f"+{w['salty_pile']:.1f}: {salt_count} cards at/above "
            f"EDHREC salt {_SALT_THRESHOLD} (offline cache)"
        )

    estimate = int(round(score))
    estimate = max(floor, min(5, max(1, estimate)))

    # --- confidence ---------------------------------------------------------
    # "high": a hard rule fired (definitional, not statistical) or 3+
    # independent weighted signals agree. "low": the list is too small
    # to mean anything (< 20 cards — partial paste / all-lands stub)
    # or nothing fired at all. Else "medium".
    if floor > 1 or fired >= 3:
        confidence = "high"
    elif len(names) < 20 or fired == 0:
        confidence = "low"
    else:
        confidence = "medium"
    if len(names) < 20:
        # Tiny lists can't be high-confidence no matter what fired.
        confidence = "low"

    # --- declared-vs-estimated ----------------------------------------------
    # Confidence-gated (see _MISMATCH_HARD_DIFF policy comment): a
    # low-confidence estimate is signal starvation, not evidence, so
    # any disagreement reports "low_signal" instead of check/mismatch
    # and the boolean stays False.
    mismatch_level: Optional[str] = None
    if declared is not None:
        diff = abs(estimate - declared)
        if diff >= 1 and confidence == "low":
            mismatch_level = "low_signal"
        elif diff >= _MISMATCH_HARD_DIFF:
            mismatch_level = "mismatch"
        elif diff >= 1:
            mismatch_level = "check"

    return {
        "estimate": estimate,
        "floor": floor,
        "confidence": confidence,
        "reasons": reasons,
        "signals": {
            "n_game_changers": n_gc,
            "game_changers": gc_names,
            "n_game_ending_combos": len(combos),
            "n_two_card_combos": n_two_card_combos,
            "mld_cards": mld_hits,
            "extra_turn_cards": extra_turn_hits,
            # Split by repeatability (2025-10-21 "chained or looped"):
            # only the repeatable engines participate in the B4 floor.
            # "extra_turn_chain_enablers" keeps the pre-split key name
            # for payload consumers and now carries the floor-relevant
            # (repeatable) hits.
            "extra_turn_chain_enablers": extra_turn_repeatable_hits,
            "extra_turn_repeatable_enablers": extra_turn_repeatable_hits,
            "extra_turn_oneshot_rebuys": extra_turn_oneshot_hits,
            "tutor_count": len(tutor_hits),
            "tutors": tutor_hits,
            "fast_mana_count": len(fast_mana_hits),
            "fast_mana": fast_mana_hits,
            "avg_cmc": avg_cmc,
            "archetype": archetype,
            "salt_count": salt_count,
            "score_raw": round(score, 2),
            "card_count": len(names),
        },
        "declared": declared,
        "mismatch": mismatch_level == "mismatch",
        "mismatch_level": mismatch_level,
    }


# ---------------------------------------------------------------------------
# Pool-hygiene helper — shared by pool_curator + meta_test
# ---------------------------------------------------------------------------

def mismatch_warning(
    filename: str,
    deck_text: str,
    declared: Optional[int],
) -> Optional[str]:
    """One-line WARN string when a deck's estimated bracket differs
    from its declared ``[Bn]`` tag by >= 2 — or None when it doesn't.

    Print-only by contract: mislabeled decks poison sim pools (a B4
    list tagged [B2] farms wins off genuine B2 decks), but the
    estimator is a heuristic, so callers WARN and never reject.
    Shared by pool_curator's candidate listing and meta_test's
    reference importer so both surfaces phrase the warning identically.
    Never raises (estimate_bracket guarantees it).

    Confidence-aware: when the estimate is LOW-confidence and would
    have flagged (diff >= 2), the return is a NOTE line saying the
    estimate is low-signal — distinct copy, so a starved estimator
    never accuses a declared tag it has no evidence against. Diff-1
    disagreements stay silent at every confidence (soft "check" is
    dashboard territory).
    """
    if declared is None or declared == 0:
        return None
    result = estimate_bracket(deck_text, declared=declared)
    if result.get("mismatch"):
        return (
            f"WARN: {filename} declares B{declared} but estimates "
            f"B{result['estimate']} ({result['confidence']} confidence) — "
            f"mislabeled decks poison sim pools. "
            f"Top reason: {result['reasons'][0] if result['reasons'] else 'n/a'}"
        )
    if (
        result.get("mismatch_level") == "low_signal"
        and abs(result["estimate"] - declared) >= _MISMATCH_HARD_DIFF
    ):
        return (
            f"NOTE: {filename} declares B{declared}; bracket estimate "
            f"unavailable/low-signal: B{result['estimate']}? "
            f"(insufficient signal — not flagged as mismatch)"
        )
    return None

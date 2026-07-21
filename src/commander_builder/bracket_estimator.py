"""Explainable Commander-bracket estimator (ManaFoundry parity).

Estimates a deck's WotC Commander bracket (1-5) from its list alone and
surfaces estimated-vs-declared so mislabeled decks get flagged before
they poison sim pools or mislead the dashboard.

WHERE THE RULES COME FROM — this module deliberately invents nothing.
Every hard bound and weighted signal cites an existing encoding of the
official bracket rules already in this repo:

  * ``prompts/moxfield_audit_v3.md`` "BRACKET RULES" table — the
    repo's canonical transcription of WotC's per-bracket Game Changer
    caps: B1/B2 allow ZERO Game Changers, B3 allows a MAX of 3,
    B4/B5 are unlimited.
  * The same prompt's "Auto-Bracket Bumper Heuristic" reference data —
    the LAND DESTRUCTION/MLD and EXTRA TURNS card lists reproduced
    below, and the "stacking 4+ tutors auto-bumps" rule the tutor
    signal implements.
  * ``game_changers.py`` — the official Game Changers list
    (``load_game_changers``), dynamic-fetch + offline fallback.
  * ``combo_detection.py`` — ``combo_bracket_floor``: a game-ending
    TWO-card combo floors a deck at B4 (WotC: B1-B3 prohibit
    early-game two-card infinite combos), a 3+-card game-ending combo
    floors at B3.
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
        estimate_bracket, mismatch_warning,
    )

    result = estimate_bracket(deck_text, declared=3)
    # -> {"estimate": 3, "floor": 3, "confidence": "high",
    #     "reasons": [...], "signals": {...}, "declared": 3,
    #     "mismatch": False, "mismatch_level": None}
"""

from __future__ import annotations

import json
from typing import Optional

# ---------------------------------------------------------------------------
# Rule data — name-based card lists, each citing its repo source
# ---------------------------------------------------------------------------

# Mass land denial. Source: prompts/moxfield_audit_v3.md, "Auto-Bracket
# Bumper Heuristic" -> "LAND DESTRUCTION/MLD" list (verbatim). WotC's
# bracket guidance prohibits mass land denial in brackets 1-3, so ANY
# of these is a hard B4 floor — see _hard_floor below.
_MLD_CARDS = frozenset(c.lower() for c in (
    "Armageddon", "Ravages of War", "Catastrophe", "Cataclysm",
    "Wildfire", "Obliterate", "Jokulhaups", "Decree of Annihilation",
))

# Extra-turn spells. Source: prompts/moxfield_audit_v3.md "EXTRA TURNS"
# list (verbatim). WotC: B1/B2 no extra turns; B3 allows them only when
# not CHAINED. We can't simulate chaining from a static list, so we use
# the same conservative proxy as combo_detection uses for "early game":
# TWO OR MORE extra-turn cards = chaining potential = hard B4 floor;
# a single one is only a weighted nudge (B3-legal by rule).
_EXTRA_TURN_CARDS = frozenset(c.lower() for c in (
    "Time Warp", "Temporal Manipulation", "Walk the Aeons",
    "Time Stretch", "Nexus of Fate", "Expropriate",
))

# Tutors, for the density signal. Sources:
#   * the Game-Changer tutors from game_changers._FALLBACK (Demonic /
#     Vampiric / Mystical / Worldly / Enlightened Tutor, Imperial Seal,
#     Gamble) — they also count in the GC signal, which is correct:
#     they carry both kinds of bracket pressure;
#   * the prompt's "TUTORS (mass)" auto-bumper list (Diabolic Intent,
#     Grim Tutor, Personal Tutor, Sylvan Tutor);
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
    # Tutor density. The prompt's mass-tutor rule is a step, not a
    # slope: "stacking 4+ tutors auto-bumps". 2-3 tutors = half signal.
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
    # One extra-turn card (B3-legal; 2+ is a hard floor instead).
    "extra_turn_single": 0.25,
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
    and ``archetype`` are optional pre-computed context (the dashboard
    already has both; CLI callers usually don't — the corresponding
    signals simply stay silent when absent, they are never recomputed
    here because that would need per-card Scryfall lookups).

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
          "mismatch": bool,       # |est - declared| >= 2
          "mismatch_level": None | "check" | "mismatch",
        }
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
    if n_two_card_combos:
        # combo_detection.combo_bracket_floor: a game-ending TWO-card
        # combo floors at B4 (WotC: B1-B3 prohibit early-game two-card
        # infinite combos; "early" is unmeasurable from a list so the
        # conservative reading applies).
        floor = max(floor, 4)
        reasons.append(
            f"floor B4: {n_two_card_combos} game-ending two-card "
            f"combo(s) detected (combo_detection bracket floor)"
        )
    elif combos:
        # 3+-card game-ending combos: combo_bracket_floor says B3
        # (more setup = later; still a deliberate combo finish).
        floor = max(floor, 3)
        reasons.append(
            f"floor B3: {len(combos)} game-ending combo(s) of 3+ cards "
            f"(combo_detection bracket floor)"
        )
    if mld_hits:
        # Mass land denial is prohibited below B4 (WotC guidance; the
        # audit prompt's MLD auto-bumper list is the repo encoding).
        floor = max(floor, 4)
        reasons.append(
            f"floor B4: mass land denial present ({', '.join(mld_hits)})"
        )
    if len(extra_turn_hits) >= 2:
        # 2+ extra-turn spells = chaining potential; B3 only allows
        # UN-chained extra turns (audit prompt EXTRA TURNS list).
        floor = max(floor, 4)
        reasons.append(
            f"floor B4: {len(extra_turn_hits)} extra-turn cards "
            f"(chaining potential; B3 allows only un-chained extra turns)"
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
            f"'stacking 4+ tutors auto-bumps' (audit prompt)"
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
    if len(extra_turn_hits) == 1:
        score += w["extra_turn_single"]
        fired += 1
        reasons.append(
            f"+{w['extra_turn_single']:.1f}: one extra-turn card "
            f"({extra_turn_hits[0]})"
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
    mismatch_level: Optional[str] = None
    if declared is not None:
        diff = abs(estimate - declared)
        if diff >= _MISMATCH_HARD_DIFF:
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
    """
    if declared is None or declared == 0:
        return None
    result = estimate_bracket(deck_text, declared=declared)
    if not result.get("mismatch"):
        return None
    return (
        f"WARN: {filename} declares B{declared} but estimates "
        f"B{result['estimate']} ({result['confidence']} confidence) — "
        f"mislabeled decks poison sim pools. "
        f"Top reason: {result['reasons'][0] if result['reasons'] else 'n/a'}"
    )

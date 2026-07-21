"""FP-012 Slice A -- intent-learning for the deck-improvement agent.

``learn_intent(deck_path)`` composes the existing classifiers to
produce an ``Intent`` dataclass that captures what a deck is trying to
do -- its archetype, themes, key win-conditions, and commander color
identity.  The result is used by ``run_improve_loop`` to:

  1. **Soft-bias** the advisor's candidate adds toward the intent's
     themes (lower-priority signal -- the win-margin objective stays
     primary).
  2. **Auto-protect** the intent's key win-cons / signature synergy
     pieces by extending the per-round protected-card list so the
     curator can't accidentally cut the deck's identity.

Why soft-bias + protect, not a hard constraint (reject swaps that
change archetype): hard constraints risk stalling the loop on noisy
sims; soft-bias + protect keeps the optimizer in control while giving
the intent a meaningful voice.  See ``docs/archive/fp012-next-slices.md``
(Slice A design decision).

Callers
-------
- ``improve.py`` threads ``Intent`` through ``run_improve_loop`` and
  appends ``intent.key_wincons`` to the per-round protected list.
- ``improve_main`` exposes ``--learn-intent <dck>`` to the CLI.
- Tests inject a stub ``classify_fn`` / ``themes_fn`` / ``lookup_fn``
  so no real Forge / Anthropic / Scryfall is needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import dck_utils


@dataclass
class Intent:
    """Captured intent for one deck.

    Attributes
    ----------
    archetype:
        One of ``aggro | midrange | control | combo | stax``
        (from ``archetype.classify``).
    themes:
        EDHREC tag slugs that the deck strongly cares about
        (from ``staples.detect_themes``). Up to 3.
    key_wincons:
        Card names detected as win-conditions or high-synergy
        pieces via ``staples.classify_role_extended``.  These are
        added to the per-round protected-card list.
    color_identity:
        WUBRG letter list for the primary commander, e.g.
        ``["W", "U"]``.  Empty list when no commander is found or
        Scryfall is unavailable.
    tribal_type:
        Commander's primary tribal type (e.g. ``"Goblin"``), or
        ``None`` for non-tribal decks.
    commander_name:
        Canonical name of the primary commander card, or ``None``.
    """

    archetype: str = "midrange"
    themes: list[str] = field(default_factory=list)
    key_wincons: list[str] = field(default_factory=list)
    color_identity: list[str] = field(default_factory=list)
    tribal_type: Optional[str] = None
    commander_name: Optional[str] = None

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_deck_text(deck_path: Path) -> str:
    """Read a .dck file, returning '' on any I/O error."""
    try:
        return deck_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _parse_commander_names(deck_text: str) -> list[str]:
    """Extract card names from the [Commander] section of a .dck file.

    Thin wrapper over ``dck_utils.section_card_names``."""
    return dck_utils.section_card_names(deck_text, "Commander")


def _parse_main_card_names(deck_text: str) -> list[str]:
    """Extract card names from the [Main] section of a .dck file.

    Thin wrapper over ``dck_utils.main_card_names``."""
    return dck_utils.main_card_names(deck_text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def learn_intent(
    deck_path: Path,
    *,
    # Injectable classifiers for tests (default to the real implementations).
    classify_fn: Optional[Callable[[Path], str]] = None,
    themes_fn: Optional[Callable[[list[tuple[str, str]]], list[str]]] = None,
    lookup_fn: Optional[Callable[[str], Optional[dict]]] = None,
    role_fn: Optional[Callable[[str, str], str]] = None,
    tribal_fn: Optional[Callable[[str, str], Optional[str]]] = None,
) -> Intent:
    """Compose existing classifiers to learn a deck's intent.

    Steps
    -----
    1. Archetype: ``archetype.classify`` (filename hint -> content scan
       -> midrange fallback).
    2. Themes: ``staples.detect_themes`` over the deck's oracle texts.
    3. Win-cons: scan each card in [Main] with
       ``staples.classify_role_extended``; cards that return
       ``"win_condition"`` or ``"finisher"`` become ``key_wincons``.
    4. Color identity: Scryfall lookup of the primary commander.
    5. Tribal type: ``staples.detect_tribal_type`` on the commander's
       oracle text.

    All injectable so tests never need real Forge / Anthropic /
    Scryfall.  The default implementations are imported lazily to keep
    the module importable even when optional extras are absent.

    Parameters
    ----------
    deck_path:
        Absolute path to a local ``.dck`` file.
    classify_fn:
        ``(deck_path) -> archetype_str`` -- defaults to
        ``archetype.classify``.
    themes_fn:
        ``(list[(name, oracle_text)]) -> list[str]`` -- defaults to
        ``staples.detect_themes``.
    lookup_fn:
        ``(card_name) -> Optional[dict]`` -- defaults to
        ``scryfall_client.lookup_card``.  Returns the Scryfall card
        object or ``None``/exception on failure.
    role_fn:
        ``(oracle_text, type_line) -> role_str`` -- defaults to
        ``staples.classify_role_extended``.
    tribal_fn:
        ``(oracle_text, type_line) -> Optional[str]`` -- defaults to
        ``staples.detect_tribal_type``.
    """
    # ------------------------------------------------------------------
    # 1. Resolve real implementations (lazy imports keep startup fast).
    # ------------------------------------------------------------------
    if classify_fn is None:
        from .archetype import classify as _classify
        classify_fn = _classify
    if themes_fn is None:
        from .staples import detect_themes as _detect_themes
        themes_fn = _detect_themes
    if lookup_fn is None:
        try:
            from .scryfall_client import lookup_card as _lookup
            lookup_fn = _lookup
        except Exception:  # noqa: BLE001
            def _no_lookup(name: str) -> Optional[dict]:
                return None
            lookup_fn = _no_lookup
    if role_fn is None:
        from .staples import classify_role_extended as _role
        role_fn = _role
    if tribal_fn is None:
        from .staples import detect_tribal_type as _tribal
        tribal_fn = _tribal

    # ------------------------------------------------------------------
    # 2. Read the deck file.
    # ------------------------------------------------------------------
    deck_text = _read_deck_text(deck_path)
    main_cards = _parse_main_card_names(deck_text)
    commander_names = _parse_commander_names(deck_text)
    commander_name: Optional[str] = commander_names[0] if commander_names else None

    # ------------------------------------------------------------------
    # 3. Archetype classification.
    # ------------------------------------------------------------------
    try:
        archetype = classify_fn(deck_path)
    except Exception:  # noqa: BLE001
        archetype = "midrange"

    # ------------------------------------------------------------------
    # 4. Theme detection -- needs oracle texts.  Fetch each card lazily;
    #    skip cards that fail Scryfall lookup (offline / unknown names).
    # ------------------------------------------------------------------
    deck_oracles: list[tuple[str, str]] = []
    for card_name in main_cards:
        try:
            data = lookup_fn(card_name)
            oracle = (data.get("oracle_text") or "") if data else ""
        except Exception:  # noqa: BLE001
            oracle = ""
        deck_oracles.append((card_name, oracle))

    try:
        themes = themes_fn(deck_oracles)
    except Exception:  # noqa: BLE001
        themes = []

    # ------------------------------------------------------------------
    # 5. Win-con detection -- any main-deck card whose extended role is
    #    "win_condition" or "finisher" is a key win-con to protect.
    # ------------------------------------------------------------------
    key_wincons: list[str] = []
    for card_name, oracle in deck_oracles:
        if not oracle:
            continue
        # Fetch the full card for the type_line so role_fn can handle
        # land priority correctly.
        try:
            data = lookup_fn(card_name)
            type_line = (data.get("type_line") or "") if data else ""
        except Exception:  # noqa: BLE001
            type_line = ""
        try:
            role = role_fn(oracle, type_line)
        except Exception:  # noqa: BLE001
            role = "other"
        if role in ("win_condition", "finisher"):
            key_wincons.append(card_name)

    # ------------------------------------------------------------------
    # 6. Color identity -- Scryfall lookup of the primary commander.
    # ------------------------------------------------------------------
    color_identity: list[str] = []
    tribal_type: Optional[str] = None
    if commander_name:
        try:
            cmd_data = lookup_fn(commander_name)
            if cmd_data:
                color_identity = list(cmd_data.get("color_identity") or [])
                # 7. Tribal type from commander oracle text.
                cmd_oracle = cmd_data.get("oracle_text") or ""
                cmd_type = cmd_data.get("type_line") or ""
                try:
                    tribal_type = tribal_fn(cmd_oracle, cmd_type)
                except Exception:  # noqa: BLE001
                    tribal_type = None
        except Exception:  # noqa: BLE001
            pass

    return Intent(
        archetype=archetype,
        themes=themes,
        key_wincons=key_wincons,
        color_identity=color_identity,
        tribal_type=tribal_type,
        commander_name=commander_name,
    )


def intent_protect_cards(intent: Optional["Intent"]) -> list[str]:
    """Extract the protect-list extension implied by ``intent``.

    Returns ``intent.key_wincons`` when an intent is present, else an
    empty list.  Used by ``improve.py`` to extend the per-round
    ``--protect`` list without coupling the loop logic to the ``Intent``
    internals.
    """
    if intent is None:
        return []
    return list(intent.key_wincons)

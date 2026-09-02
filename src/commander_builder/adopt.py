"""FP-018.3 — ``commander adopt``: understand a deck, then gently make it yours.

The improve loop OPTIMIZES; this flow ADOPTS. A player picks up a deck
(usually imported with its primer sidecar, ``primer.py``) and the app
helps them (1) understand it — the primer's plan cross-checked against
the actual list — and (2) make small, identity-preserving changes
steered by what THEY like. Two outputs, in that order, both produced by
:func:`adopt_deck`.

DETERMINISTIC AND OFFLINE BY DESIGN. The explanation is computed from
the list, the oracle snapshot (cache-only, never the network) and the
stored primer text — no LLM anywhere in this module. An LLM-polish pass
was deliberately NOT built: the deterministic output is the contract,
and any future polish option must be opt-in and degrade to exactly this.

WHAT THE PRIMER IS TRUSTED FOR (harvest evidence, 2026-08-27 — see
``primer.py``'s module docstring):

* **Exact card references come ONLY from card-link embeds.** Prose primers
  name cards with typos and nicknames, so free text is never mined for
  names. Links are evidence about what the primer discusses, not an
  instruction to lock a card: only explicit ``Protect=`` metadata prevents
  a cut. Prose is still USED — read-only — to confirm
  which deck cards the primer talks about (matching a KNOWN list name
  into the text is lookup, not NLP) and to quote the author's own
  win-line paragraphs verbatim (``primer.quoted_win_lines``).
* **No primer is the COMMON case** (~75% of even top-ranked harvested
  decks). The explanation then grounds itself in the list alone —
  themes, role distribution, win-condition cards — and the preference
  pass still runs. Absence is stated, never an error.

THE OVERHAUL PATH IS STRUCTURALLY OFF THE TABLE. Suggestions reuse
``deck_builder_personalize.lift_swaps`` — the FP-014.3 like-for-like
pass, which preserves deck size, singleton and role counts by
construction — with the swap budget clamped to :data:`POLISH_MAX_SWAPS`,
a constant read from ``change_budget.TIER_CAPS["polish"]``. There is no
mode parameter, no call to ``resolve_tier``, and no read of
``COMMANDER_BUILDER_REBUILD_TIER`` anywhere in this module: the rebuild
tier is not defaulted off, it is UNREACHABLE from this code path. If a
deck is genuinely misbuilt for its primer, adopt says so and points at
``commander improve``; it does not become it.

Grounding rule (FP-018 non-goal): every card named in the output is
either in the list or resolved through the oracle cache — free text
steers attention, it never invents card facts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional

from . import dck_utils, primer
from .change_budget import TIER_CAPS
from .intent import free_text_theme_slugs

#: The hard swap-suggestion ceiling: the polish tier's add budget (5).
#: Read from TIER_CAPS so the two numbers cannot drift, but bound at
#: import time to a plain int — nothing at call time (flag, env, score)
#: can raise it. This constant is the whole reason rebuild is
#: structurally unreachable here; see the module docstring.
POLISH_MAX_SWAPS: int = TIER_CAPS["polish"][0]

#: Printed whenever the deck has no user-authored protection metadata.
#: Primer links remain exact-name evidence, but never imply permission to
#: lock cards against otherwise valid suggestions.
NO_EXPLICIT_PROTECT_NOTE = (
    "no explicit Protect= locks are configured in [metadata]. Primer card "
    "links are references only and do not lock cards; suggestions still "
    "preserve roles and never touch the commander or lands. Add Protect= "
    "lines to pin specific cards."
)


def _lookup_cache_only(name: str) -> Optional[dict]:
    """Default resolver: the on-disk oracle snapshot, never the network.

    Same seam as ``_deck_judge_prompt._lookup_cache_only`` — adopt runs
    as a read-only comprehension flow and must never hang on a per-card
    network round-trip.
    """
    from .scryfall_client import lookup_card
    try:
        return lookup_card(name, cache_only=True)
    except Exception:  # noqa: BLE001 — a bad name is a miss, not a crash
        return None


# ---------------------------------------------------------------------------
# Output 1 — UNDERSTAND: the grounded explanation
# ---------------------------------------------------------------------------

def explain_deck(
    deck_text: str,
    primer_text: Optional[str],
    card_links: Optional[list[str]] = None,
    *,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """The deterministic explanation payload for one deck.

    ``primer_text`` / ``card_links`` come from the sidecar (or are
    None/empty when the deck has no primer — the common case, which
    yields the list-grounded sections and an explicit "no primer"
    marker, never an error).

    Cross-check semantics:

    * ``linked_present`` / ``linked_absent`` — the card-link embeds
      (exact names) split by actual list membership. ``linked_absent``
      is the primer-vs-list DISAGREEMENT signal: the author links a
      card the list does not run.
    * ``prose_mentions`` — deck cards whose full name appears in the
      primer prose (casefolded containment of a KNOWN name; deliberately
      not the reverse direction, which would be name-guessing over free
      text).
    """
    lookup = lookup or _lookup_cache_only
    from .staples import (
        card_theme_slugs,
        classify_role_extended,
        detect_themes,
    )

    commanders = dck_utils.section_card_names(deck_text, "Commander")
    main_cards = dck_utils.main_card_names(deck_text)
    main_keys = {n.casefold() for n in main_cards}
    all_keys = main_keys | {n.casefold() for n in commanders}

    # One resolve pass; every downstream section reads from it.
    cards: dict[str, dict] = {}
    unresolved = 0
    for name in main_cards:
        card = lookup(name) or {}
        cards[name] = card
        if not (card.get("oracle_text") or card.get("type_line")):
            unresolved += 1

    roles: dict[str, int] = {}
    wincons: list[str] = []
    deck_oracles: list[tuple[str, str]] = []
    for name, card in cards.items():
        oracle = card.get("oracle_text") or ""
        type_line = card.get("type_line") or ""
        deck_oracles.append((name, oracle))
        if not oracle and not type_line:
            continue
        role = classify_role_extended(oracle, type_line)
        roles[role] = roles.get(role, 0) + 1
        if role in ("win_condition", "finisher"):
            wincons.append(name)

    themes = detect_themes(deck_oracles)
    theme_packages: dict[str, list[str]] = {}
    for slug in themes:
        members = [
            name for name, card in cards.items()
            if slug in card_theme_slugs(card.get("oracle_text") or "")
        ]
        theme_packages[slug] = members[:8]  # examples, not an inventory

    links = list(card_links or [])
    linked_present = [n for n in links if n.casefold() in all_keys]
    linked_absent = [n for n in links if n.casefold() not in all_keys]

    prose_mentions: list[str] = []
    if primer_text:
        folded = primer_text.casefold()
        linked_keys = {n.casefold() for n in links}
        prose_mentions = [
            n for n in commanders + main_cards
            if n.casefold() in folded and n.casefold() not in linked_keys
        ]

    notes: list[str] = []
    if unresolved:
        notes.append(
            f"{unresolved} of {len(main_cards)} cards are not in the local "
            f"oracle snapshot — role/theme counts under-report them "
            f"(run commander oracle-refresh to close the gap)."
        )
    if linked_absent:
        # The FP-018 non-goal, said where it matters: adopt reports the
        # mismatch and points at the improve loop; it does not overhaul.
        notes.append(
            f"the primer links {len(linked_absent)} card(s) the list does "
            f"NOT run ({', '.join(linked_absent)}) — the deck may have "
            f"drifted from its primer. Reconciling that is the improve "
            f"loop's job (commander improve), not adopt's."
        )

    from .deck_legality import validate_deck
    legality = validate_deck(deck_text, lookup=lookup).to_dict()

    return {
        "commanders": commanders,
        "main_count": dck_utils.count_main_cards(deck_text),
        "legality": legality,
        "primer": {
            "present": bool(primer_text),
            "words": primer.primer_word_count(primer_text or ""),
            "card_links": links,
            "linked_present": linked_present,
            "linked_absent": linked_absent,
            "prose_mentions": prose_mentions,
            "win_lines": primer.quoted_win_lines(primer_text),
        },
        "themes": themes,
        "theme_packages": theme_packages,
        "roles": dict(sorted(roles.items(), key=lambda kv: -kv[1])),
        "wincons": wincons,
        "unresolved": unresolved,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Output 2 — PERSONALIZE: polish-capped, preference-steered suggestions
# ---------------------------------------------------------------------------

def personalize_suggestions(
    deck_text: str,
    *,
    preferences: Optional[str],
    protected: list[str],
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
    matrix: Optional[dict] = None,
    max_swaps: int = POLISH_MAX_SWAPS,
) -> dict:
    """Suggest like-for-like swaps, biased by the pilot's own words.

    Reuses ``deck_builder_personalize.lift_swaps`` (FP-014.3) rather
    than restating it — its net-zero/same-role/singleton machinery is
    exactly the "small, not crazy" contract — via the two seams added
    for this flow:

    * ``prefer``: candidates whose oracle text matches a theme the
      pilot's free text mentions are tried FIRST for the bounded swap
      budget. Soft by construction — order is all it changes; nothing
      is filtered for being un-preferred (``free_text_theme_slugs``
      maps the prose to the same slug vocabulary themes already use).
    * ``protect``: the caller's explicitly protected names (Protect=
      metadata lines, not primer links) are never swapped out.

    ``max_swaps`` is CLAMPED to :data:`POLISH_MAX_SWAPS` — a caller may
    ask for fewer, never more. Suggestions are returned, not applied:
    adopt writes nothing to the deck.
    """
    lookup = lookup or _lookup_cache_only
    from . import deck_builder_personalize as personalize
    from .staples import (
        card_theme_slugs,
        classify_role_extended,
        is_basic_land,
    )

    # The clamp that keeps every wider tier unreachable (see module
    # docstring): whatever the caller asked for, the polish add-budget
    # is the ceiling.
    max_swaps = max(0, min(int(max_swaps), POLISH_MAX_SWAPS))

    commanders = dck_utils.section_card_names(deck_text, "Commander")
    main_cards = dck_utils.main_card_names(deck_text)

    def _card(nm: str) -> dict:
        return lookup(nm) or {}

    def _is_land(nm: str) -> bool:
        type_line = _card(nm).get("type_line") or ""
        front = type_line.split("//")[0].lower()
        return "land" in front or (not type_line and is_basic_land(nm))

    nonlands = [n for n in main_cards if not _is_land(n)]
    lands = [n for n in main_cards if _is_land(n)]

    from .collection import name_key
    reserved = {name_key(n) for n in commanders}
    reserved |= {name_key(n) for n in lands}

    def role_of(nm: str) -> str:
        card = _card(nm)
        return classify_role_extended(
            card.get("oracle_text") or "", card.get("type_line") or "")

    ci: Optional[set] = None
    for cmdr in commanders:
        ident = _card(cmdr).get("color_identity")
        if ident:
            ci = (ci or set()) | {str(c).upper() for c in ident}

    def ci_ok(nm: str) -> bool:
        if ci is None:
            return True  # unresolvable identity degrades open, as elsewhere
        ident = _card(nm).get("color_identity")
        if ident is None:
            return False  # unresolvable candidate: refuse, don't guess
        return {str(c).upper() for c in ident} <= ci

    pref_slugs = free_text_theme_slugs(preferences)

    def prefer(nm: str) -> float:
        if not pref_slugs:
            return 0.0
        slugs = card_theme_slugs(_card(nm).get("oracle_text") or "")
        return float(len(slugs & set(pref_slugs)))

    protected_keys = {p.casefold() for p in protected}

    def protect(nm: str) -> bool:
        return nm.casefold() in protected_keys

    if not commanders:
        return {"suggestions": [], "skipped": "no [Commander] section",
                "max_swaps": max_swaps, "tier": "polish",
                "preference_slugs": pref_slugs}

    new_nonlands, notes, skipped = personalize.lift_swaps(
        nonlands,
        commander=commanders[0],
        partner=commanders[1] if len(commanders) > 1 else None,
        bracket=None,
        matrix=matrix,
        reserved_keys=reserved,
        role_of=role_of,
        ci_ok=ci_ok,
        max_swaps=max_swaps,
        prefer=prefer if pref_slugs else None,
        protect=protect if protected_keys else None,
    )

    suggestions: list[dict] = []
    for old, new in zip(nonlands, new_nonlands):
        if old == new:
            continue
        # Attach lift's own rationale by exact-name match — the note list
        # is in candidate order while this walk is in deck order, so a
        # positional pairing could misattribute rationales.
        rationale = next(
            (n for n in notes if n.startswith(f"swapped {old} for {new}")),
            "")
        matched = card_theme_slugs(_card(new).get("oracle_text") or "")
        served = sorted(matched & set(pref_slugs))
        suggestions.append({
            "out": old,
            "in": new,
            "rationale": rationale,
            # Every suggestion says which preference it serves and what
            # it preserves (FP-018.3's output contract).
            "serves": (f"your stated preference: {', '.join(served)}"
                       if served else "corpus synergy (lift co-occurrence)"),
            "preserves": (
                f"role balance ({role_of(old)} for {role_of(new)}), deck "
                f"size, singleton, and every protected card"
            ),
        })

    return {
        "suggestions": suggestions,
        "skipped": skipped,
        "max_swaps": max_swaps,
        "tier": "polish",
        "preference_slugs": pref_slugs,
    }


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------

def adopt_deck(
    deck_path: Path,
    *,
    preferences: Optional[str] = None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
    matrix: Optional[dict] = None,
    deck_dir: Optional[Path] = None,
    max_swaps: int = POLISH_MAX_SWAPS,
) -> dict:
    """UNDERSTAND then PERSONALIZE one deck. Returns the full payload.

    ``matrix`` (injectable for tests) defaults to the on-disk lift
    corpus for ``deck_dir`` (or the deck's own directory); no corpus
    just skips the suggestion pass with lift's own honest reason.
    """
    deck_path = Path(deck_path)
    deck_text = deck_path.read_text(encoding="utf-8")
    lookup = lookup or _lookup_cache_only

    primer_text = primer.read_primer_sidecar(deck_path)
    card_links = primer.read_primer_card_links(deck_path)

    explanation = explain_deck(
        deck_text, primer_text, card_links, lookup=lookup)

    # Only an explicit Protect= line is a lock. Primer card-link embeds
    # stay in explanation.primer as exact-name evidence; treating a link
    # as consent to prevent a cut made ordinary primer references sticky.
    from .web._helpers import read_protected_cards
    protected = list(read_protected_cards(deck_text))
    protection_note = None if protected else NO_EXPLICIT_PROTECT_NOTE

    if matrix is None:
        from . import lift_analysis
        try:
            matrix = lift_analysis.load_or_build_matrix(
                Path(deck_dir) if deck_dir else deck_path.parent)
        except Exception:  # noqa: BLE001 — no corpus, no suggestions
            matrix = None

    suggestions = personalize_suggestions(
        deck_text,
        preferences=preferences,
        protected=protected,
        lookup=lookup,
        matrix=matrix,
        max_swaps=max_swaps,
    )
    suggestions["protected"] = protected
    suggestions["protection_note"] = protection_note

    return {
        "deck": deck_path.name,
        "explanation": explanation,
        "personalize": suggestions,
    }


def render_adoption(payload: dict) -> str:
    """Human-readable report for one :func:`adopt_deck` payload."""
    exp = payload["explanation"]
    per = payload["personalize"]
    pr = exp["primer"]
    lines: list[str] = []
    add = lines.append

    add(f"=== ADOPT: {payload['deck']} ===")
    add(f"commander: {', '.join(exp['commanders']) or '(none found)'}"
        f"  |  {exp['main_count']} main-deck cards")
    add("")

    legality = exp.get("legality")
    if legality:
        add("-- Rules check (warning only) --")
        if legality["status"] == "legal":
            add("  no confirmed Commander rules problems found")
        elif legality["status"] == "unverified":
            add("  no confirmed rules problems, but some checks were unavailable")
        else:
            add("  confirmed rules problems found; suggestions remain advisory")
        for item in legality.get("violations") or []:
            cards = f" ({', '.join(item['cards'])})" if item["cards"] else ""
            add(f"  WARNING [{item['code']}]: {item['message']}{cards}")
        for item in legality.get("unverified") or []:
            cards = f" ({', '.join(item['cards'])})" if item["cards"] else ""
            add(f"  UNVERIFIED [{item['code']}]: {item['message']}{cards}")
        if legality.get("data_warning"):
            add(f"  DATA WARNING: {legality['data_warning']}")
        add("")

    add("-- What this deck is (from the list itself) --")
    if exp["themes"]:
        for slug in exp["themes"]:
            members = ", ".join(exp["theme_packages"].get(slug) or [])
            add(f"  theme {slug}: "
                f"{members or '(members not resolvable offline)'}")
    else:
        add("  no dominant theme package detected")
    role_line = ", ".join(f"{r} x{n}" for r, n in exp["roles"].items())
    add(f"  roles: {role_line or '(no cards resolvable offline)'}")
    wincon_line = ", ".join(exp["wincons"])
    add(f"  win-condition cards: {wincon_line or '(none detected)'}")
    add("")

    add("-- What the primer says --")
    if not pr["present"]:
        add("  no primer sidecar found — this is common (~75% of decks); "
            "the explanation above is grounded in the list alone.")
    else:
        add(f"  primer: {pr['words']} words")
        if pr["linked_present"]:
            add(f"  primer-linked exact-name references IN the list "
                f"(not automatically protected): "
                f"{', '.join(pr['linked_present'])}")
        if pr["linked_absent"]:
            add(f"  primer-linked cards NOT in the list: "
                f"{', '.join(pr['linked_absent'])}")
        if pr["prose_mentions"]:
            add(f"  also discussed in prose: "
                f"{', '.join(pr['prose_mentions'])}")
        if pr["win_lines"]:
            add("  how it wins, in the author's own words:")
            for quote in pr["win_lines"]:
                add(f'    "{quote}"')
    add("")

    for note in exp["notes"]:
        add(f"  NOTE: {note}")
    if exp["notes"]:
        add("")

    add(f"-- Making it yours (polish tier: at most {per['max_swaps']} "
        f"suggested swaps; nothing is applied) --")
    if per.get("protection_note"):
        add(f"  {per['protection_note']}")
    elif per.get("protected"):
        add(f"  protected (never suggested as cuts): "
            f"{', '.join(per['protected'])}")
    if per.get("preference_slugs"):
        add(f"  steering toward your preferences: "
            f"{', '.join(per['preference_slugs'])}")
    if per.get("skipped"):
        add(f"  no suggestions: {per['skipped']}")
    elif not per["suggestions"]:
        add("  no suggestions: nothing beat the cards already in the deck")
    else:
        for s in per["suggestions"]:
            add(f"  swap OUT {s['out']}  ->  IN {s['in']}")
            add(f"      serves: {s['serves']}")
            add(f"      preserves: {s['preserves']}")
    add("")
    add("(adopt never overhauls — for bigger changes, run: "
        "commander improve <deck>)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI — `commander adopt` / `commander-adopt`
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """``commander-adopt`` entry point."""
    parser = argparse.ArgumentParser(
        prog="commander-adopt",
        description=(
            "Understand an imported deck (primer cross-checked against "
            "the list) and get small, preference-steered swap "
            "suggestions. Read-only and offline; polish-tier budget; "
            "the rebuild tier is not reachable from this command."
        ),
    )
    parser.add_argument("deck", help="Path to the .dck file to adopt.")
    parser.add_argument(
        "--preferences", default=None,
        help="Your own words about what you like doing (free text).")
    parser.add_argument(
        "--preferences-file", default=None,
        help="Read --preferences text from a file instead.")
    parser.add_argument(
        "--max-swaps", type=int, default=POLISH_MAX_SWAPS,
        help=(f"Suggestion budget, clamped to the polish tier's "
              f"{POLISH_MAX_SWAPS} — asking for more still yields "
              f"{POLISH_MAX_SWAPS}."))
    parser.add_argument(
        "--deck-dir", default=None,
        help="Deck corpus directory for the lift matrix "
             "(default: the deck's own directory).")
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit the payload as JSON instead of the report.")
    args = parser.parse_args(argv)

    deck_path = Path(args.deck)
    if not deck_path.exists():
        print(f"ERROR: deck not found: {deck_path}", file=sys.stderr)
        return 2

    preferences = args.preferences
    if args.preferences_file:
        try:
            preferences = Path(args.preferences_file).read_text(
                encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: cannot read --preferences-file: {exc}",
                  file=sys.stderr)
            return 2

    payload = adopt_deck(
        deck_path,
        preferences=preferences,
        deck_dir=Path(args.deck_dir) if args.deck_dir else None,
        max_swaps=args.max_swaps,
    )
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_adoption(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

* **Exact card names come ONLY from card-link embeds.** Prose primers
  name cards with typos and nicknames, so free text is never mined for
  names. Auto-protection (below) keys exclusively on the sidecar's
  card-links block; a prose-only primer gets an explanation WITHOUT
  auto-protection, and the output says so out loud rather than silently
  protecting nothing. Prose is still USED — read-only — to confirm
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
from .collection import match_key
from .intent import free_text_theme_slugs, resolve_preferences

#: The hard swap-suggestion ceiling: the polish tier's add budget (5).
#: Read from TIER_CAPS so the two numbers cannot drift, but bound at
#: import time to a plain int — nothing at call time (flag, env, score)
#: can raise it. This constant is the whole reason rebuild is
#: structurally unreachable here; see the module docstring.
POLISH_MAX_SWAPS: int = TIER_CAPS["polish"][0]

#: Printed whenever suggestions run without primer-derived protection,
#: naming WHY (no sidecar vs. no embeds) — "we protected nothing" must
#: never look like "everything important was protected".
NO_AUTO_PROTECT_NOTE = (
    "auto-protection unavailable: {reason}. Suggestions still preserve "
    "roles and never touch the commander{lands_clause}; add Protect= "
    "lines to the .dck [metadata] to pin specific cards."
)

#: Above this share of unresolved main-deck cards the suggestion pass is
#: refused outright (R3 F-09): with the oracle cache this cold every role
#: is ``other`` and a like-for-like swap is a coin flip dressed as advice.
UNRESOLVED_REFUSE_SHARE = 0.25


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
    # ONE key on both sides (R3 F-02): the sidecar's card-links carry a
    # DFC as "Front // Back" while the .dck carries the front face.
    main_keys = {match_key(n) for n in main_cards}
    all_keys = main_keys | {match_key(n) for n in commanders}

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
    linked_present = [n for n in links if match_key(n) in all_keys]
    # A linked name the list lacks is only a DRIFT signal when the oracle
    # cache knows the card (R3 F-11); a name nothing recognizes is
    # reported as unrecognized, never printed as a card the deck "does
    # NOT run" — grounding rule: every card named resolves through the
    # cache or the list.
    linked_absent: list[str] = []
    linked_unrecognized: list[str] = []
    for n in links:
        if match_key(n) in all_keys:
            continue
        card = lookup(n) or {}
        if card.get("oracle_text") or card.get("type_line"):
            linked_absent.append(n)
        else:
            linked_unrecognized.append(n)

    prose_mentions: list[str] = []
    if primer_text:
        linked_keys = {match_key(n) for n in links}
        prose_mentions = [
            n for n in commanders + main_cards
            if match_key(n) not in linked_keys
            and _prose_mentions_name(primer_text, n)
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
    if linked_unrecognized:
        notes.append(
            f"{len(linked_unrecognized)} primer card-link(s) are neither in "
            f"the list nor in the local oracle snapshot "
            f"({', '.join(linked_unrecognized)}) — not reported as drift; "
            f"run commander oracle-refresh if they are real cards."
        )

    return {
        "commanders": commanders,
        # Cards, not lines (R3 F-18): ``8 Swamp`` is eight cards.
        "main_count": dck_utils.count_main_cards(deck_text),
        "primer": {
            "present": bool(primer_text),
            "words": primer.primer_word_count(primer_text or ""),
            "card_links": links,
            "linked_present": linked_present,
            "linked_absent": linked_absent,
            "linked_unrecognized": linked_unrecognized,
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


def _prose_mentions_name(prose: str, name: str) -> bool:
    """Whole-name, word-bounded containment of a KNOWN list name in the
    primer prose (R3 F-16: ``"Opt" in "option"`` used to count). Both
    sides go through ``match_key`` so a curly apostrophe or a DFC's back
    face in the prose still matches the list's spelling."""
    import re
    key = match_key(name)
    if not key:
        return False
    # Not ``match_key(prose)``: that would keep only the text before the
    # first "//" — fold the whole prose the same way minus that split.
    folded = " ".join(
        prose.casefold().translate(_APOSTROPHES).split()) if prose else ""
    return re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", folded) is not None


_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


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
    * ``protect``: the caller's protected names (Protect= lines +
      primer card-links present in the list) are never swapped out.

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

    def _resolved(nm: str) -> bool:
        card = _card(nm)
        return bool(card.get("oracle_text") or card.get("type_line")
                    or is_basic_land(nm))

    # Unresolved names (R3 F-09): with no oracle text the role classifier
    # calls a card ``other`` and ``_is_land`` calls it a nonland, so an
    # unresolvable Command Tower used to be proposed as a cut under a
    # note promising lands are never touched. Such names are never
    # swapped OUT (protected below) and are counted here so the note
    # can say so instead of promising what it cannot check.
    unresolved = [n for n in main_cards if not _resolved(n)]
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

    # ONE key (R3 F-02 / F-10): ``Protect=Jeska’s Will`` (curly) must pin
    # the list's ``Jeska's Will``; a DFC Protect= may carry either face
    # spelling. Unresolved names join the set — see above.
    protected_keys = {match_key(p) for p in protected}
    protected_keys |= {match_key(n) for n in unresolved}

    def protect(nm: str) -> bool:
        return match_key(nm) in protected_keys

    common = {
        "max_swaps": max_swaps, "tier": "polish",
        "preference_slugs": pref_slugs, "unresolved": unresolved,
    }
    if not commanders:
        return {"suggestions": [], "skipped": "no [Commander] section",
                **common}
    if main_cards and len(unresolved) / len(main_cards) > UNRESOLVED_REFUSE_SHARE:
        return {
            "suggestions": [],
            "skipped": (
                f"{len(unresolved)} of {len(main_cards)} main-deck cards are "
                f"not in the local oracle snapshot (> "
                f"{UNRESOLVED_REFUSE_SHARE:.0%}); roles cannot be classified, "
                f"so no swap is proposed. Run commander oracle-refresh "
                f"--from-bulk --everything (adopt is cache-only and never "
                f"primes on demand)."
            ),
            **common,
        }

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

    return {"suggestions": suggestions, "skipped": skipped, **common}


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
    deck_text = dck_utils.read_deck_text(deck_path)
    lookup = lookup or _lookup_cache_only

    # R3 F-07: a sidecar whose header names another source deck is not
    # this deck's primer — say so loudly and explain from the list alone.
    identity_warning = primer.sidecar_identity_warning(deck_path, deck_text)
    if identity_warning is None:
        primer_text = primer.read_primer_sidecar(deck_path)
        card_links = primer.read_primer_card_links(deck_path)
    else:
        primer_text, card_links = None, []

    explanation = explain_deck(
        deck_text, primer_text, card_links, lookup=lookup)
    if identity_warning:
        explanation["notes"].insert(0, identity_warning)

    # AUTO-PROTECT — card-link embeds only (exact names; the harvest
    # rule), unioned with any Protect= lines already in the .dck (the
    # existing metadata idiom, one reader for all of it). When neither
    # yields anything the output SAYS protection is unavailable rather
    # than silently protecting nothing.
    from .web._helpers import read_protected_cards
    protected = list(read_protected_cards(deck_text))
    for name in explanation["primer"]["linked_present"]:
        if match_key(name) not in {match_key(p) for p in protected}:
            protected.append(name)

    protection_note: Optional[str] = None
    if not protected:
        if identity_warning:
            reason = "the primer sidecar belongs to another deck"
        elif primer_text is None:
            reason = "this deck has no primer sidecar"
        elif not card_links:
            reason = ("the primer is prose-only — no card-link embeds, "
                      "and prose is never mined for names")
        else:
            reason = "none of the primer's linked cards are in the list"
        # The lands promise is only true when every card resolved (R3
        # F-09): an unresolved nonbasic land is invisible to the land
        # test, so past that point the note names the gap instead.
        lands_clause = (
            " or lands" if not explanation["unresolved"] else
            f" (lands are recognized from the oracle snapshot, and "
            f"{explanation['unresolved']} card(s) are missing from it — "
            f"those are never proposed as cuts either)"
        )
        protection_note = NO_AUTO_PROTECT_NOTE.format(
            reason=reason, lands_clause=lands_clause)

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
            add(f"  primer-linked cards IN the list: "
                f"{', '.join(pr['linked_present'])}")
        if pr["linked_absent"]:
            add(f"  primer-linked cards NOT in the list: "
                f"{', '.join(pr['linked_absent'])}")
        if pr.get("linked_unrecognized"):
            add(f"  primer card-links nothing here recognizes: "
                f"{', '.join(pr['linked_unrecognized'])}")
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
    if per.get("unresolved"):
        add(f"  never proposed as cuts (not in the oracle snapshot, so "
            f"unclassifiable): {', '.join(per['unresolved'])}")
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
        help="Your own words about what you like doing (free text). Read "
             "as AFFIRMATIVE theme keywords: 'I love tokens' steers "
             "toward tokens; a negated mention ('no tokens') is dropped, "
             "it does not steer away (R3 F-04).")
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

    try:
        preferences = resolve_preferences(args.preferences,
                                          args.preferences_file)
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

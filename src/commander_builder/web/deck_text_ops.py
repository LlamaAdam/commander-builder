"""Deck-text transformation helpers for the web layer.

Functions that rewrite Forge ``.dck`` blobs: paste normalization,
commander→constructed conversion, basic-land padding, and add/cut
swap splicing. Extracted verbatim from ``web/_helpers.py``
(2026-06-12 split); ``_helpers`` re-exports every name here for
backward compatibility.
"""

from __future__ import annotations

from typing import Optional

from ..dck_utils import CARD_LINE_RE, count_main_cards, parse_card_line
from ..import_formats import arena_to_dck, csv_to_lines, detect_paste_format


def _normalize_pasted_deck(text: str) -> str:
    """Accept a Forge-format .dck blob, an MTGA/Arena export, a CSV
    card list, or a Moxfield bulk-paste line list and return a valid
    .dck. This is the single dispatch point for the paste box:
    ``import_formats.detect_paste_format`` classifies the text (its
    ``"dck"`` test is byte-identical to the historical any-`[section]`
    check here, so pre-existing pastes route unchanged) and each
    branch produces the same .dck intermediate downstream writers
    (Name= stamping, role prefixes) have always consumed.

    May raise ``import_formats.ImportFormatError`` when a POSITIVELY
    detected Arena/CSV paste contains a malformed line — the import
    route turns that into a 400 naming the line. Ambiguous text never
    errors; it falls through to the plain-lines wrap below.
    """
    text = text.strip()
    if not text:
        return ""
    fmt = detect_paste_format(text)
    if fmt == "arena":
        # Arena's Commander/Sideboard sections map to [Commander]/
        # [Sideboard] — the only paste shape besides .dck that can
        # carry an explicit commander.
        return arena_to_dck(text)
    if fmt == "csv":
        # CSV degrades to the plain line list ON PURPOSE: exports have
        # no commander column, so commander handling must be exactly
        # whatever the plain-paste path does (today: nothing — all
        # cards to [Main]). Fall through to the wrap below.
        text = csv_to_lines(text).strip()
        if not text:
            return ""
    elif fmt == "dck":
        # The paste already has section headers — trust the user.
        return text + "\n"
    # Otherwise wrap in [Main]. Filter trivial header lines like
    # "Mainboard (99)" that Moxfield's UI sometimes includes.
    body_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # Skip obvious headers (e.g. "Commander (1)", "Mainboard (99)").
        if s.lower().startswith(("mainboard", "commander", "sideboard",
                                 "considering")):
            continue
        body_lines.append(s)
    return "[Main]\n" + "\n".join(body_lines) + "\n"


def _to_constructed_format(text: str) -> str:
    """Convert a Forge commander .dck to a 1v1-constructed-loadable
    .dck.

    Forge's `sim -f constructed` mode silently produces zero games
    when the deck has a ``[Commander]`` section — the format flag
    doesn't match the deck shape, so Forge loads the file but never
    actually starts a match. The fix is to:

    1. Move the commander line into ``[Main]`` so the deck is just
       a single 100-card stack of cards Forge can shuffle.
    2. Drop the ``[Commander]`` section header.
    3. Stamp ``Deck Type=constructed`` into ``[metadata]`` so Forge's
       deck-type detector picks the right rule set.

    This mirrors what forge_py's correlate_with_forge.py harness has
    been doing for the round-robin study; the propose-swap web endpoint
    needs the same conversion before handing decks to Forge.
    """
    import re as _re
    out: list[str] = []
    in_cmdr = False
    in_meta = False
    cmdr_lines: list[str] = []
    seen_metadata = False
    deck_type_set = False

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            out.append(raw)
            continue
        if s.lower() == "[commander]":
            in_cmdr = True
            in_meta = False
            continue  # drop the header
        if s.lower() == "[metadata]":
            in_meta = True
            in_cmdr = False
            seen_metadata = True
            out.append(raw)
            continue
        if s.startswith("[") and s.endswith("]"):
            in_cmdr = False
            in_meta = False
            out.append(raw)
            continue
        if in_cmdr:
            cmdr_lines.append(s)
            continue
        if in_meta and s.lower().startswith("deck type="):
            out.append("Deck Type=constructed")
            deck_type_set = True
            continue
        out.append(raw)

    new_text = "\n".join(out)
    if not seen_metadata:
        new_text = "[metadata]\nDeck Type=constructed\n\n" + new_text
    elif not deck_type_set:
        # Insert under existing metadata block.
        new_text = _re.sub(
            r"(\[metadata\][^\n]*\n)",
            r"\1Deck Type=constructed\n",
            new_text, count=1, flags=_re.IGNORECASE,
        )
    # Append commander lines to [Main]. If [Main] doesn't exist, add it.
    if cmdr_lines:
        cmdr_block = "\n".join(cmdr_lines) + "\n"
        if _re.search(r"^\[Main\]\s*$", new_text, _re.MULTILINE | _re.IGNORECASE):
            # Insert just after the [Main] header. Use a callable
            # replacement so card-line content (which starts with
            # digits like "1 Hakbal") doesn't get parsed as numeric
            # backreferences (\1 → \11 collision).
            new_text = _re.sub(
                r"(\[Main\][^\n]*\n)",
                lambda m: m.group(0) + cmdr_block,
                new_text, count=1, flags=_re.IGNORECASE,
            )
        else:
            new_text += "\n[Main]\n" + cmdr_block
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text


def _format_added_line(name: str, qty: int = 1) -> str:
    """Render a `<qty> <Name>|<SET>|<CN>` line for an added card.

    Forge's deck loader can be strict about ambiguous name-only
    lookups (alternate art, reprints across many sets, special
    characters like //). We resolve each appended card to its
    current Scryfall printing so the proposed deck loads cleanly.

    ``qty`` defaults to 1 for back-compat with single-add callers.
    ``_apply_swaps_to_dck`` passes qty>1 when collapsing duplicate
    add entries for the same card name.

    The shared ``oracle_snapshots`` cache stores forge_py-projected
    snapshots that don't carry ``set`` / ``collector_number`` fields
    (those are stripped to keep payload size small). When the cached
    snapshot lacks them we fall through to a cache-bypassed Scryfall
    fetch, which returns the full payload and re-caches it. Plain
    ``<qty> <name>`` is the final fallback when Scryfall is unreachable.
    """
    try:
        from ..scryfall_client import lookup_card
        data = lookup_card(name) or {}
        set_code = (data.get("set") or "").upper()
        cn = data.get("collector_number") or ""
        if not (set_code and cn):
            # Cached snapshot was the projected shape — fetch fresh.
            data = lookup_card(name, cache=False) or {}
            set_code = (data.get("set") or "").upper()
            cn = data.get("collector_number") or ""
    except Exception:
        return f"{qty} {name}"
    if set_code and cn:
        return f"{qty} {name}|{set_code}|{cn}"
    return f"{qty} {name}"


_BASIC_LANDS = ("Forest", "Island", "Plains", "Swamp", "Mountain", "Wastes")

# Lowercase lookup set for the add-validation path in
# ``_apply_swaps_to_dck``. Basic lands are the ONE class of card a
# Commander deck may legally run multiples of, so an add for a basic
# already in [Main] is a legitimate quantity bump while the same add
# for any other card would produce an illegal ``2 Rhystic Study`` line.
_BASIC_LAND_KEYS = frozenset(b.lower() for b in _BASIC_LANDS)


def _is_basic_land_name(name: str) -> bool:
    """True when ``name`` is a basic land (including the Snow-Covered
    variants, which share the basic supertype and the multiples
    exemption). Case-insensitive."""
    n = name.strip().lower()
    if n.startswith("snow-covered "):
        n = n[len("snow-covered "):]
    return n in _BASIC_LAND_KEYS


def _dck_name_key(name: str) -> str:
    """Matching key for a card name in a .dck context: case-folded
    front face.

    Double-faced cards drift between two spellings depending on the
    source: Scryfall / proposal JSON often carries the full
    ``Malakir Rebirth // Malakir Mire`` form while Forge .dck lines
    usually carry only the front face ``Malakir Rebirth`` (see
    forge_cards_loader.slug_for's DFC notes) — and occasionally vice
    versa. Folding BOTH sides to the lowercase front face makes cut /
    add matching insensitive to that drift. Front-face names are
    unique across Magic cards, so the fold cannot collide two
    distinct cards.

    The ``split("//")`` convention matches the existing DFC handling
    in ``edhrec_client._slugify_commander`` and
    ``_card_list_refresh`` — one shared parsing rule, not a new one.
    """
    return name.split("//", 1)[0].strip().lower()


def _count_main_cards(text: str) -> int:
    """Sum quantity prefixes across the [Main] section of a .dck blob.

    Counts what is ACTUALLY in the deck text (``27 Mountain`` counts
    27), which is the number Forge's loader sees. Used by
    ``proposer.apply_proposal_to_deck``'s last-resort ``!= 99`` write
    guard; the web layer's ``routes_audit._count_main_lines`` does
    the same walk for the UI headline. Thin alias for the canonical
    ``dck_utils.count_main_cards`` (post-split shared primitive).
    """
    return count_main_cards(text)


def _pad_main_to_99(text: str, current_main: int) -> tuple[str, int, dict[str, int]]:
    """Top up the [Main] section with basic lands until it hits 99.

    The user's source decks sometimes ship short of legal Commander
    size (e.g. the Goblin deck is 71 mainboard). The advisor's adds==
    cuts balance preserves any deficit, so the proposed deck inherits
    it and Forge refuses to load it. Pad with basics matching the
    distribution already present in the deck — preserves color balance
    without us needing to round-trip Scryfall for the commander's color
    identity.

    Returns ``(padded_text, padding_added, breakdown)`` where breakdown
    is ``{basic_name: count_added}``. If the deck is already at or above
    99 mainboard, returns the input text and an empty breakdown.

    ``padding_added`` reports what was ACTUALLY inserted, not the
    computed deficit. If the text has no [Main] header there is
    nowhere to splice the pad lines, so nothing is inserted and the
    return is ``(text, 0, {})`` — previously this path returned the
    deficit anyway, making callers (and their post-swap size math)
    believe 28 basics landed when zero did.
    """
    if current_main >= 99:
        return text, 0, {}
    deficit = 99 - current_main

    # Count basics currently in [Main] so we mirror the user's distribution.
    counts: dict[str, int] = {b: 0 for b in _BASIC_LANDS}
    in_main = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_main = stripped.lower() == "[main]"
            continue
        if not in_main:
            continue
        parsed = parse_card_line(stripped)
        if parsed is None:
            continue
        qty, name = parsed
        if name in counts:
            counts[name] += qty

    basics_present = {b: c for b, c in counts.items() if c > 0}
    # No basics? Fall back to Wastes (colorless, legal in any color identity).
    # Better than guessing colors blind.
    if not basics_present:
        basics_present = {"Wastes": 1}

    total = sum(basics_present.values())
    pad: dict[str, int] = {}
    distributed = 0
    # Largest share first (sorted descending) so floor-rounding leftovers
    # gravitate to the dominant color.
    for b, c in sorted(basics_present.items(), key=lambda kv: -kv[1]):
        share = (c * deficit) // total
        if share > 0:
            pad[b] = share
            distributed += share
    leftover = deficit - distributed
    if leftover > 0:
        top = max(basics_present, key=lambda b: basics_present[b])
        pad[top] = pad.get(top, 0) + leftover

    # Render new lines and splice them at the end of [Main].
    pad_lines = [f"{n} {b}" for b, n in pad.items() if n > 0]
    out_lines: list[str] = []
    in_main = False
    inserted = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_main and not inserted:
                out_lines.extend(pad_lines)
                inserted = True
            in_main = stripped.lower() == "[main]"
        out_lines.append(raw)
    if in_main and not inserted:
        out_lines.extend(pad_lines)
        inserted = True

    # No [Main] header anywhere → the loop never found an insertion
    # point and out_lines is just the input verbatim. Report the truth
    # (0 padded) instead of the intended deficit so callers don't
    # count phantom basics into their deck-size math.
    if not inserted:
        return text, 0, {}

    new_text = "\n".join(out_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, deficit, pad


def _apply_swaps_to_dck(
    original_text: str, recommendations, *, drop_report: Optional[dict] = None,
) -> tuple[str, list[str], list[str], int]:
    """Apply add / cut recommendations to a .dck blob.

    Returns ``(new_text, added_card_names, removed_card_names,
    kept_count)``. Quantity-1 lines are added by default.

    Handling:
    - The [Commander] section is preserved as-is (audits never cut commanders).
    - The [Main] section is rebuilt: each cut DECREMENTS the matching
      card's total quantity by 1 (not the line-remove-by-name behavior
      this had pre-2026-05-15). For ``cuts=["Mountain", "Mountain"]``
      on a deck containing ``27 Mountain|EXP|123``, the line becomes
      ``25 Mountain|EXP|123`` — not gone entirely. A cut that drives
      the quantity to zero drops the whole line.
    - Adds are quantity-aware (matches the cut semantics): an add for
      a BASIC LAND already in [Main] increments that line's quantity
      (and preserves its |SET|CN edition tail); duplicate basic-land
      add entries collapse to one line with the summed quantity.
      Multi-printing lines: an add bumps the FIRST matching line in
      deck order (consistent with the cut-decrement contract). Cards
      with no existing line get appended at the end of [Main] via
      ``_format_added_line`` (Scryfall-resolved |SET|CN).
    - Other sections (sideboard, considering, metadata) are preserved.

    **Adds and cuts are balanced** -- Commander needs exactly 99 main +
    1 commander. The advisor's heuristic produces M adds and N cuts
    independently and they're often unequal, leaving an illegal deck
    that Forge refuses to load. We trim whichever list is longer so
    both have ``min(M, N)`` entries (priority: keep adds, since they
    came from EDHREC's top-cards rank order; drop the *bottom* of the
    cuts list, since those are the lowest-confidence "doesn't appear
    in EDHREC top/synergy" guesses).

    **Swap pairs are validated against the ACTUAL decklist** (2026-07-19
    fix). Balancing by REQUESTED count used to leave a hole: a cut that
    matched no [Main] line (LLM hallucination, DFC ``A // B`` vs ``A``
    naming drift, a second cut of a quantity-1 card) was silently
    skipped while its paired add still landed — writing a 100-card
    mainboard that Forge rejects or, worse, silently mis-sims.
    After balancing, each positional (cut[i], add[i]) pair must pass:

      - the cut matches a [Main] card with quantity still available
        (earlier cuts in the same proposal consume quantity), else the
        PAIR is dropped under ``dropped_unmatched_cut``;
      - the add is not the [Commander] card, else the pair is dropped
        under ``dropped_commander_add``;
      - the add is not a non-basic already in [Main] (or already
        accepted earlier in this proposal) — an increment there would
        violate the singleton rule — else the pair is dropped under
        ``dropped_duplicate_add``. Basic lands are exempt (legal in
        multiples).

    Dropping the WHOLE pair keeps adds == cuts, so the mainboard size
    is preserved no matter what the LLM proposed: an illegal proposal
    degrades to a smaller applied swap set, never to an illegal deck.

    Name matching folds case AND double-faced-card naming: proposal
    names like ``Malakir Rebirth // Malakir Mire`` match deck lines
    reading ``Malakir Rebirth`` and vice versa (see ``_dck_name_key``).

    ``drop_report``, when passed a dict, is populated in place with
    ``dropped_for_balance`` (list of surplus card names) and
    ``dropped_unmatched_cut`` / ``dropped_duplicate_add`` /
    ``dropped_commander_add`` (lists of ``{"cut": ..., "add": ...}``
    pair dicts) so callers can show WHY a requested swap didn't land.
    Kept as an optional out-param rather than a fifth tuple element so
    the many existing 4-tuple call sites stay valid.

    Card names are matched against the leading
    ``<qty> <Name>[|<SET>|<CN>]`` pattern. The edition tail (|SET|CN)
    is preserved verbatim when a line is rewritten with a smaller
    quantity.

    ``removed_card_names`` returned reflects what ACTUALLY came out
    of the deck, one entry per instance, in the casing the caller
    requested. With cuts=["Mountain", "Mountain"] against a
    27-Mountain stack, ``removed`` is ``["Mountain", "Mountain"]``.
    """
    from collections import Counter as _Counter

    all_adds = [r.card for r in recommendations if r.action == "add"]
    all_cuts = [r.card for r in recommendations if r.action == "cut"]
    # Balance to keep Commander deck size legal. Surplus from the
    # longer list is reported under dropped_for_balance.
    n = min(len(all_adds), len(all_cuts))
    add_names = all_adds[:n]
    cuts = all_cuts[:n]
    dropped_for_balance = all_adds[n:] + all_cuts[n:]

    # ---- Pass 1: inventory the deck so swaps can be validated -------
    # main_qty counts total copies per name-key across all printings;
    # commander_keys holds the [Commander] card(s). Both keyed via
    # _dck_name_key so DFC naming drift can't dodge the checks.
    main_qty: _Counter = _Counter()
    commander_keys: set[str] = set()
    section = ""
    for raw in original_text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s.lower()
            continue
        if section not in ("[main]", "[commander]"):
            continue
        m = CARD_LINE_RE.match(s)
        if not m:
            continue
        try:
            qty = int(m.group(1))
        except (TypeError, ValueError):
            qty = 1
        key = _dck_name_key(m.group(2).strip())
        if section == "[main]":
            main_qty[key] += qty
        else:
            commander_keys.add(key)

    # ---- Pass 2: validate each (cut, add) pair ----------------------
    # Pairing is positional: after the min() slice both lists are the
    # same length and cut[i] funds the slot add[i] fills. When either
    # half of a pair is invalid the WHOLE pair is dropped — applying
    # just the surviving half would change the mainboard size, which
    # is exactly the corruption this validation exists to prevent.
    remaining_qty: _Counter = _Counter(main_qty)
    valid_adds: list[str] = []
    valid_cuts: list[str] = []
    dropped_unmatched_cut: list[dict] = []
    dropped_duplicate_add: list[dict] = []
    dropped_commander_add: list[dict] = []
    # Non-basic adds accepted earlier in this same proposal — a second
    # add of the same non-basic would collapse to a qty-2 line, so it
    # gets the same duplicate rejection as a card already on disk.
    accepted_nonbasic_add_keys: set[str] = set()
    for cut, add in zip(cuts, add_names):
        cut_key = _dck_name_key(cut)
        if remaining_qty.get(cut_key, 0) <= 0:
            # Cut names nothing left in [Main]: hallucinated card,
            # naming drift _dck_name_key couldn't bridge, or an
            # earlier cut already consumed the last copy.
            dropped_unmatched_cut.append({"cut": cut, "add": add})
            continue
        add_key = _dck_name_key(add)
        if add_key in commander_keys:
            # The commander already occupies the command zone; adding
            # it to [Main] duplicates it across zones.
            dropped_commander_add.append({"cut": cut, "add": add})
            continue
        if not _is_basic_land_name(add) and (
            main_qty.get(add_key, 0) > 0
            or add_key in accepted_nonbasic_add_keys
        ):
            # Non-basic already in the deck — incrementing would write
            # e.g. ``2 Rhystic Study``, a singleton violation. NOTE:
            # checked against the ORIGINAL main contents even when a
            # cut in this proposal would remove the copy first; a
            # cut-then-readd of the same card is pointless churn and
            # dropping it is the conservative call.
            dropped_duplicate_add.append({"cut": cut, "add": add})
            continue
        remaining_qty[cut_key] -= 1
        if not _is_basic_land_name(add):
            accepted_nonbasic_add_keys.add(add_key)
        valid_cuts.append(cut)
        valid_adds.append(add)

    if drop_report is not None:
        drop_report["dropped_for_balance"] = dropped_for_balance
        drop_report["dropped_unmatched_cut"] = dropped_unmatched_cut
        drop_report["dropped_duplicate_add"] = dropped_duplicate_add
        drop_report["dropped_commander_add"] = dropped_commander_add

    add_names = valid_adds
    cuts = valid_cuts

    # ---- Pass 3: splice the validated swaps into the text -----------
    # Quantity-aware cut budget -- count occurrences so duplicate cuts
    # ("Mountain" listed twice) decrement the named card by 2 rather
    # than collapsing to a single line-remove. Keyed by _dck_name_key
    # so a full-DFC cut name still decrements a front-face-named line.
    cuts_remaining: _Counter = _Counter(_dck_name_key(c) for c in cuts)
    # Map name-key -> canonical casing from the cut request. Used so
    # ``removed`` returns the casing the caller passed in (matches
    # what audit-panel rows show) regardless of how the .dck file
    # capitalized the name on disk. First-occurrence wins on dedup.
    cut_canonical: dict[str, str] = {}
    for c in cuts:
        cut_canonical.setdefault(_dck_name_key(c), c)
    # Quantity-aware add budget — duplicate (basic-land) add entries
    # collapse, and adds for basics already in [Main] increment the
    # existing line rather than appending a stale ``1 <Name>``
    # duplicate. Validation above guarantees non-basics in this
    # counter have no existing line to merge into.
    adds_remaining: _Counter = _Counter(_dck_name_key(a) for a in add_names)
    add_canonical: dict[str, str] = {}
    for a in add_names:
        add_canonical.setdefault(_dck_name_key(a), a)

    out_lines: list[str] = []
    in_main = False
    main_kept = 0
    removed: list[str] = []

    def _flush_remaining_adds(target: list[str]) -> None:
        """Append one merged line per still-pending add. Order follows
        the original ``add_names`` insertion order (Counter preserves
        insertion order on Python 3.7+). Doesn't touch ``main_kept`` —
        the caller estimates post-swap size as ``kept + len(added)``,
        so flushed adds are counted via ``len(added)`` at the call
        site, not here."""
        for nlower, count in adds_remaining.items():
            if count > 0:
                target.append(_format_added_line(add_canonical[nlower], count))
        adds_remaining.clear()

    for raw in original_text.splitlines():
        stripped = raw.strip()
        if not stripped:
            out_lines.append(raw)
            continue
        # Section header tracking.
        if stripped.startswith("[") and stripped.endswith("]"):
            # If we're leaving [Main], flush any pending adds that
            # didn't merge into an existing line.
            if in_main:
                _flush_remaining_adds(out_lines)
            in_main = stripped.lower() == "[main]"
            out_lines.append(raw)
            continue

        if not in_main:
            # Non-main section content passes through unchanged.
            out_lines.append(raw)
            continue

        m = CARD_LINE_RE.match(stripped)
        if not m:
            # Lines that don't match the qty/name pattern (rare) pass
            # through unchanged. main_kept is unaffected.
            out_lines.append(raw)
            continue

        try:
            qty = int(m.group(1))
        except (TypeError, ValueError):
            qty = 1
        raw_name = m.group(2).strip()
        edition_tail = m.group(3) or ""
        # DFC-tolerant key (front face, case-folded) so a cut named
        # ``A // B`` decrements a line reading ``A`` and vice versa.
        name_key = _dck_name_key(raw_name)

        # Apply cuts first.
        requested = cuts_remaining.get(name_key, 0)
        to_remove = min(requested, qty)
        if to_remove > 0:
            cuts_remaining[name_key] = requested - to_remove
            if cuts_remaining[name_key] == 0:
                del cuts_remaining[name_key]
            canonical = cut_canonical.get(name_key, raw_name)
            for _ in range(to_remove):
                removed.append(canonical)

        post_cut_qty = qty - to_remove
        # ``main_kept`` only counts surviving original cards. Add
        # merges that bump this line are tallied via ``len(added)``
        # at the call site so the caller's
        # ``post_swap_main = kept + len(added)`` math stays correct.
        if post_cut_qty > 0:
            main_kept += post_cut_qty

        # Apply add merge: if there's a queued add for this name, fold
        # it into the (possibly cut-decremented) line. Preserves the
        # edition tail and avoids duplicate name-only lines. Post-
        # validation, only basic lands can reach this merge with an
        # existing line — non-basic duplicates were already dropped.
        merged_add = adds_remaining.get(name_key, 0)
        new_qty = post_cut_qty + merged_add
        if merged_add > 0:
            adds_remaining[name_key] = 0

        if new_qty <= 0:
            # Line fully cut and no merging add -- drop it.
            continue
        if new_qty == qty and to_remove == 0:
            # Untouched line -- preserve raw verbatim.
            out_lines.append(raw)
        else:
            # Rewrite preserving casing + edition.
            out_lines.append(f"{new_qty} {raw_name}{edition_tail}")

    # If [Main] was the last section, the section-header flush above
    # didn't fire — emit any still-pending adds at end-of-file.
    if in_main:
        _flush_remaining_adds(out_lines)

    new_text = "\n".join(out_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, list(add_names), removed, main_kept


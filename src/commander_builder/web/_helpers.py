"""Pure helper functions shared across the web layer's route handlers.

Extracted from ``web/app.py`` as part of the blueprint refactor
(tier-3 issue #3.1). Every function here is independent of Flask
state — no ``current_app``, no ``request``, no closure over
``create_app``'s arguments — so the route blueprints can import
them freely.

Functions:

- ``_bracket_from_filename``    parse [B<n>] suffix from a deck name
- ``_normalize_pasted_deck``    Moxfield bulk-paste → .dck shape
- ``_match_pct_from_evidence``  evidence dict → 0..100 or None
- ``_to_constructed_format``    Commander .dck → 1v1-constructed .dck
- ``_format_added_line``        ``1 <Name>|<SET>|<CN>`` for an add
- ``_pad_main_to_99``           top up [Main] with basics
- ``_apply_swaps_to_dck``       splice add/cut recs into a .dck
- ``_build_suggested_adds``     project ``advise()`` recs → dashboard shape
- ``_iteration_to_dict``        Iteration row → JSON-friendly dict

External callers should NOT import from this module — it's an
internal layout detail. The web layer's public surface stays
``commander_builder.web.app:create_app``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def _resolve_deck_path(
    deck_dir: Path, deck_id: Optional[str], explicit_path: Optional[str],
) -> Optional[Path]:
    """Resolve a deck identifier or explicit path to a real file.

    Both forms are validated against ``deck_dir`` — explicit paths must
    be inside ``deck_dir`` after resolution AND carry a ``.dck`` suffix,
    otherwise None is returned. The ``.dck`` lock matters because this
    resolver also backs the DELETE/PUT deck routes: without it a crafted
    ``?path=`` could target a non-deck file inside the dir (pool JSON,
    soak summary, a staged file) and the write/delete would clobber it.

    Moved here from ``web/app.py`` in the 2026-05-14 Tier-1 #0a
    cleanup so the 5 blueprint factories can import it directly
    instead of receiving it as a constructor parameter.
    """
    if deck_id:
        candidate = (deck_dir / f"{deck_id}.dck").resolve()
        try:
            candidate.relative_to(deck_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.exists() else None
    if explicit_path:
        candidate = Path(explicit_path).resolve()
        # Only ever resolve to actual deck files — never arbitrary files
        # that merely happen to live inside deck_dir.
        if candidate.suffix.lower() != ".dck":
            return None
        try:
            candidate.relative_to(deck_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.exists() else None
    return None


def _bracket_from_filename(deck_id: str | None) -> int | None:
    """Parse the ``[B<n>]`` suffix the user encodes in deck filenames.

    Returns the bracket integer (1..5) or None if the suffix is missing
    or unparseable. The filename is the user's declared/intended
    bracket; it should override the heuristic guess unless the request
    explicitly passes a different bracket.
    """
    if not deck_id:
        return None
    import re as _re
    m = _re.search(r"\[B(\d)\](?:\.dck)?\s*$", deck_id)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 1 <= n <= 5 else None


def _normalize_pasted_deck(text: str) -> str:
    """Accept either a Forge-format .dck blob or a Moxfield bulk-paste
    line list and return a valid .dck. Bulk-paste shape is one line
    per card: ``<qty> <Name>`` with no `[Main]` header. We detect that
    by looking for any `[section]` markers; if none, wrap the lines
    in a `[Main]` section.
    """
    text = text.strip()
    if not text:
        return ""
    # If the paste already has section headers, trust the user.
    has_section = any(
        line.strip().startswith("[") and line.strip().endswith("]")
        for line in text.splitlines()
    )
    if has_section:
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


def _match_pct_from_evidence(evidence: dict | None) -> int | None:
    """Mirror deck_dashboard.match_score combination so audit output
    uses the same 0..100 scale the suggestion panel renders.

    Bracket-peers recs (source="bracket_peers") only set
    ``in_n_references`` / ``total_references`` rather than the EDHREC
    inclusion%/synergy% pair. When those are present we compute
    ``100 * in_n_references / total_references`` — a card in 5/5
    references shows 100, 3/5 shows 60. This keeps the UI's match-pct
    pill meaningful across all sources without bracket_peers needing
    to fabricate EDHREC-shaped fields.

    Returns ``None`` (JSON ``null``) when evidence carries no usable
    scoring signal — manabase essentials, vanilla Claude recs with
    no peer-ref data, etc. The UI branches on null and shows a
    source-specific badge (Manabase / Claude analyst) instead of a
    misleading "0%" pill. Before 2026-05-13 we returned 0 here and
    the UI rendered it as "0%", which looked identical to "this
    card is a bad match" rather than "this card has no inclusion%
    to score against."
    """
    if not evidence:
        return None
    # Prefer reference-frequency math when bracket_peers fields are set.
    total = evidence.get("total_references")
    in_n = evidence.get("in_n_references")
    if isinstance(total, int) and total > 0 and isinstance(in_n, int):
        return max(0, min(100, round(100 * in_n / total)))
    inclusion = evidence.get("inclusion_pct")
    synergy = evidence.get("synergy_pct")
    # If neither inclusion nor synergy was provided, the rec carries
    # no scoring signal — return None so the UI renders a
    # source-tag badge instead of a confusing "0%" pill.
    if inclusion is None and synergy is None:
        return None
    inclusion = float(inclusion or 0)
    synergy = min(float(synergy or 0), 20.0)
    raw = inclusion + synergy
    if raw <= 0:
        return None
    return max(1, min(100, round(raw)))


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
    """
    import re as _re
    if current_main >= 99:
        return text, 0, {}
    deficit = 99 - current_main

    # Count basics currently in [Main] so we mirror the user's distribution.
    counts: dict[str, int] = {b: 0 for b in _BASIC_LANDS}
    in_main = False
    qty_name_re = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_main = stripped.lower() == "[main]"
            continue
        if not in_main:
            continue
        m = qty_name_re.match(stripped)
        if not m:
            continue
        name = m.group(2).strip()
        if name in counts:
            try:
                counts[name] += int(m.group(1))
            except (TypeError, ValueError):
                counts[name] += 1

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

    new_text = "\n".join(out_lines)
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, deficit, pad


def _apply_swaps_to_dck(
    original_text: str, recommendations,
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
      a card already in [Main] increments that line's quantity (and
      preserves its |SET|CN edition tail). Duplicate add entries for
      the same name collapse to one line with the summed quantity.
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

    Card names are matched case-insensitively against the leading
    ``<qty> <Name>[|<SET>|<CN>]`` pattern. The edition tail (|SET|CN)
    is preserved verbatim when a line is rewritten with a smaller
    quantity.

    ``removed_card_names`` returned reflects what ACTUALLY came out
    of the deck, one entry per instance. With cuts=["Mountain",
    "Mountain"] against a 27-Mountain stack, ``removed`` is
    ``["Mountain", "Mountain"]``. With cuts=["Mountain", "Mountain"]
    against a 1-Mountain deck, ``removed`` is ``["Mountain"]`` (only
    one instance was actually available to remove).
    """
    import re as _re
    from collections import Counter as _Counter

    add_names = [r.card for r in recommendations if r.action == "add"]
    cuts = [r.card for r in recommendations if r.action == "cut"]
    # Balance to keep Commander deck size legal.
    n = min(len(add_names), len(cuts))
    add_names = add_names[:n]
    cuts = cuts[:n]
    # Quantity-aware cut budget -- count occurrences so duplicate cuts
    # ("Mountain" listed twice) decrement the named card by 2 rather
    # than collapsing to a single line-remove.
    cuts_remaining: _Counter = _Counter(c.lower() for c in cuts)
    # Map case-folded card name -> canonical casing from the cut
    # request. Used so ``removed`` returns the casing the caller
    # passed in (matches what audit-panel rows show) regardless of
    # how the .dck file capitalized the name on disk. First-occurrence
    # wins on dedup.
    cut_canonical: dict[str, str] = {}
    for c in cuts:
        cut_canonical.setdefault(c.lower(), c)
    # Quantity-aware add budget — duplicate add entries collapse, and
    # adds for cards already in [Main] increment the existing line
    # rather than appending a stale ``1 <Name>`` duplicate.
    adds_remaining: _Counter = _Counter(a.lower() for a in add_names)
    add_canonical: dict[str, str] = {}
    for a in add_names:
        add_canonical.setdefault(a.lower(), a)

    out_lines: list[str] = []
    in_main = False
    main_kept = 0
    removed: list[str] = []
    line_pattern = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")

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

        m = line_pattern.match(stripped)
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
        name_lower = raw_name.lower()

        # Apply cuts first.
        requested = cuts_remaining.get(name_lower, 0)
        to_remove = min(requested, qty)
        if to_remove > 0:
            cuts_remaining[name_lower] = requested - to_remove
            if cuts_remaining[name_lower] == 0:
                del cuts_remaining[name_lower]
            canonical = cut_canonical.get(name_lower, raw_name)
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
        # edition tail and avoids duplicate name-only lines.
        merged_add = adds_remaining.get(name_lower, 0)
        new_qty = post_cut_qty + merged_add
        if merged_add > 0:
            adds_remaining[name_lower] = 0

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


def _total_price_for_deck_text(text: str) -> tuple[Optional[float], int]:
    """Sum Scryfall USD prices across all cards (commander + main)
    in a ``.dck`` blob. Returns ``(total_or_none, n_priced_cards)``.

    ``total_or_none`` is None when zero cards in the deck have a
    Scryfall price (e.g. all-digital-only deck, Scryfall down). The
    UI distinguishes between "$0.00 priced" and "unpriced" via this
    None signal so a budget-mode user doesn't get confused by a
    zero total that's actually "no data."

    Quantities count: ``29 Mountain`` contributes 29× the Mountain
    price (which is ~$0.00 anyway, but consistent with the
    dashboard's tile math).

    Used by the audit endpoint to compute the post-swap deck price
    so the UI can show "$X → $Y (Δ +$12.30)" alongside the diff
    list. Tier-2 backlog item from STATUS.md.
    """
    import re as _re
    from ..scryfall_client import lookup_card
    line_re = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")
    total = 0.0
    n_priced = 0
    in_card_section = False
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            # Count cards in [Main] and [Commander]; ignore
            # [Sideboard], [Considering], [metadata], etc.
            sl = s.lower()
            in_card_section = sl in ("[main]", "[commander]")
            continue
        if not in_card_section:
            continue
        m = line_re.match(s)
        if not m:
            continue
        try:
            qty = int(m.group(1))
        except (TypeError, ValueError):
            qty = 1
        name = m.group(2).strip()
        try:
            card = lookup_card(name)
        except Exception:
            card = None
        if not card:
            continue
        prices = card.get("prices") if isinstance(card, dict) else None
        if not isinstance(prices, dict):
            continue
        raw_price = prices.get("usd")
        if not raw_price:
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        total += price * qty
        n_priced += qty
    if n_priced == 0:
        return (None, 0)
    return (round(total, 2), n_priced)


def _build_suggested_adds(deck_path: Path, bracket: int) -> list[dict]:
    """Project ``improvement_advisor.advise()`` recommendations into the
    shape ``deck_dashboard.build_dashboard`` expects for
    ``suggested``::

        [{"card": str, "inclusion_pct": float, "synergy_pct": float,
          "rationale": str, "price_usd": Optional[float]}, ...]

    Only `add` actions are forwarded — the dashboard's "suggested
    adds" panel is for cards to consider including, not cuts.
    Pulled out as a helper so both `/api/dashboard?advise=1` and
    `/api/advise` reuse the same projection.
    """
    from ..improvement_advisor import advise
    report = advise(deck_path, bracket=bracket)
    out: list[dict] = []
    for rec in report.recommendations:
        if rec.action != "add":
            continue
        ev = rec.evidence or {}
        out.append({
            "card": rec.card,
            "inclusion_pct": float(ev.get("inclusion_pct") or 0),
            "synergy_pct": float(ev.get("synergy_pct") or 0),
            "rationale": rec.reason or "",
            "price_usd": ev.get("price_usd"),
        })
    return out


def _iteration_to_dict(it) -> dict:
    """JSON-friendly projection of an Iteration. Drops the deck_snapshot
    blob to keep payloads small — callers can re-request the full row
    via /api/iteration/<id> if we add it later."""
    return {
        "id": it.id,
        "deck_id": it.deck_id,
        "deck_name": it.deck_name,
        "bracket": it.bracket,
        "parent_id": it.parent_id,
        "audit_version": it.audit_version,
        "audit_manifest": it.audit_manifest,
        "verdict": it.verdict,
        "verdict_notes": it.verdict_notes,
        "win_rate_old": it.win_rate_old,
        "win_rate_new": it.win_rate_new,
        "margin": it.margin,
        "created_at": it.created_at,
        # Milestone (schema v2 / #012) — None when unset. Powers the
        # iteration-graph flag glyph + "reference baseline" filter
        # on /api/iterations.
        "milestone": getattr(it, "milestone", None),
    }


# ---------------------------------------------------------------------------
# EDHREC average-deck preview projection
# ---------------------------------------------------------------------------
#
# AdviceReport carries an Optional[AverageDeck] (commit 4ee8a0e) and a
# lowercase-name → category map sourced from the commander page. This
# helper turns those into a JSON-friendly dict the audit-panel UI
# renders inside a collapsible <details> section. The UI doesn't open
# the section by default — most users only need it when they want to
# compare their list to the bracket archetype.


def _user_deck_card_names(deck_text: str) -> set[str]:
    """Extract the lowercase set of card names from a Forge .dck blob.

    Handles both bare ``1 Sol Ring`` and ``1 Sol Ring|CLB|871`` lines
    by stripping the optional edition tail. Section headers, metadata,
    and blank lines are ignored — only quantity-prefixed cards count.
    """
    import re as _re
    line_pattern = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")
    out: set[str] = set()
    for raw in deck_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("["):
            continue
        m = line_pattern.match(stripped)
        if m:
            out.add(m.group(2).strip().lower())
    return out


def project_average_deck_preview(
    average_deck,        # Optional[AverageDeck] — forward-typed to avoid cycle
    edhrec_categories,   # dict[str, str] — lowercase name → category
    user_deck_text: str,
):
    """Project an AverageDeck → JSON-friendly preview dict.

    Returns ``None`` when there's nothing to show:
      - ``average_deck`` is None (EDHREC unreachable, no published
        average deck for this commander+bracket)
      - ``average_deck.cards`` is empty (page parsed but produced
        zero entries)

    Otherwise returns::

        {
          "bracket_slug": str | None,
          "card_count": int,
          "cards": [
            {"name": str, "inclusion_pct": float,
             "category": str | None, "in_user_deck": bool},
            ...
          ]
        }

    Card ordering preserves the input list — EDHREC's average-deck page
    ranks by typical-build prominence and that ordering is meaningful.

    in_user_deck folds case both ways: the average-deck card name and
    the .dck-extracted name set are both lowercased before comparison.

    category match is also case-insensitive against ``edhrec_categories``;
    cards missing from the map surface ``category=None`` so the UI can
    group them under an 'Other' bucket without the helper inventing
    a label.
    """
    if average_deck is None:
        return None
    cards = list(getattr(average_deck, "cards", []) or [])
    if not cards:
        return None

    user_names = _user_deck_card_names(user_deck_text)
    # Categories map is keyed lowercase already (the advisor builds it
    # that way); fold the average-deck card name for the lookup.
    projected = []
    for entry in cards:
        key = (entry.name or "").lower()
        projected.append({
            "name": entry.name,
            "inclusion_pct": float(getattr(entry, "inclusion_pct", 0.0) or 0.0),
            "category": edhrec_categories.get(key),
            "in_user_deck": key in user_names,
        })

    return {
        "bracket_slug": getattr(average_deck, "bracket_slug", None),
        "card_count": len(projected),
        "cards": projected,
    }


# ---------------------------------------------------------------------------
# Protected-cards list — pet cards the curator must not propose for cut
# ---------------------------------------------------------------------------
#
# Per-deck protection lives in the .dck file's ``[metadata]`` section as
# ``Protect=`` entries. Forge ignores unknown metadata keys, so the
# list travels with the deck file across imports, Moxfield round-trips,
# and version bumps without polluting anything Forge cares about.
#
# Format (either / both work, unioned):
#
#     [metadata]
#     Name=Goblin
#     Moxfield=abc123
#     Protect=Krenko, Mob Boss
#     Protect=Goblin Lackey, Skirk Prospector
#
# Comma-separated on a single line OR multiple Protect= lines; both
# accepted. Card names preserve casing on read so the UI can render
# them faithfully — comparison against cut suggestions folds case at
# the call site.


def read_protected_cards(deck_text: str) -> list[str]:
    """Parse ``[metadata] Protect=`` entries out of a .dck blob.

    Convention: **one card per Protect= line**, comma is literal.
    This way the most common case — protecting your commander —
    works naturally without quoting:

        Protect=Krenko, Mob Boss
        Protect=Jaya Ballard, Task Mage
        Protect=Sol Ring

    All three are single cards. To protect multiple cards, use
    multiple Protect= lines (not comma-separated).

    Compact comma-separated form is supported only when entries are
    wrapped in double quotes, so users who want a one-line list of
    no-comma names can still do:

        Protect=\"Sol Ring\", \"Lightning Bolt\", \"Counterspell\"

    Quoted form mixes safely with bare lines on the same .dck.

    Returns a list preserving input order with duplicates collapsed
    case-insensitively. Empty list when no Protect= entries exist —
    the caller treats this as "no protection list configured."

    Whitespace trimmed per entry; empty entries silently dropped.
    """
    import re as _re
    # Quoted-entry pattern: matches `\"...\"` sequences. We pull each
    # quoted chunk out separately and treat unquoted leftover as a
    # single bare entry (preserves the comma-in-name case).
    _quoted_re = _re.compile(r'"([^"]*)"')

    protected: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        n = name.strip()
        if not n:
            return
        key = n.lower()
        if key in seen:
            return
        seen.add(key)
        protected.append(n)

    in_metadata = False
    for raw in deck_text.splitlines():
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            in_metadata = s.lower() == "[metadata]"
            continue
        if not in_metadata or "=" not in s:
            continue
        key, _, value = s.partition("=")
        if key.strip().lower() != "protect":
            continue
        value = value.strip()
        if not value:
            continue
        if '"' in value:
            # Quoted form: pull each \"...\" chunk out; ignore any
            # commas / whitespace between chunks.
            for m in _quoted_re.finditer(value):
                _add(m.group(1))
        else:
            # Bare form: the entire value is ONE card name. Commas
            # inside the name (e.g. "Krenko, Mob Boss") stay literal
            # rather than splitting the name in half — which is the
            # whole point of this rule, since commanders are almost
            # always comma-named.
            _add(value)
    return protected


# ---------------------------------------------------------------------------
# Salt-list warning aggregator
# ---------------------------------------------------------------------------
#
# Per-recommendation salt annotations already land on each add/cut
# entry (see routes_audit.py's salt_map lookups). The banner that the
# audit panel renders ABOVE the recommendations needs an aggregate
# view of the user's CURRENT deck — every salty card, sorted by score,
# regardless of whether the advisor flagged it for cut. That's what
# this helper produces.


# Default threshold for "salty" — EDHREC's salt scores run 0..5.
# Anything ≥ 1.5 is "noticeable salt" in their UI's color scale; we
# use the same cut-off so the banner reflects what a casual reader
# of EDHREC would already consider problematic.
_SALT_WARN_THRESHOLD = 1.5

# Brackets at which the warning shows. WotC's bracket guidance:
# B1 (Exhibition) + B2 (Core) are unconditionally casual; B3 (Upgraded)
# is "focused but still social". Salt is unwelcome at all three. B4+
# tables expect cEDH-grade picks and the banner just becomes noise.
_SALT_WARN_BRACKET_MAX = 3


def project_salt_warning(
    user_deck_text: str,
    salt_map: dict,
    bracket: int,
    *,
    threshold: float = _SALT_WARN_THRESHOLD,
    bracket_max: int = _SALT_WARN_BRACKET_MAX,
):
    """Aggregate salty cards in the user's deck into a banner payload.

    Returns ``None`` when there's no warning to show:
      - bracket > bracket_max (B4/B5 expect salty picks; banner = noise)
      - no salt_map (EDHREC unreachable)
      - no cards in the deck meet the threshold

    Otherwise returns::

        {
          "bracket": int,
          "count": int,
          "threshold": float,
          "cards": [
            {"name": str, "salt": float},
            ...    # sorted by salt desc, then name asc
          ]
        }

    The UI uses ``count`` for the headline ("3 salty cards at B2 —
    consider cutting"), iterates ``cards`` for the inline list, and
    ``threshold`` shows what cut-off we used (so the banner stays
    truthful if we ever tune the threshold).
    """
    if bracket > bracket_max:
        return None
    if not salt_map:
        return None

    # Preserve the canonical casing from the user's .dck for display
    # — the salt-list is keyed lowercase but the banner reads better
    # as "Smothering Tithe" than "smothering tithe".
    import re as _re
    line_pattern = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")
    canonical_by_lower: dict[str, str] = {}
    for raw in user_deck_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("["):
            continue
        m = line_pattern.match(stripped)
        if m:
            name = m.group(2).strip()
            canonical_by_lower.setdefault(name.lower(), name)

    hits: list[dict] = []
    for name_lower, canonical in canonical_by_lower.items():
        score = salt_map.get(name_lower)
        if score is None:
            continue
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            continue
        if score_f >= threshold:
            hits.append({"name": canonical, "salt": round(score_f, 2)})

    if not hits:
        return None

    hits.sort(key=lambda h: (-h["salt"], h["name"]))
    return {
        "bracket": bracket,
        "count": len(hits),
        "threshold": threshold,
        "cards": hits,
    }


# ---------------------------------------------------------------------------
# Cross-deck library search — which decks run a given card
# ---------------------------------------------------------------------------
#
# Backs the unified app's "which of my decks run this card?" lookup
# (FP-007 next slice). Pure file read over the .dck set in a directory.


def decks_containing_card(deck_dir: Path, card_name: str) -> list[str]:
    """Return the SORTED deck IDs whose [Commander] or [Main] section
    runs ``card_name``.

    Each ``.dck`` file in ``deck_dir`` is scanned. A deck matches when
    its ``[Commander]`` or ``[Main]`` section contains a line for
    ``card_name``, matched case-insensitively and ignoring the leading
    quantity and any ``|SET|CN`` edition tail (so ``1 Sol Ring|CLB|871``
    matches ``"sol ring"``). The deck ID returned is the filename stem
    (e.g. ``"Alpha [B3]"`` for ``Alpha [B3].dck``). Empty list when no
    deck runs the card.

    Only ``[Commander]`` and ``[Main]`` count — sideboard / considering
    / metadata sections are ignored, mirroring the card-section scope
    used elsewhere in this module.
    """
    import re as _re
    line_pattern = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")
    target = card_name.strip().lower()
    matches: list[str] = []
    for path in deck_dir.glob("*.dck"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        in_card_section = False
        found = False
        for raw in text.splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.startswith("[") and s.endswith("]"):
                in_card_section = s.lower() in ("[commander]", "[main]")
                continue
            if not in_card_section:
                continue
            m = line_pattern.match(s)
            if not m:
                continue
            if m.group(2).strip().lower() == target:
                found = True
                break
        if found:
            matches.append(path.stem)
    return sorted(matches)

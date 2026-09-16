"""Which decks may sit in a sim's FILLER seats (decision C1).

WHY THIS MODULE EXISTS (2026-09-03, R3 C-03)
============================================
Decision C1 (2026-08-17) excluded ``[REF]`` decks from filler seats —
"they stay pool candidates, but stop being seeded as fillers, matching
the ``[PREMADE]`` popularity rule". The exclusion tuple lived in
``_proposer_sim`` and was applied on ONE of the four filler paths
(``_pick_filler_decks``). ``compare_versions._pick_filler_pairs`` — the
path behind the web A/B, ``commander-compare``, ``commander-iterate`` and
``meta_test`` — seated ``run_match._load_pool()`` verbatim, and the
curator deliberately keeps ``[REF]`` decks as ranked candidates, so any
``[REF]`` deck that ranked top-6 was written into ``pool_a``/``pool_b``
and then seated as a filler by every ``compare()`` caller. The no-pool
fallback (``run_match._fallback_opponents``) skipped ``[USER]`` and
``[PREMADE]`` but not ``[REF]`` or ``[CONTROL]``.

One tuple, one predicate, imported by every filler picker — so the four
paths cannot drift again. Rationale per prefix:

  ``[USER]``     the user's own decks; the opponent pool is everything
                 WITHOUT the prefix, and a stale copy of the deck under
                 test must never sit across the table from itself.
  ``[CONTROL]``  do-nothing calibration decks: a filler that never wins
                 inflates the head-to-head decisive count for both sides.
  ``[PREMADE]``  popularity-ranked imports (Moxfield top-likes / EDHREC
                 average decks): skew pod opposition strength upward.
  ``[REF]``      meta-test references — the SAME popularity selection as
                 ``[PREMADE]``. A filler seat is never ranked, so its
                 strength silently sets the A/B baseline.

A curated pool JSON may legitimately carry ``[REF]`` entries (they are
candidates); this module is where they are turned away from the filler
seats specifically. The exclusion is LOUD: pickers report what they
excluded, and a pool that cannot seat a pod after exclusion raises
naming the counts by prefix rather than silently seating a control deck.
"""

from __future__ import annotations

#: Filename prefixes that are never filler-eligible. ``_proposer_sim``
#: re-exports this as ``_FILLER_EXCLUDED_PREFIXES`` so its picker and its
#: "why did I get zero fillers?" census (R2-P22) keep explaining THIS list.
FILLER_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "[USER]", "[CONTROL]", "[PREMADE]", "[REF]",
)


def is_filler_eligible(deck_filename: str) -> bool:
    """False for any deck whose filename starts with an excluded prefix."""
    return not deck_filename.startswith(FILLER_EXCLUDED_PREFIXES)


def partition_filler_candidates(
    candidates: "list[str]", exclude: "tuple[str, ...] | list[str]" = (),
) -> "tuple[list[str], dict[str, int]]":
    """Split ``candidates`` into ``(eligible, excluded_by_prefix)``.

    ``exclude`` names the decks under test (the compared pair); they are
    dropped FIRST and never counted against a prefix — blaming their
    ``[USER]`` prefix for an empty pool would explain the wrong thing to
    the operator (same rule as ``_proposer_sim._filler_exclusion_census``).
    Order of ``candidates`` is preserved so callers that rely on pool
    order (``compare_versions``' stride walk) see the same sequence minus
    the excluded entries.
    """
    exclude_set = set(exclude)
    eligible: list[str] = []
    by_prefix: dict[str, int] = {}
    for name in candidates:
        if name in exclude_set:
            continue
        matched = next(
            (pre for pre in FILLER_EXCLUDED_PREFIXES if name.startswith(pre)),
            None,
        )
        if matched is None:
            eligible.append(name)
        else:
            by_prefix[matched] = by_prefix.get(matched, 0) + 1
    return eligible, by_prefix


def describe_exclusions(by_prefix: "dict[str, int]") -> str:
    """``'[REF] 2, [CONTROL] 1'`` — for warnings and refusal messages."""
    return ", ".join(
        f"{pre} {by_prefix[pre]}"
        for pre in FILLER_EXCLUDED_PREFIXES if by_prefix.get(pre)
    ) or "none"

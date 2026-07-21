"""Pairwise co-occurrence *lift* over the locally harvested deck corpus.

ManaFoundry parity ("Lift Web", our twist): every pool/reference deck we
have harvested locally is a tiny vote about which cards belong together.
This module aggregates those votes into a classic market-basket statistic
and surfaces the result as "pairs well with your deck" candidate adds.

The statistic
-------------

For two cards A and B over a corpus of N decks::

    lift(A, B) = P(A and B) / (P(A) * P(B))
               = (co / N) / ((cA / N) * (cB / N))
               = co * N / (cA * cB)

where ``co`` is the number of decks containing BOTH cards and ``cA`` /
``cB`` the number of decks containing each individually.

* ``lift == 1``  — A and B co-occur exactly as often as chance predicts
  given their individual popularity. No signal.
* ``lift > 1``   — they appear together MORE often than chance: deck
  builders who pick one deliberately pick the other. That's the synergy
  signal we want.
* ``lift < 1``   — they actively avoid each other (competing archetypes,
  anti-synergy, or color-disjoint pools).

Why lift instead of raw co-occurrence counts: raw counts are dominated
by whatever is merely POPULAR. Sol Ring co-occurs with everything
because it's in everything — its co-counts are huge and its lift is
~1.0 across the board. Lift normalizes out popularity so the ranking
rewards *specific affinity*, not fame.

Guard rails (each is why-commented at its use site):

* **Support floor (>= 3 joint decks).** With ``co == 1`` the lift of two
  singleton cards is ``1 * N / (1 * 1) = N`` — a single coincidence
  masquerading as the strongest signal in the corpus. Requiring the
  pair in at least 3 independent harvested decks caps that variance;
  below the floor we simply refuse to report a lift at all.
* **Universal staples + basic lands excluded from the vocabulary.**
  ``P(staple) ~ 1`` so every staple pair has lift ~1 (pure noise), and
  basics appear in dozens of copies across every deck. Keeping them
  would bloat the pair matrix quadratically while contributing zero
  ranking signal.
* **Small-corpus refusal (< 10 decks).** With single-digit N the
  probabilities are so coarse (steps of 1/N >= 0.11) that lift values
  are numerically meaningless; we report "corpus too small" rather
  than confident-looking garbage.

Corpus definition
-----------------

Mirrors ``pool_curator._list_bracket_candidates`` across ALL brackets:
every ``*.dck`` in the deck dir EXCEPT ``[USER]`` (the user's own decks
— the whole point is to learn from the harvested community pool, and a
user deck must never inflate the lift of its own card choices) and
``[CONTROL]`` (calibration do-nothing decks, designed to carry no
signal). ``[REF]`` decks (meta_test's imported community references)
are deliberately KEPT — real, playable community builds are exactly the
population whose card-pairing wisdom we want to mine.

Cache
-----

The pair matrix is O(decks x deck-size^2) to build, so it's cached to a
JSON file under the repo ``.cache/`` dir (same convention as the
game_changers 7-day cache), keyed by a hash of the sorted corpus file
list + mtimes. Any harvest/rename/edit changes the key and triggers a
rebuild; loading a warm cache is a single JSON read because the file
only stores pairs above the support floor.

Everything here is stdlib-only and offline-safe. The only network-
adjacent seam is the OPTIONAL color-identity filter used by the
dashboard surface, which routes through the same disk-cached
``scryfall_client.lookup_card`` + ``enforce_color_identity`` machinery
as the proposer and degrades to "no filter" when the commander can't be
resolved.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import dck_utils
from .collection import name_key
from .staples import is_basic_land, is_universal_staple

REPO_ROOT = Path(__file__).resolve().parents[2]

# Versioned like game_changers.v2.json: bumping the suffix orphans old
# cache files instead of trying to migrate them. Resolved at CALL time
# inside load_or_build_matrix (the DEFAULT_DB_PATH lesson: tests
# monkeypatch the module attribute, so no def-time freezing).
DEFAULT_CACHE_PATH = REPO_ROOT / ".cache" / "lift_matrix.v1.json"

# See module docstring for the statistics rationale behind each knob.
SUPPORT_FLOOR = 3          # min joint decks before a pair earns a lift
MIN_CORPUS_DECKS = 10      # below this, refuse to compute anything
MIN_BAND_DECKS = 10        # per-band matrices need this many decks too
MIN_SUPPORTING_PAIRS = 2   # a candidate needs >= 2 in-deck partners —
#                            one strong pair could be a two-card combo
#                            we'd recommend into a deck that can't use
#                            it; two independent partners means the
#                            candidate fits the deck's *fabric*.
TOP_PAIRS_PER_CANDIDATE = 5  # score = mean of the candidate's top-5
#                              lifts vs in-deck cards. Mean-of-top-K
#                              rewards broad affinity without letting
#                              one outlier pair dominate (max would)
#                              or letting 90 irrelevant zero-lift
#                              non-pairs drown the signal (full mean
#                              would).
DEFAULT_PICK_LIMIT = 10

# Additive-advisor knobs: only picks whose aggregate lift clears 2.0
# ("appears together at least twice as often as chance") are strong
# enough to append next to EDHREC/peer-sourced recommendations, and we
# cap at 3 so lift stays supporting evidence, not the headline act.
ADVISOR_MIN_SCORE = 2.0
ADVISOR_PICK_LIMIT = 3

# Same [B<n>] filename-suffix convention as web/_helpers
# ``_bracket_from_filename`` — re-implemented here because core modules
# must not import from the web layer (layering rule; see
# collection.name_key's identical rationale).
_BRACKET_RE = re.compile(r"\[B(\d)\]\.dck\s*$")


@dataclass
class CorpusDeck:
    """One harvested deck, reduced to its lift-relevant vocabulary."""
    filename: str
    # name_key()s of [Main]+[Commander] cards, staples/basics excluded.
    cards: frozenset[str]
    bracket: Optional[int]
    # key -> first-seen original casing, so downstream surfaces can
    # render "Rhystic Study" instead of the lowercase matching key.
    display_names: dict[str, str] = field(default_factory=dict)


def bracket_from_filename(filename: str) -> Optional[int]:
    """Parse the declared ``[B<n>].dck`` suffix; None when absent/invalid."""
    m = _BRACKET_RE.search(filename)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 5 else None


def band_for_bracket(bracket: Optional[int]) -> Optional[str]:
    """Map a declared bracket to its lift band.

    Bands are B1-2 / B3 / B4-5 rather than five separate brackets
    because per-single-bracket sub-corpora are usually too thin to
    clear MIN_BAND_DECKS, and adjacent brackets share deck-building
    norms (B1-2 casual precons vs B4-5 optimized) far more than they
    differ.
    """
    if bracket in (1, 2):
        return "B1-2"
    if bracket == 3:
        return "B3"
    if bracket in (4, 5):
        return "B4-5"
    return None


def _is_corpus_file(name: str) -> bool:
    """Corpus membership rule — see module docstring 'Corpus definition'."""
    return (
        name.endswith(".dck")
        and not name.startswith("[USER]")
        and not name.startswith("[CONTROL]")
    )


def _deck_vocab_keys(deck_text: str) -> tuple[set[str], dict[str, str]]:
    """Extract the lift vocabulary from one deck's text.

    Returns ``(keys, display_names)`` over [Main]+[Commander]. Basics
    and universal staples are dropped here — at ingestion — so they
    never enter the pair matrix at all (cheaper than filtering at
    query time, and it keeps the cached matrix small).
    """
    keys: set[str] = set()
    display: dict[str, str] = {}
    for section in ("Main", "Commander"):
        for raw_name in dck_utils.section_card_names(deck_text, section):
            key = name_key(raw_name)
            if not key:
                continue
            # name_key lower-cases, which is exactly the form the
            # staples frozensets are keyed on.
            if is_basic_land(key) or is_universal_staple(key):
                continue
            if key not in keys:
                keys.add(key)
                display[key] = raw_name.split("//", 1)[0].strip()
    return keys, display


def build_corpus(deck_dir: Path) -> list[CorpusDeck]:
    """Enumerate + parse every corpus-eligible deck under ``deck_dir``.

    Unreadable files are skipped rather than failing the whole scan —
    a single corrupt harvest file must not disable the feature.
    """
    if not deck_dir or not Path(deck_dir).is_dir():
        return []
    out: list[CorpusDeck] = []
    for f in sorted(Path(deck_dir).glob("*.dck")):
        if not _is_corpus_file(f.name):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        keys, display = _deck_vocab_keys(text)
        if not keys:
            continue  # empty / all-staple decks carry no pair signal
        out.append(CorpusDeck(
            filename=f.name,
            cards=frozenset(keys),
            bracket=bracket_from_filename(f.name),
            display_names=display,
        ))
    return out


def _count_section(decks: list[CorpusDeck]) -> dict:
    """Counts + above-floor pairs for one slice of the corpus.

    Pairs are stored sparse, dict-of-dicts, with the lexicographically
    smaller key on the outside (``pairs[a][b]`` with ``a < b``) so each
    unordered pair is stored exactly once. Only pairs whose joint
    support clears SUPPORT_FLOOR are kept — that's what keeps the
    cached matrix loadable in one cheap JSON read.
    """
    counts: Counter = Counter()
    pair_counts: Counter = Counter()
    for deck in decks:
        cards = sorted(deck.cards)
        counts.update(cards)
        # All C(k,2) in-deck pairs; sorted order gives the a<b invariant.
        for i, a in enumerate(cards):
            for b in cards[i + 1:]:
                pair_counts[(a, b)] += 1
    pairs: dict[str, dict[str, int]] = {}
    for (a, b), co in pair_counts.items():
        if co < SUPPORT_FLOOR:
            continue  # support floor — see module docstring
        pairs.setdefault(a, {})[b] = co
    return {
        "n_decks": len(decks),
        "counts": dict(counts),
        "pairs": pairs,
    }


def compute_lift_matrix(corpus: list[CorpusDeck]) -> dict:
    """Fold the corpus into the (JSON-serializable) lift matrix.

    Shape::

        {
          "too_small": bool,          # True -> everything else empty
          "n_decks": int,
          "counts": {key: cA},        # deck-level presence counts
          "pairs":  {a: {b: co}},     # a < b, co >= SUPPORT_FLOOR
          "names":  {key: "Display"}, # first-seen original casing
          "bands":  {"B3": {n_decks, counts, pairs}, ...},
        }

    Bands (B1-2 / B3 / B4-5) are only materialized when they hold at
    least MIN_BAND_DECKS decks — a thin band would just re-introduce
    the small-N variance the corpus-wide floor exists to prevent.
    Decks without a [Bn] tag contribute to the overall matrix but to
    no band.
    """
    n = len(corpus)
    if n < MIN_CORPUS_DECKS:
        # Refuse rather than emit garbage — see module docstring.
        return {
            "too_small": True, "n_decks": n,
            "counts": {}, "pairs": {}, "names": {}, "bands": {},
        }
    overall = _count_section(corpus)
    names: dict[str, str] = {}
    for deck in corpus:
        for key, disp in deck.display_names.items():
            names.setdefault(key, disp)
    bands: dict[str, dict] = {}
    by_band: dict[str, list[CorpusDeck]] = defaultdict(list)
    for deck in corpus:
        band = band_for_bracket(deck.bracket)
        if band:
            by_band[band].append(deck)
    for band, decks in by_band.items():
        if len(decks) >= MIN_BAND_DECKS:
            bands[band] = _count_section(decks)
    return {
        "too_small": False,
        "n_decks": overall["n_decks"],
        "counts": overall["counts"],
        "pairs": overall["pairs"],
        "names": names,
        "bands": bands,
    }


# --- Cache -----------------------------------------------------------------

def corpus_fingerprint(deck_dir: Path) -> str:
    """Hash of the sorted corpus file list + mtimes.

    mtime_ns (not content) keeps the check O(files) — a stat walk, no
    reads. Any add/remove/rename/edit of a corpus file perturbs the
    hash and invalidates the cache; editing a [USER] deck does NOT
    (user decks aren't corpus members, so their churn is irrelevant).
    """
    h = hashlib.sha256()
    if Path(deck_dir).is_dir():
        for f in sorted(Path(deck_dir).glob("*.dck")):
            if not _is_corpus_file(f.name):
                continue
            try:
                mtime = f.stat().st_mtime_ns
            except OSError:
                mtime = -1
            h.update(f"{f.name}|{mtime}\n".encode("utf-8"))
    return h.hexdigest()


def load_or_build_matrix(
    deck_dir: Path, cache_path: Optional[Path] = None,
) -> dict:
    """Return the lift matrix for ``deck_dir``, via cache when fresh.

    Cache hit = stored fingerprint matches the current stat walk.
    Any read/parse failure falls through to a rebuild — a corrupt
    cache must never disable the feature. Too-small corpora are NOT
    cached: there is nothing worth caching, and skipping the write
    keeps toy/temporary deck dirs (tests, fresh installs) from
    churning the real cache file.
    """
    if cache_path is None:
        cache_path = DEFAULT_CACHE_PATH  # call-time module attr (testable)
    key = corpus_fingerprint(deck_dir)
    try:
        cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        if cached.get("corpus_key") == key and isinstance(
            cached.get("matrix"), dict,
        ):
            return cached["matrix"]
    except (OSError, ValueError):
        pass  # missing / corrupt cache -> rebuild
    matrix = compute_lift_matrix(build_corpus(deck_dir))
    if not matrix.get("too_small"):
        try:
            cache_path = Path(cache_path)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"corpus_key": key, "matrix": matrix}),
                encoding="utf-8",
            )
        except OSError:
            pass  # read-only FS etc. — caching is best-effort
    return matrix


# --- Lift queries ----------------------------------------------------------

def pair_support(section: dict, a: str, b: str) -> int:
    """Joint-deck count for an unordered pair; 0 when below floor/absent."""
    if a > b:
        a, b = b, a  # stored a < b
    return int(section.get("pairs", {}).get(a, {}).get(b, 0))


def lift_value(section: dict, a: str, b: str) -> Optional[float]:
    """lift(a, b) within one matrix section; None below the support floor."""
    co = pair_support(section, a, b)
    if co < SUPPORT_FLOOR:
        return None
    n = section.get("n_decks", 0)
    ca = section.get("counts", {}).get(a, 0)
    cb = section.get("counts", {}).get(b, 0)
    if n <= 0 or ca <= 0 or cb <= 0:
        return None  # degenerate — can't happen for a well-formed matrix
    return (co * n) / (ca * cb)


def _section_for(matrix: dict, bracket: Optional[int]) -> tuple[dict, str]:
    """Pick the band sub-matrix for ``bracket`` when it exists, else the
    overall matrix. Returns ``(section, band_label)`` where band_label
    is "overall" on fallback so surfaces can disclose which population
    the numbers came from."""
    band = band_for_bracket(bracket)
    if band and band in matrix.get("bands", {}):
        return matrix["bands"][band], band
    return matrix, "overall"


def _display(matrix: dict, key: str) -> str:
    return matrix.get("names", {}).get(key, key)


def deck_keys_for_path(deck_path: Path) -> set[str]:
    """The user deck's cards in matrix-vocabulary form ([Main]+[Commander],
    name_key'd, staples/basics dropped — same convention as the corpus
    so membership tests line up exactly)."""
    try:
        text = Path(deck_path).read_text(encoding="utf-8")
    except OSError:
        return set()
    keys, _display_map = _deck_vocab_keys(text)
    return keys


def top_deck_pairs(
    matrix: dict,
    deck_keys: set[str],
    bracket: Optional[int] = None,
    limit: int = DEFAULT_PICK_LIMIT,
) -> list[dict]:
    """The deck's OWN strongest synergies: in-deck pairs that clear the
    support floor, ranked by lift. This is the "your deck's spine"
    view the CLI's --show-lift prints — useful for seeing which cards
    the harvested corpus considers this deck's core packages (and,
    by absence, which cards pair with nothing)."""
    if matrix.get("too_small"):
        return []
    section, band = _section_for(matrix, bracket)
    rows: list[dict] = []
    keys = sorted(deck_keys)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            lift = lift_value(section, a, b)
            if lift is None:
                continue
            rows.append({
                "card_a": _display(matrix, a),
                "card_b": _display(matrix, b),
                "co": pair_support(section, a, b),
                "lift": round(lift, 2),
                "band": band,
            })
    rows.sort(key=lambda r: (-r["lift"], r["card_a"], r["card_b"]))
    return rows[:limit]


def lift_candidates(
    matrix: dict,
    deck_keys: set[str],
    bracket: Optional[int] = None,
    limit: int = DEFAULT_PICK_LIMIT,
) -> list[dict]:
    """Rank corpus-vocabulary cards NOT in the deck by aggregate lift
    against the deck's cards.

    Ranking formula (the one true definition — every surface routes
    through here):

    1. For each candidate, collect its above-floor pairs with in-deck
       cards whose ``lift > 1.0`` ("supporting pairs" — lift <= 1 is
       chance-or-worse co-occurrence, i.e. not evidence of fit).
    2. Require >= MIN_SUPPORTING_PAIRS supporting pairs (a single
       strong partner is combo-piece territory, not deck fit).
    3. score = mean of the top TOP_PAIRS_PER_CANDIDATE lifts.
    4. Sort by score desc; ties break by supporting-pair count desc
       (broader fit wins), then name for determinism.

    Each row carries a human rationale built from the candidate's
    single strongest partner:
    "appears with <in-deck card> in <co>/<cA> harvested decks (lift <x.x>)"
    where cA is the candidate's own corpus count — i.e. "of the cA
    harvested decks running this card, co also ran your <partner>".
    """
    if matrix.get("too_small"):
        return []
    section, band = _section_for(matrix, bracket)
    # Symmetric adjacency view over the deck's cards only: iterate the
    # stored pairs once and index both directions. O(pairs incident to
    # the deck) — much cheaper than probing every (candidate, deck
    # card) combination.
    supports: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
    n = section.get("n_decks", 0)
    counts = section.get("counts", {})
    for a, row in section.get("pairs", {}).items():
        for b, co in row.items():
            # Exactly one of (a, b) must be in the deck for this pair
            # to link a candidate to the deck.
            if (a in deck_keys) == (b in deck_keys):
                continue
            cand, in_deck = (b, a) if a in deck_keys else (a, b)
            ca, cb = counts.get(cand, 0), counts.get(in_deck, 0)
            if n <= 0 or ca <= 0 or cb <= 0:
                continue
            lift = (co * n) / (ca * cb)
            if lift <= 1.0:
                continue  # chance-or-worse — not a supporting pair
            supports[cand].append((lift, co, in_deck))
    out: list[dict] = []
    for cand, pairs in supports.items():
        if len(pairs) < MIN_SUPPORTING_PAIRS:
            continue
        pairs.sort(key=lambda t: -t[0])
        top = pairs[:TOP_PAIRS_PER_CANDIDATE]
        score = sum(t[0] for t in top) / len(top)
        best_lift, best_co, best_partner = top[0]
        ca = counts.get(cand, 0)
        out.append({
            "card": _display(matrix, cand),
            "key": cand,
            "score": round(score, 2),
            "n_pairs": len(pairs),
            "band": band,
            "rationale": (
                f"appears with {_display(matrix, best_partner)} in "
                f"{best_co}/{ca} harvested decks (lift {best_lift:.1f})"
            ),
        })
    out.sort(key=lambda r: (-r["score"], -r["n_pairs"], r["card"]))
    return out[:limit]


# --- Surface A: dashboard payload ------------------------------------------

def _resolve_deck_ci(deck_path: Path) -> Optional[str]:
    """Best-effort deck color identity with the standard 'colorless vs
    unresolvable' disambiguation (mirrors _advise_steps / proposer):
    when NO commander resolves in Scryfall, return None so the CI
    filter is skipped entirely — better a few off-color lift picks
    than an empty panel every time Scryfall is unreachable."""
    try:
        from .scryfall_client import (
            _parse_commander_names_from_dck,
            color_identity_for_commander,
            lookup_card,
        )
        commanders = _parse_commander_names_from_dck(Path(deck_path))
        resolved = False
        for name in commanders:
            try:
                card = lookup_card(name)
            except Exception:  # noqa: BLE001 — network blip != colorless
                card = None
            if isinstance(card, dict) and "color_identity" in card:
                resolved = True
                break
        if not resolved:
            return None
        return color_identity_for_commander(Path(deck_path))
    except Exception:  # noqa: BLE001 — resolver failure = skip CI filter
        return None


def lift_picks_payload(
    deck_path: Path,
    deck_dir: Path,
    bracket: Optional[int] = None,
    cache_path: Optional[Path] = None,
    ci_filter: Optional[Callable] = None,
    resolve_ci: Optional[Callable] = None,
    limit: int = DEFAULT_PICK_LIMIT,
) -> dict:
    """The dashboard's "Lift picks" payload.

    Shape (stable contract for app.js)::

        {
          "corpus_size": int,
          "band": "B3" | "B1-2" | "B4-5" | "overall",
          "picks": [{card, score, n_pairs, rationale}],
          "reason": str | None,   # set when picks is empty by design
        }

    ``ci_filter`` / ``resolve_ci`` are injectable for tests (fake
    identity resolver); production defaults are the proposer's
    ``enforce_color_identity`` and the Scryfall-backed resolver above.
    """
    matrix = load_or_build_matrix(deck_dir, cache_path=cache_path)
    if matrix.get("too_small"):
        return {
            "corpus_size": matrix.get("n_decks", 0),
            "band": "overall",
            "picks": [],
            "reason": (
                f"corpus too small ({matrix.get('n_decks', 0)} harvested "
                f"decks; need {MIN_CORPUS_DECKS})"
            ),
        }
    deck_keys = deck_keys_for_path(deck_path)
    candidates = lift_candidates(
        matrix, deck_keys, bracket=bracket,
        # Over-fetch before the CI filter so dropping off-color picks
        # doesn't leave a half-empty panel.
        limit=limit * 3,
    )
    band = candidates[0]["band"] if candidates else (
        _section_for(matrix, bracket)[1]
    )
    if ci_filter is None:
        from ._proposer_filters import enforce_color_identity as ci_filter
    if resolve_ci is None:
        resolve_ci = _resolve_deck_ci
    deck_ci = resolve_ci(deck_path)
    if deck_ci is not None:
        kept, _dropped = ci_filter([c["card"] for c in candidates], deck_ci)
        kept_set = {k.lower() for k in kept}
        candidates = [c for c in candidates if c["card"].lower() in kept_set]
    picks = [
        {
            "card": c["card"],
            "score": c["score"],
            "n_pairs": c["n_pairs"],
            "rationale": c["rationale"],
        }
        for c in candidates[:limit]
    ]
    return {
        "corpus_size": matrix.get("n_decks", 0),
        "band": band,
        "picks": picks,
        "reason": None if picks else "no candidates cleared the lift bar",
    }


# --- Surface B: advisor recommendations ------------------------------------

def lift_recommendations(
    deck_path: Path,
    deck_dir: Path,
    bracket: Optional[int] = None,
    cache_path: Optional[Path] = None,
    limit: int = ADVISOR_PICK_LIMIT,
    min_score: float = ADVISOR_MIN_SCORE,
) -> list:
    """A few top lift picks as advisor ``SwapRecommendation`` adds.

    ADDITIVE by design: the orchestrator appends these AFTER the
    manabase + primary-source recommendations, so dedup lets the
    established sources win any collision and lift only ever
    contributes cards nobody else surfaced. Threshold ``min_score``
    (default 2.0 = "co-occurs at least twice as often as chance")
    keeps weak corpus noise out of the advisor entirely.

    No color-identity filtering here — the advisor pipeline runs its
    own ``enforce_color_identity`` pass over ALL adds downstream, and
    double-filtering would just cost extra Scryfall lookups.

    Returns [] (never raises to the caller's benefit — but the
    orchestrator still wraps the call fail-quiet) when the corpus is
    too small or nothing clears the bar.
    """
    from ._advisor_models import SwapRecommendation

    matrix = load_or_build_matrix(deck_dir, cache_path=cache_path)
    if matrix.get("too_small"):
        return []
    deck_keys = deck_keys_for_path(deck_path)
    candidates = [
        c for c in lift_candidates(matrix, deck_keys, bracket=bracket,
                                   limit=limit)
        if c["score"] >= min_score
    ]

    def _role_for(card_name: str) -> str:
        # Same role vocabulary as the other sources so the saturation
        # filter treats lift adds identically. Fail-quiet to "unknown"
        # (never saturates) on lookup failure — a Scryfall blip must
        # not crash the audit over supplemental evidence.
        try:
            from .scryfall_client import lookup_card
            from .staples import classify_role_extended
            card = lookup_card(card_name)
            if not card:
                return "unknown"
            return classify_role_extended(
                card.get("oracle_text", "") or "",
                card.get("type_line", "") or "",
            )
        except Exception:  # noqa: BLE001
            return "unknown"

    return [
        SwapRecommendation(
            card=c["card"],
            action="add",
            reason=f"Lift Web: {c['rationale']}",
            evidence={
                "source": "lift",
                "lift_score": c["score"],
                "supporting_pairs": c["n_pairs"],
                "band": c["band"],
                "role": _role_for(c["card"]),
            },
        )
        for c in candidates
    ]


# --- Surface C: CLI text ---------------------------------------------------

def format_lift_report(
    deck_path: Path,
    deck_dir: Path,
    bracket: Optional[int] = None,
    cache_path: Optional[Path] = None,
) -> str:
    """Render the --show-lift section for commander-advise: the deck's
    own strongest in-deck pairs, then the top candidate adds."""
    lines = ["", "-" * 60, " Lift analysis (harvested-corpus co-occurrence)",
             "-" * 60]
    matrix = load_or_build_matrix(deck_dir, cache_path=cache_path)
    if matrix.get("too_small"):
        lines.append(
            f"Corpus too small ({matrix.get('n_decks', 0)} harvested decks; "
            f"need {MIN_CORPUS_DECKS}). Harvest more pool/reference decks "
            f"to enable lift analysis."
        )
        return "\n".join(lines)
    deck_keys = deck_keys_for_path(deck_path)
    section_label = _section_for(matrix, bracket)[1]
    lines.append(
        f"Corpus: {matrix.get('n_decks', 0)} harvested decks "
        f"(band: {section_label})"
    )
    pairs = top_deck_pairs(matrix, deck_keys, bracket=bracket)
    lines.append("")
    lines.append(f"Top in-deck pairs ({len(pairs)}):")
    if not pairs:
        lines.append("  (no in-deck pair clears the support floor)")
    for p in pairs:
        lines.append(
            f"  {p['card_a']} + {p['card_b']} — together in {p['co']} "
            f"harvested decks (lift {p['lift']:.1f})"
        )
    cands = lift_candidates(matrix, deck_keys, bracket=bracket)
    lines.append("")
    lines.append(f"Top candidate adds ({len(cands)}):")
    if not cands:
        lines.append("  (no candidate cleared the lift bar)")
    for c in cands:
        lines.append(
            f"  + {c['card']} (score {c['score']:.2f}, "
            f"{c['n_pairs']} supporting pairs) — {c['rationale']}"
        )
    return "\n".join(lines)

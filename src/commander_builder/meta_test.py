"""Meta-reference benchmarking — pit your deck against canonical reference
builds to see which cards the better-performing builds use that yours
doesn't.

Workflow:
  1. Resolve user deck's commander.
  2. Auto-fetch two reference decks:
       - Moxfield top-likes (`commanderName` search, sort by likes)
       - EDHREC's "Average Deck" sample (URL parsed from the commander page)
  3. Import each via existing moxfield_import (writes them to the
     commander/ dir as REF-tagged .dck files so they don't pollute the
     [USER] namespace).
  4. Run compare_versions(user, reference) for each pair.
  5. Diff the cards across all three decks:
       - "must-add"   = in BOTH references, NOT in user
       - "consider"   = in EXACTLY ONE reference, NOT in user
       - "off-meta"   = in user, NOT in EITHER reference
  6. Aggregate winner stats across the comparisons. If the references won
     decisively, the missing cards are concrete recommendation targets.

CLI:

    commander-meta-test --user "[USER] Hakbal of the Surging Soul [B3].dck" --bracket 3 --games 5

By default uses two auto-fetched references. Add `--reference-url <url>` to
include a manually-chosen reference (e.g. a specific Moxfield deck) IN
ADDITION TO the auto-fetched ones.

Honest framing on "saltiest from EDHREC": EDHREC publishes salt scores per
CARD, not per deck. There's no "saltiest deck" view. We use EDHREC's
"Average Deck" as the canonical reference instead — it's the well-defined
sample they auto-generate per commander.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .compare_versions import COMPARE_OUT_DIR, ComparisonReport, compare
from .edhrec_client import (
    fetch_average_deck,
    fetch_commander_page,
)
from .forge_runner import VENDOR_FORGE
from .moxfield_import import (
    DECK_OUT_DIR,
    deck_destination,
    fetch_deck,
    find_top_liked_deck_for_commander,
    parse_deck_id,
    resolve_bracket,
    safe_filename,
    to_dck,
)
from .scryfall_client import _parse_commander_names_from_dck

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"
META_OUT_DIR = DECK_DIR / "_meta"


@dataclass
class ReferenceDeck:
    """One reference deck pulled from a meta source."""
    source: str             # "moxfield_top_likes" | "edhrec_average" | "manual"
    moxfield_id: Optional[str]
    name: str
    bracket: int
    deck_filename: str      # On-disk filename (REF-prefixed)
    main_cards: list[str] = field(default_factory=list)


@dataclass
class CardSuggestion:
    """One card recommended by the diff, with metadata for ranking."""
    card: str
    in_n_references: int      # 1 ≤ n ≤ total_references
    total_references: int
    role: str = "other"       # ramp / draw / removal / finisher / etc.

    @property
    def confidence(self) -> str:
        """Human-readable label: 'ALL', '<N>/<M>'."""
        if self.in_n_references == self.total_references:
            return "ALL"
        return f"{self.in_n_references}/{self.total_references}"

    @property
    def frequency_label(self) -> str:
        """Render the reference-frequency label used in report views.

        Delegates to ``staples.render_frequency_label`` so the same label
        format ("unanimous", "majority", "minority") is used wherever this
        suggestion is rendered, not just in the meta_test report."""
        from .staples import render_frequency_label
        return render_frequency_label(self.in_n_references, self.total_references)

    @property
    def confidence_tier(self) -> int:
        """Bucket 0..3 — used for sorting and visual emphasis."""
        from .staples import confidence_tier
        return confidence_tier(self.in_n_references, self.total_references)


@dataclass
class CardDiffReport:
    """Set arithmetic over user vs each reference deck."""
    user_cards: list[str] = field(default_factory=list)
    must_add: list[CardSuggestion] = field(default_factory=list)   # in ALL refs, not user
    consider: list[CardSuggestion] = field(default_factory=list)   # in some refs, not user
    off_meta: list[str] = field(default_factory=list)              # in user, in NO refs
    shared_with_user: list[str] = field(default_factory=list)      # in user AND >=1 ref
    excluded_universal_staples: list[str] = field(default_factory=list)  # debug: what we dropped

    def must_add_by_role(self) -> dict[str, list[CardSuggestion]]:
        """Group must-add suggestions by classified role. Categories appear
        in priority order — finishers and tutors first since those address
        the common 'deck can't close' diagnosis."""
        priority = ["finisher", "lord", "tutor", "wipe", "removal",
                    "counter", "draw", "ramp", "other"]
        groups: dict[str, list[CardSuggestion]] = {k: [] for k in priority}
        for s in self.must_add:
            groups.setdefault(s.role, []).append(s)
        # Drop empty groups for cleaner output.
        return {k: v for k, v in groups.items() if v}


@dataclass
class MetaTestReport:
    user_deck: str
    bracket: int
    timestamp: str
    references: list[ReferenceDeck] = field(default_factory=list)
    comparisons: list[dict] = field(default_factory=list)    # ComparisonReport.to_dict()
    card_diff: CardDiffReport = field(default_factory=CardDiffReport)
    user_record: dict = field(default_factory=dict)          # aggregate W/L/D vs all refs

    def to_dict(self) -> dict:
        return asdict(self)


# --- Reference fetching ----------------------------------------------------

REF_PREFIX = "[REF]"


# Universal Commander staples — present in ~every deck regardless of strategy.
# These should never show up in must-add (everyone has them) or off-meta
# (universal). Sourced from `staples.py` so the canonical list lives in one
# place. Adds basic lands (handled separately in staples) since meta_test's
# diff logic needs them in the same set.
from .staples import UNIVERSAL_STAPLES_LC, BASIC_LANDS_LC

UNIVERSAL_STAPLES: frozenset[str] = UNIVERSAL_STAPLES_LC | BASIC_LANDS_LC


# Card-name → role map for grouping must-add suggestions. Heuristic — not
# every card matches; falls through to "other" when unclassified. The role
# tags map to the diagnosis-driven recommendation flow (e.g. "deck needs
# a finisher" → highlight the `finisher`-tagged adds first).
def _classify_card_role(card_name: str) -> str:
    """Best-effort role classification by name + known-card lookup. Cheap
    and approximate — not a substitute for actual oracle text."""
    lc = card_name.lower()
    # Tutors first — they're often miscategorized as "creature" or "instant".
    if any(k in lc for k in (
        "tutor", "demonic", "mystical", "vampiric", "enlightened",
        "worldly tutor", "imperial seal", "diabolic intent",
        "chord of calling", "eldritch evolution", "green sun's zenith",
        "natural order", "finale of devastation",
    )):
        return "tutor"
    # Removal — single target.
    if any(k in lc for k in (
        "swords to plowshares", "path to exile", "generous gift", "beast within",
        "anguished unmaking", "assassin's trophy", "go for the throat",
        "fatal push", "abrupt decay", "oust", "pongify", "rapid hybridization",
        "reality shift",
    )):
        return "removal"
    # Sweepers / mass removal.
    if any(k in lc for k in (
        "wrath of god", "damnation", "toxic deluge", "blasphemous act",
        "supreme verdict", "farewell", "merciless eviction", "cyclonic rift",
        "evacuation", "river's rebuke", "kindred dominance",
    )):
        return "wipe"
    # Counterspells.
    if any(k in lc for k in (
        "counterspell", "negate", "swan song", "dispel",
        "force of will", "force of negation", "fierce guardianship",
        "pact of negation", "an offer you can't refuse", "mana drain",
    )):
        return "counter"
    # Card draw engines.
    if any(k in lc for k in (
        "rhystic study", "mystic remora", "necropotence", "phyrexian arena",
        "kindred discovery", "guardian project", "soul of the harvest",
        "reconnaissance mission", "coastal piracy",
    )):
        return "draw"
    # Ramp.
    if any(k in lc for k in (
        "rampant growth", "cultivate", "kodama's reach", "skyshroud claim",
        "nature's lore", "three visits", "farseek", "wood elves",
        "birds of paradise", "noble hierarch", "ignoble hierarch",
        "delighted halfling", "elvish mystic", "llanowar elves",
        "fyndhorn elves", "selvala", "carpet of flowers",
    )):
        return "ramp"
    # Finishers — board-wide buffs / alpha-strike enablers.
    if any(k in lc for k in (
        "craterhoof behemoth", "overwhelming stampede", "triumph of the hordes",
        "finale of devastation", "kindred summons", "akroma's will",
        "doubling season", "anointed procession", "parallel lives",
        "coat of arms", "shared animosity", "cathars' crusade",
        "thunderfoot baloth", "end-raze forerunners", "pathbreaker ibex",
    )):
        return "finisher"
    # Tribal lord-likes (good for tribal decks; flagged as 'lord').
    if any(k in lc for k in (
        "lord of atlantis", "master of the pearl trident", "merrow reejerey",
        "metallic mimic", "deeproot champion", "tatyova",
    )):
        return "lord"
    return "other"


def _ref_destination(source: str, deck_name: str, bracket: int,
                     base: Path = DECK_OUT_DIR) -> Path:
    """Compute the on-disk path for a reference .dck. `[REF]` prefix keeps
    it distinct from `[USER]` decks so it's never selected as a candidate
    in pool curation."""
    bracket_suffix = f" [B{bracket}]" if bracket else " [B?]"
    safe = safe_filename(deck_name)
    # Tag the source in the filename so the user can tell where it came from.
    tag = source.replace("_", "-")
    return base / f"{REF_PREFIX} {tag} {safe}{bracket_suffix}.dck"


def _import_reference(deck_json: dict, source: str,
                      deck_dir: Path = DECK_OUT_DIR) -> ReferenceDeck:
    """Write a reference deck to disk with the [REF] prefix."""
    bracket = resolve_bracket(deck_json) or 0
    name = deck_json.get("name", "Unknown Reference")
    text = to_dck(deck_json)
    out_path = _ref_destination(source, name, bracket, base=deck_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    main_cards = _parse_main_card_names(out_path)
    return ReferenceDeck(
        source=source,
        moxfield_id=deck_json.get("publicId"),
        name=name,
        bracket=bracket,
        deck_filename=out_path.name,
        main_cards=main_cards,
    )


def _parse_main_card_names(deck_path: Path) -> list[str]:
    """Pull just the [Main] card names (without qty / set / cn)."""
    if not deck_path.exists():
        return []
    out: list[str] = []
    in_main = False
    for raw in deck_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() == "[main]":
            in_main = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_main = False
            continue
        if in_main:
            m = re.match(r"^\d+\s+(.+?)(?:\|.*)?$", line)
            if m:
                out.append(m.group(1).strip())
    return out


def _fetch_edhrec_average_deck(
    commander_name: str,
    bracket: Optional[int] = None,
    budget: Optional[str] = None,
) -> Optional[dict]:
    """Fetch EDHREC's average deck for a commander+bracket+budget.

    Returns a Moxfield-shape deck JSON (so it flows through `_import_reference`
    unchanged), or None if EDHREC doesn't publish one for the request.

    Strategy: try a sequence of URL specificity levels —
      1. `<commander>/<bracket>/<budget>` (most specific)
      2. `<commander>/<bracket>` (no budget tier)
      3. `<commander>` (whatever EDHREC defaults to)
    First hit wins. The previous implementation looked for a Moxfield URL
    inside the commander page's __NEXT_DATA__ blob; that data isn't there
    (EDHREC's average decks live on edhrec.com, not on Moxfield)."""
    attempts: list[tuple[Optional[int], Optional[str]]] = []
    if bracket and budget:
        attempts.append((bracket, budget))
    if bracket:
        attempts.append((bracket, None))
    attempts.append((None, None))

    for try_bracket, try_budget in attempts:
        deck = fetch_average_deck(
            commander_name,
            bracket=try_bracket,
            budget=try_budget,
        )
        if deck is None:
            continue
        return deck.to_moxfield_shape(bracket_int=bracket)
    return None


def _fetch_edhrec_deck_by_url(direct_url: str) -> Optional[dict]:
    """Direct-URL variant for `--reference-url <edhrec-url>` invocations."""
    deck = fetch_average_deck(
        commander_or_slug="",
        direct_url=direct_url,
    )
    if deck is None:
        return None
    return deck.to_moxfield_shape()


def fetch_reference_decks(
    commander_name: str,
    bracket: Optional[int] = None,
    deck_dir: Path = DECK_OUT_DIR,
    extra_urls: Optional[list[str]] = None,
    verbose: bool = True,
) -> list[ReferenceDeck]:
    """Auto-fetch the canonical references for a commander, plus any manually
    supplied URLs. Always tries Moxfield top-likes + EDHREC average; if either
    fails, the result list shrinks (caller decides whether that's OK).

    `verbose=True` prints per-source diagnostics so the user can see which
    auto-fetch paths failed and why."""
    refs: list[ReferenceDeck] = []

    if verbose:
        print(f"  [moxfield] looking up most-liked deck for {commander_name!r}...",
              flush=True)
    mox_top = find_top_liked_deck_for_commander(
        commander_name, bracket=bracket, verbose=verbose,
    )
    if mox_top:
        refs.append(_import_reference(mox_top, "moxfield_top_likes", deck_dir))
        if verbose:
            print(f"  [moxfield] OK: {mox_top.get('name')!r} "
                  f"(publicId={mox_top.get('publicId')})", flush=True)
    elif verbose:
        print("  [moxfield] no result. Try passing --reference-url manually.",
              flush=True)

    if verbose:
        print(f"  [edhrec] looking up average deck for {commander_name!r}"
              f"{f' (bracket {bracket})' if bracket else ''}...",
              flush=True)
    edhrec_avg = _fetch_edhrec_average_deck(commander_name, bracket=bracket)
    if edhrec_avg:
        refs.append(_import_reference(edhrec_avg, "edhrec_average", deck_dir))
        if verbose:
            print(f"  [edhrec] OK: {edhrec_avg.get('name')!r} "
                  f"(publicId={edhrec_avg.get('publicId')})", flush=True)
    elif verbose:
        print("  [edhrec] no average-deck URL found on the EDHREC page.",
              flush=True)

    for url in extra_urls or []:
        deck_json: Optional[dict]
        try:
            if "edhrec.com/average-decks" in url.lower():
                deck_json = _fetch_edhrec_deck_by_url(url)
                source_label = "manual_edhrec"
            else:
                deck_json = fetch_deck(parse_deck_id(url))
                source_label = "manual"
        except Exception as exc:
            if verbose:
                print(f"  [manual] {url} -> failed: {type(exc).__name__}: {exc}",
                      flush=True)
            continue
        if deck_json is None:
            if verbose:
                print(f"  [manual] {url} -> empty result.", flush=True)
            continue
        refs.append(_import_reference(deck_json, source_label, deck_dir))
        if verbose:
            print(f"  [{source_label}] OK: {deck_json.get('name')!r}", flush=True)

    return refs


# --- Card-set diff ---------------------------------------------------------

def compute_card_diff(
    user_cards: list[str],
    references: list[ReferenceDeck],
) -> CardDiffReport:
    """Set arithmetic over user vs N references with three quality filters:

      1. Universal staples (Sol Ring, Arcane Signet, basics, etc.) are
         dropped from both must-add and off-meta — they're noise either way.
      2. Each suggestion carries a frequency count (`in_n_references`) so
         the caller can rank by confidence (ALL > some).
      3. Suggestions are tagged with a role classification so callers can
         group by ramp/draw/removal/finisher/etc.
    """
    user_set = {c.lower() for c in user_cards}
    ref_sets = [{c.lower() for c in r.main_cards} for r in references]
    n_refs = len(ref_sets)

    if not ref_sets:
        return CardDiffReport(user_cards=sorted(user_cards))

    # Frequency map: how many references contain each card.
    from collections import Counter
    freq: Counter[str] = Counter()
    for s in ref_sets:
        for c in s:
            freq[c] += 1

    # Cards in EVERY reference.
    all_refs = {c for c, n in freq.items() if n == n_refs}
    any_ref = set(freq.keys())

    must_add_lc = (all_refs - user_set) - UNIVERSAL_STAPLES
    consider_lc = ((any_ref - all_refs) - user_set) - UNIVERSAL_STAPLES
    off_meta_lc = (user_set - any_ref) - UNIVERSAL_STAPLES
    shared_lc = user_set & any_ref

    # Track what we dropped — useful for debugging trust in the lists.
    excluded = sorted(
        ((all_refs - user_set) | ((any_ref - all_refs) - user_set) | (user_set - any_ref))
        & UNIVERSAL_STAPLES
    )

    # Map lowercase → display-cased name.
    case_map: dict[str, str] = {}
    for source_list in [user_cards] + [r.main_cards for r in references]:
        for c in source_list:
            if c.lower() not in case_map:
                case_map[c.lower()] = c

    def _build_suggestions(lc_set: set[str]) -> list[CardSuggestion]:
        out = [
            CardSuggestion(
                card=case_map[lc],
                in_n_references=freq[lc],
                total_references=n_refs,
                role=_classify_card_role(case_map[lc]),
            )
            for lc in lc_set if lc in case_map
        ]
        # Sort by frequency desc, then alphabetical.
        out.sort(key=lambda s: (-s.in_n_references, s.card.lower()))
        return out

    return CardDiffReport(
        user_cards=sorted(user_cards),
        must_add=_build_suggestions(must_add_lc),
        consider=_build_suggestions(consider_lc),
        off_meta=sorted(case_map[lc] for lc in off_meta_lc if lc in case_map),
        shared_with_user=sorted(case_map[lc] for lc in shared_lc if lc in case_map),
        excluded_universal_staples=excluded,
    )


# --- Top-level driver ------------------------------------------------------

def run_meta_test(
    user_deck: str,
    bracket: int,
    games_per_pod: int = 5,
    filler_pairs: int = 1,
    extra_urls: Optional[list[str]] = None,
    out_dir: Path = META_OUT_DIR,
    deck_dir: Path = DECK_DIR,
) -> MetaTestReport:
    """Full workflow: fetch references → compare each → diff cards →
    aggregate winner stats → persist."""
    user_path = deck_dir / user_deck if not Path(user_deck).is_absolute() else Path(user_deck)
    if not user_path.exists():
        raise FileNotFoundError(f"user deck not found: {user_path}")

    commanders = _parse_commander_names_from_dck(user_path)
    if not commanders:
        raise ValueError(f"no commanders found in {user_path.name}")
    primary_commander = commanders[0]

    print(f"Fetching reference decks for commander: {primary_commander}", flush=True)
    refs = fetch_reference_decks(
        primary_commander,
        bracket=bracket,
        deck_dir=deck_dir,
        extra_urls=extra_urls,
    )
    if not refs:
        raise RuntimeError(
            f"No references could be fetched for {primary_commander!r}. "
            f"Try passing --reference-url <moxfield-url> manually."
        )
    print(f"  Fetched {len(refs)} reference(s):", flush=True)
    for r in refs:
        print(f"    - {r.source}: {r.deck_filename}", flush=True)

    # Run head-to-head against each reference.
    comparisons: list[dict] = []
    user_w = 0
    user_l = 0
    user_d = 0
    for r in refs:
        print(f"\n--- Comparing user vs {r.source}: {r.name} ---", flush=True)
        cmp_report = compare(
            old_deck=user_deck,
            new_deck=r.deck_filename,
            bracket=bracket,
            games_per_pod=games_per_pod,
            filler_pairs=filler_pairs,
        )
        comparisons.append(cmp_report.to_dict())
        # In compare_versions: old=user, new=reference. So user wins are old_stats.wins.
        user_w += cmp_report.old_stats.wins
        user_l += cmp_report.new_stats.wins
        user_d += cmp_report.draws

    # Diff the user deck against the references.
    user_main = _parse_main_card_names(user_path)
    card_diff = compute_card_diff(user_main, refs)

    report = MetaTestReport(
        user_deck=user_deck,
        bracket=bracket,
        timestamp=datetime.now(timezone.utc).isoformat(),
        references=refs,
        comparisons=comparisons,
        card_diff=card_diff,
        user_record={
            "user_wins": user_w,
            "user_losses": user_l,
            "draws": user_d,
            "total_games": user_w + user_l + user_d,
            "win_rate": user_w / max(1, user_w + user_l) if (user_w + user_l) else 0.0,
        },
    )

    # Persist.
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w-]+", "_", Path(user_deck).stem).strip("_") or "deck"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{stem}_meta_{ts}.json"
    out_path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    print(f"\nWrote meta-test report: {out_path}", flush=True)
    return report


# --- Output formatting -----------------------------------------------------

def format_report_text(report: MetaTestReport) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f" Meta-test — {report.user_deck}")
    lines.append("=" * 60)
    lines.append(f"Bracket: B{report.bracket}")
    lines.append(f"References ({len(report.references)}):")
    for r in report.references:
        bracket_note = ""
        if r.bracket and r.bracket != report.bracket:
            bracket_note = f"  ⚠ BRACKET MISMATCH (ref is B{r.bracket}, user is B{report.bracket})"
        lines.append(f"  - [{r.source}] {r.name}  ({len(r.main_cards)} main cards){bracket_note}")
    lines.append("")
    rec = report.user_record
    lines.append(
        f"Aggregate record across all references: "
        f"{rec['user_wins']}W / {rec['user_losses']}L / {rec['draws']}D "
        f"over {rec['total_games']} games "
        f"(win rate: {rec['win_rate']:.0%} of decisive)"
    )
    lines.append("")
    decisive = rec["user_wins"] + rec["user_losses"]
    if rec["user_wins"] > rec["user_losses"]:
        lines.append("=> Your deck OUTPERFORMED the references. The diff below "
                     "is more 'cards you might add' than 'must-haves'.")
    elif rec["user_wins"] < rec["user_losses"]:
        lines.append("=> The references BEAT your deck. The 'must-add' list "
                     "below is concrete signal — those cards are in both "
                     "winning decks and missing from yours.")
    elif decisive == 0 and rec["draws"] > 0:
        # 0-0-N: neither side could close. Different message than "even".
        lines.append(f"=> NEITHER deck could close ({rec['draws']} draws, no "
                     "decisive games). This is a meta-signal: the archetype "
                     "may struggle to finish at this bracket regardless of build. "
                     "Cards in must-add are still informational, but the "
                     "real takeaway is 'add a finisher / closer'.")
    else:
        lines.append("=> Roughly even. Card diff is informational, not urgent.")
    lines.append("")
    diff = report.card_diff
    n_refs = max((s.total_references for s in diff.must_add + diff.consider), default=0)

    lines.append(f"Must-add (in ALL references, not in user): {len(diff.must_add)}")
    if diff.must_add:
        groups = diff.must_add_by_role()
        for role, suggestions in groups.items():
            lines.append(f"  [{role}]")
            for s in suggestions[:8]:
                # Use the richer frequency_label when refs are >= 2, fall
                # back to the bare confidence string for single-ref runs.
                label = s.frequency_label or s.confidence
                lines.append(f"    + {s.card}  ({label})")
            if len(suggestions) > 8:
                lines.append(f"    ... and {len(suggestions) - 8} more in this role")
    lines.append("")

    lines.append(f"Consider (in some references, not user): {len(diff.consider)}")
    # Sorted by frequency desc, so the highest-coverage "consider" picks come first.
    for s in diff.consider[:15]:
        label = s.frequency_label or s.confidence
        lines.append(f"  ? {s.card}  ({label})")
    if len(diff.consider) > 15:
        lines.append(f"  ... and {len(diff.consider) - 15} more")
    lines.append("")

    lines.append(f"Off-meta (in user, in no references): {len(diff.off_meta)}")
    for c in diff.off_meta[:15]:
        lines.append(f"  ! {c}")
    if len(diff.off_meta) > 15:
        lines.append(f"  ... and {len(diff.off_meta) - 15} more")

    if diff.excluded_universal_staples:
        lines.append("")
        lines.append(
            f"Excluded {len(diff.excluded_universal_staples)} universal "
            f"staples from the diff (Sol Ring / Arcane Signet / basics / etc.) "
            f"because they're noise in either direction."
        )
    return "\n".join(lines)


# --- CLI -------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="commander-meta-test",
        description="Pit your deck against canonical reference builds.",
    )
    p.add_argument("--user", required=True, help="User deck filename.")
    p.add_argument("--bracket", type=int, required=True)
    # Default 2 games while the suggestion engine is being iterated on.
    # Faster shakedown runs (~3-5 min per reference). Bump to 5+ for real
    # signal once the workflow is stable.
    p.add_argument("--games", type=int, default=2,
                   help="Games per comparison (default 2 — fast iteration; "
                        "use 5+ for real signal once workflow is stable).")
    p.add_argument("--filler-pairs", type=int, default=1,
                   help="Filler pair pods per comparison (default 1; "
                        "matches the smaller scale of meta-test vs full compare).")
    p.add_argument("--reference-url", action="append", default=[],
                   help="Manually-supplied reference deck URL. Repeatable.")
    args = p.parse_args(argv)

    report = run_meta_test(
        user_deck=args.user,
        bracket=args.bracket,
        games_per_pod=args.games,
        filler_pairs=args.filler_pairs,
        extra_urls=args.reference_url,
    )
    text = format_report_text(report)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

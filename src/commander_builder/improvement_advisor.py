"""Local improvement advisor — produce swap recommendations without a
browser-Claude session.

The Moxfield audit prompt (`prompts/moxfield_audit_v3.md`) is the gold-standard
proposer, but it requires a browser-equipped Claude session (with
`javascript_tool` and `get_page_text`). This module is the **non-browser
alternative**: it builds a swap recommendation from data we *already pull
locally*:

  - EDHREC commander page via `edhrec_client` (HTML scrape; no Moxfield API)
  - Scryfall card metadata via `scryfall_client`
  - The deck's own performance history from `knowledge_log` + `_matches/`
    (reveal weaknesses like "high draw rate → no finishers")

It then either:
  - Renders a heuristic swap proposal from EDHREC inclusion-% deltas (cheap,
    deterministic, no LLM)
  - OR asks Claude (or local Ollama) to synthesize a swap manifest given the
    diagnosis + EDHREC data + current deck

Output mirrors the `audit_manifest.json` schema, so the result flows directly
into `iteration_loop.run_one_iteration` like a normal audit.

Public API:

    from commander_builder.improvement_advisor import advise

    rec = advise(
        deck_path=Path("[USER] Hakbal of the Surging Soul [B3].dck"),
        bracket=3,
    )
    # rec is a dict matching audit_manifest schema:
    #   { added, removed, rationale, audit_version, ... }

CLI:

    commander-advise --user "[USER] Hakbal of the Surging Soul [B3].dck" --bracket 3
    commander-advise --user "..." --bracket 3 --output advice.json
    commander-advise --user "..." --bracket 3 --source bracket_peers
    commander-advise --user "..." --bracket 3 --source claude
    commander-advise --user "..." --bracket 3 --source claude --claude-model claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .edhrec_client import CardEntry, CommanderPage, fetch_commander_page
from .forge_runner import VENDOR_FORGE
from .moxfield_import import find_top_liked_decks_for_commander
from .scryfall_client import _parse_commander_names_from_dck, lookup_card
from .staples import (
    ROLE_SATURATION_THRESHOLDS,
    classify_role,
    count_deck_roles,
    detect_tribal_type,
    essential_manabase_for_colors,
    is_basic_land,
    is_land,
    is_role_saturated,
    is_universal_staple,
    render_frequency_label,
    tribal_essential_lands,
)

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"
MATCH_DIR = DECK_DIR / "_matches"

# How many candidate adds + cuts to recommend. Roughly matches the audit
# prompt's expected swap-list size for a single-iteration pass.
DEFAULT_ADD_LIMIT = 8
DEFAULT_CUT_LIMIT = 8

# Inclusion% threshold below which a card is unlikely to be a top add.
MIN_INCLUSION_PCT_FOR_ADD = 30.0

# Synergy% threshold for the "high synergy" buckets — these get prioritized
# even if their inclusion is moderate.
MIN_SYNERGY_PCT = 25.0


@dataclass
class DeckDiagnosis:
    """What we know about how this deck has been performing."""
    win_rate: Optional[float] = None
    games_played: int = 0
    draws: int = 0
    draw_rate: float = 0.0
    avg_ending_life: Optional[float] = None
    avg_damage_taken: Optional[float] = None
    fastest_loss_turn: Optional[int] = None
    pattern_summary: str = ""        # human-readable diagnosis
    weakness_signals: list[str] = field(default_factory=list)
    # Roles ranked in descending priority based on weakness signals.
    # Used by the heuristic recommender to re-rank role-tagged adds so
    # the diagnosis steers which bucket surfaces first.
    priority_roles: list[str] = field(default_factory=list)


@dataclass
class SwapRecommendation:
    """One concrete swap suggestion. Multiple SwapRecommendations form the
    full proposal."""
    card: str
    action: str                       # "add" | "cut"
    reason: str
    evidence: dict = field(default_factory=dict)  # inclusion%, synergy%, etc.
    # Hallucination flag for the Claude analyst path: True when the card
    # name resolves in Scryfall, False when Scryfall returns 404
    # (Claude invented it), None when validation was skipped or the
    # lookup raised (network out, cache corruption — don't accuse a
    # real card of being fake just because Scryfall is down).
    name_known: Optional[bool] = None


@dataclass
class AdviceReport:
    deck_filename: str
    deck_id: Optional[str]
    bracket: int
    commander_names: list[str] = field(default_factory=list)
    diagnosis: DeckDiagnosis = field(default_factory=DeckDiagnosis)
    recommendations: list[SwapRecommendation] = field(default_factory=list)
    source: str = "heuristic"         # "heuristic" | "claude" | "ollama" | "bracket_peers"
    timestamp: str = ""
    # When `source` falls back to "heuristic" because a requested LLM
    # backend was unavailable or threw, this captures the user-visible
    # reason. None on the happy path. Populated even on success when
    # the fallback was specifically requested.
    fallback_reason: Optional[str] = None
    # Adds that the recommender produced but the saturation guard
    # dropped (deck already has ≥ ROLE_SATURATION_THRESHOLDS[role]
    # cards in that bucket). Each entry: {card, role, deck_count,
    # threshold}. Surfaced so the UI can show "skipped 3 ramp adds —
    # your deck already has 13 ramp pieces" rather than silently
    # producing a short list.
    skipped_for_saturation: list[dict] = field(default_factory=list)
    # Non-zero only when ``source="claude"`` AND the Claude path
    # successfully fetched bracket-peer references before the LLM
    # call. Lets the UI disclose 'Claude analyst (5 peer refs)' on
    # the source pill so users can tell when the LLM had archetype-
    # specific data vs. just EDHREC averages.
    bracket_peer_ref_count: int = 0

    def to_manifest(self) -> dict:
        """Render as an audit_manifest.json-compatible dict so this feeds
        directly into iteration_loop."""
        added = [r.card for r in self.recommendations if r.action == "add"]
        removed = [r.card for r in self.recommendations if r.action == "cut"]
        rationale_lines = [self.diagnosis.pattern_summary] if self.diagnosis.pattern_summary else []
        if self.diagnosis.weakness_signals:
            rationale_lines.append("Weakness signals: " + "; ".join(self.diagnosis.weakness_signals))
        return {
            "deck_id": self.deck_id,
            "bracket": self.bracket,
            "audit_version": f"advisor-{self.source}",
            "audit_timestamp": self.timestamp or datetime.now(timezone.utc).isoformat(),
            "added": added,
            "removed": removed,
            "rationale": " ".join(rationale_lines),
            "details": {
                "commanders": self.commander_names,
                "diagnosis": asdict(self.diagnosis),
                "recommendations": [asdict(r) for r in self.recommendations],
            },
        }

    def to_dict(self) -> dict:
        return asdict(self)


# --- Diagnosis (read past performance from local data) --------------------

def _aggregate_match_history(deck_filename: str, match_dir: Path = MATCH_DIR) -> DeckDiagnosis:
    """Walk `_matches/<deck>_*.json` reports and aggregate. Empty diagnosis
    when there are no prior matches."""
    diag = DeckDiagnosis()
    if not match_dir.exists():
        return diag

    # Filter by stem-prefix match — the run_match output filename starts with
    # a sanitized version of the deck filename.
    import re
    stem = re.sub(r"[^\w-]+", "_", Path(deck_filename).stem).strip("_")
    reports = sorted(
        match_dir.glob(f"{stem}_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not reports:
        return diag

    # Aggregate across reports.
    total_games = 0
    total_wins = 0
    total_draws = 0
    life_sum = 0.0
    life_count = 0
    damage_sum = 0.0
    damage_count = 0
    fastest_loss: Optional[int] = None
    for path in reports:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        total_games += data.get("games_played", 0)
        total_wins += data.get("user_wins", 0)
        total_draws += data.get("draws", 0)
        if data.get("avg_user_ending_life") is not None:
            life_sum += data["avg_user_ending_life"] * data.get("games_played", 0)
            life_count += data.get("games_played", 0)
        if data.get("avg_user_damage_taken") is not None:
            damage_sum += data["avg_user_damage_taken"] * data.get("games_played", 0)
            damage_count += data.get("games_played", 0)
        if data.get("fastest_loss_turn") is not None:
            cur = fastest_loss
            fastest_loss = (
                data["fastest_loss_turn"] if cur is None
                else min(cur, data["fastest_loss_turn"])
            )

    decisive = total_games - total_draws
    diag.games_played = total_games
    diag.draws = total_draws
    diag.draw_rate = total_draws / total_games if total_games else 0.0
    diag.win_rate = total_wins / decisive if decisive > 0 else 0.0
    diag.avg_ending_life = round(life_sum / life_count, 1) if life_count else None
    diag.avg_damage_taken = round(damage_sum / damage_count, 1) if damage_count else None
    diag.fastest_loss_turn = fastest_loss

    # Heuristic pattern detection.
    signals: list[str] = []
    if diag.draw_rate >= 0.5:
        signals.append(
            f"high draw rate ({diag.draw_rate:.0%}) — deck likely lacks "
            f"a closer / finisher"
        )
    if diag.win_rate is not None and diag.win_rate < 0.15 and decisive >= 5:
        signals.append(
            f"low win rate ({diag.win_rate:.0%}) over {decisive} decisive games"
        )
    if diag.avg_ending_life is not None and diag.avg_ending_life >= 25:
        signals.append("deck survives well; problem is offense, not defense")
    if diag.avg_ending_life is not None and diag.avg_ending_life <= 5:
        signals.append("deck consistently ends low — defense / sustain is weak")
    if diag.fastest_loss_turn is not None and diag.fastest_loss_turn <= 8:
        signals.append(
            f"fastest loss at turn {diag.fastest_loss_turn} — vulnerable to "
            f"early aggression / no T1-T3 interaction"
        )
    # Tag each signal with the role that addresses it. The advisor uses
    # these tags to re-rank role-categorized adds so the diagnosis directly
    # maps to which add bucket gets surfaced first.
    diag.priority_roles = _signals_to_priority_roles(signals)
    diag.weakness_signals = signals

    parts = []
    if diag.win_rate is not None:
        parts.append(f"{total_wins}W/{decisive - total_wins}L/{total_draws}D over {total_games} games (win rate {diag.win_rate:.0%})")
    if signals:
        parts.append("; ".join(signals))
    diag.pattern_summary = ". ".join(parts) if parts else "Insufficient match history."

    return diag


# --- Card-level signals from EDHREC ---------------------------------------

def _read_main_cards(deck_path: Path) -> list[str]:
    """Pull the [Main] section card names (without qty / set / cn)."""
    if not deck_path.exists():
        return []
    import re
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


# Map diagnosis weakness keywords to the role buckets that address them.
# Order in each tuple matters — leftmost role is the strongest match for
# that weakness, used to break ties in priority ranking.
_SIGNAL_TO_ROLES: list[tuple[str, tuple[str, ...]]] = [
    # "no closer / finisher" → bring in finishers, then wipes
    ("closer", ("finisher", "wipe")),
    ("finisher", ("finisher", "wipe")),
    # "low win rate" → assume offense problem; finishers + draw to dig for them
    ("low win rate", ("finisher", "draw", "tutor")),
    # "offense, not defense" → finisher + draw (deck survives, just doesn't close)
    ("offense, not defense", ("finisher", "tutor", "draw")),
    # "defense / sustain is weak" → wipe (clear board) + protection
    ("defense", ("wipe", "protection", "removal")),
    # "early aggression / no T1-T3 interaction" → cheap removal + ramp + protection
    ("early aggression", ("removal", "ramp", "protection")),
    ("T1-T3", ("removal", "ramp", "protection")),
    # "high draw rate" (signal text) → finisher / closer
    ("high draw rate", ("finisher", "wipe", "tutor")),
]


def _signals_to_priority_roles(signals: list[str]) -> list[str]:
    """Translate weakness-signal phrases into a deduplicated, priority-ordered
    role list. The earliest match in each signal contributes the strongest
    role, with later signals adding progressively lower-priority roles.

    Returns at most 4 unique roles. Empty signals → empty list (no
    re-ranking, fall back to default ordering)."""
    out: list[str] = []
    for signal in signals:
        lc = signal.lower()
        for keyword, roles in _SIGNAL_TO_ROLES:
            if keyword in lc:
                for r in roles:
                    if r not in out:
                        out.append(r)
                break
    return out[:4]


def _filter_for_saturation(
    recs: list[SwapRecommendation],
    role_counts: dict,
) -> tuple[list[SwapRecommendation], list[dict]]:
    """Drop add candidates whose role bucket is already saturated in
    the user's deck.

    Real failure mode this addresses (Ur-Dragon B4 audit, 2026-05-13):
    the EDHREC heuristic and bracket-peers source both rank "what
    other decks have a lot of" without checking what the user's deck
    already has. A deck running 13 ramp pieces doesn't need a 14th
    suggested; recommending one would either get applied (replacing
    a stronger non-ramp card) or get balanced out by
    ``_apply_swaps_to_dck``'s adds==cuts rule, wasting a slot.

    Returns ``(kept_recs, skipped_records)``. Each skipped record:
    ``{card, role, deck_count, threshold}``. Cuts are never filtered
    (they're already in the deck — removing a 13th ramp piece IS the
    user's decision). Recs without ``evidence.role`` bucket as
    ``"other"`` which never saturates, so legacy stubs pass through
    untouched.
    """
    kept: list[SwapRecommendation] = []
    skipped: list[dict] = []
    for rec in recs:
        if rec.action != "add":
            kept.append(rec)
            continue
        role = (rec.evidence or {}).get("role", "other") or "other"
        deck_count = int(role_counts.get(role, 0))
        if is_role_saturated(role, deck_count):
            threshold = ROLE_SATURATION_THRESHOLDS.get(role, 0)
            skipped.append({
                "card": rec.card,
                "role": role,
                "deck_count": deck_count,
                "threshold": threshold,
            })
            continue
        kept.append(rec)
    return kept, skipped


def _missing_manabase_recommendations(
    deck_cards,
    color_identity,
    tribe: Optional[str] = None,
    budget: bool = False,
) -> list[SwapRecommendation]:
    """Curated manabase-essentials safety net.

    User feedback (2026-05-13): "tribal decks should have cavern of
    souls. All decks should have dual lands and bond lands and fetch
    lands." The heuristic + bracket_peers paths only surface lands
    when they happen to appear in references/EDHREC. This helper
    runs alongside those paths to deterministically recommend any
    color-identity-appropriate ABU dual / fetch / shock / bond land
    that the deck doesn't already own.

    ``deck_cards`` is the set of card names currently in the deck.
    ``color_identity`` is a set/iterable of WUBRG letters (case-
    insensitive). Empty/colorless identity → no color-gated lands
    (but tribal essentials still surface if ``tribe`` is set).

    ``tribe`` (optional) is the deck's primary creature type as
    detected from the commander's oracle text. When set, appends
    Cavern of Souls + Path of Ancestry + Secluded Courtyard +
    Unclaimed Territory — colorless lands every tribal deck wants
    regardless of color identity.

    Each rec carries ``evidence.role="land"`` so it groups cleanly
    in the UI. Source identifies which arm produced it:
    ``manabase_essentials`` for color-gated, ``tribal_essentials``
    for the tribe-utility set.
    """
    essentials = essential_manabase_for_colors(color_identity, budget=budget)
    tribal = tribal_essential_lands(tribe)
    if not essentials and not tribal:
        return []
    deck_lc = {c.lower() for c in deck_cards}
    recs: list[SwapRecommendation] = []
    for name in essentials:
        if name.lower() in deck_lc:
            continue
        recs.append(SwapRecommendation(
            card=name,
            action="add",
            reason=(
                "manabase essential — high-impact land for this "
                "color identity (dual / fetch / shock / bond)"
            ),
            evidence={
                "source": "manabase_essentials",
                "role": "land",
            },
        ))
    for name in tribal:
        if name.lower() in deck_lc:
            continue
        recs.append(SwapRecommendation(
            card=name,
            action="add",
            reason=(
                f"tribal essential — colorless utility land for "
                f"{tribe} decks (uncounterable / mana of any color)"
            ),
            evidence={
                "source": "tribal_essentials",
                "role": "land",
                "tribe": tribe,
            },
        ))
    return recs


def _validate_card_names(recs: list[SwapRecommendation]) -> None:
    """Mutate each rec's ``name_known`` flag based on Scryfall lookup.

    Defense against Claude analyst hallucinations: when the LLM invents a
    plausible-sounding card name (e.g. "Accursed Marauder"), the audit
    pipeline would otherwise pass it down to Forge, which then rejects
    the deck silently. Cross-checking against the Scryfall cache catches
    it early so the UI can mark the recommendation with a warning pill.

    Three terminal states for each rec:

    - ``True``  — Scryfall returned a card dict; the name is real.
    - ``False`` — Scryfall returned ``None`` (HTTP 404); the name is fake.
    - ``None``  — lookup raised (network, cache corruption); we couldn't
      check. **Never** flag a legitimate card as fake on transient
      failure.

    Heuristic recs come from EDHREC and should always resolve; running
    them through the validator is cheap (cache hit) and uniform so
    callers don't need to special-case the source.
    """
    for rec in recs:
        try:
            card = lookup_card(rec.card)
        except Exception:
            rec.name_known = None
            continue
        rec.name_known = card is not None


def _role_for_card(card_name: str) -> str:
    """Look up ``card_name`` via Scryfall (cached) and classify its role.

    Returns ``"unknown"`` on Scryfall miss or offline. The role tag is
    advisory — it groups recommendations on the advice surface but doesn't
    drive program logic, so a soft failure is fine."""
    try:
        card = lookup_card(card_name)
    except Exception:
        return "unknown"
    if not card:
        return "unknown"
    return classify_role(card.get("oracle_text", ""), card.get("type_line", ""))


def _heuristic_swap_recommendations(
    deck_cards: set[str],
    edhrec_page: CommanderPage,
    add_limit: int = DEFAULT_ADD_LIMIT,
    cut_limit: int = DEFAULT_CUT_LIMIT,
    diagnosis: Optional[DeckDiagnosis] = None,
) -> list[SwapRecommendation]:
    """Pure-data swap proposals from EDHREC inclusion-% deltas.

    Adds: cards EDHREC ranks high (top_cards or high_synergy) that are NOT
    already in the deck. Cuts: cards in the deck that AREN'T in EDHREC's
    top-cards list (likely off-archetype). No LLM, no card-text reasoning —
    just statistical co-inclusion.

    If ``edhrec_page`` is ``None`` (commander missing from EDHREC, network
    blip, slug mismatch), returns an empty list rather than crashing the
    audit. The caller still produces a valid AdviceReport with zero swaps,
    which the UI surfaces as "no audit suggestions available."
    """
    if edhrec_page is None:
        return []
    recs: list[SwapRecommendation] = []
    deck_cards_lc = {c.lower() for c in deck_cards}

    # Adds — pull from high-synergy first (commander-specific signal), then
    # top cards (color staples).
    candidates_for_add: list[CardEntry] = []
    seen: set[str] = set()
    for c in edhrec_page.high_synergy_cards:
        if c.synergy_pct >= MIN_SYNERGY_PCT and c.name.lower() not in seen:
            candidates_for_add.append(c)
            seen.add(c.name.lower())
    for c in edhrec_page.top_cards:
        if c.inclusion_pct >= MIN_INCLUSION_PCT_FOR_ADD and c.name.lower() not in seen:
            candidates_for_add.append(c)
            seen.add(c.name.lower())

    # Build the full add-recommendation list first, then re-rank.
    add_recs: list[SwapRecommendation] = []
    for c in candidates_for_add:
        if c.name.lower() in deck_cards_lc:
            continue
        # Skip universal staples — they're noise in the must-add list. Every
        # deck already has Sol Ring; if it doesn't, that's an intentional choice.
        if is_universal_staple(c.name):
            continue
        bucket = "high_synergy" if c.synergy_pct >= MIN_SYNERGY_PCT else "top_cards"
        # Categorize the recommendation by role so the advice surface can
        # group adds by ramp/draw/removal/finisher rather than show a flat list.
        role = _role_for_card(c.name)
        # `inclusion_pct` from EDHREC is actually a raw deck count
        # (e.g. 30627 — "this card appears in 30627 decks"), not a
        # percentage. Render it as a count so the rationale doesn't
        # read "in 30627% of decks".
        # If the value is small (≤100) we treat it as a real
        # percentage; otherwise format as a deck count.
        inclusion_phrase = (
            f"{c.inclusion_pct:.0f}% of decks"
            if 0 < c.inclusion_pct <= 100
            else f"{int(c.inclusion_pct):,} decks"
        )
        add_recs.append(SwapRecommendation(
            card=c.name,
            action="add",
            reason=(
                f"EDHREC {bucket}: in {inclusion_phrase}"
                + (f", synergy {c.synergy_pct:.0f}%" if c.synergy_pct else "")
            ),
            evidence={
                "inclusion_pct": c.inclusion_pct,
                "synergy_pct": c.synergy_pct,
                "source": f"edhrec.{bucket}",
                "role": role,
            },
        ))

    # Re-rank by diagnosis priority roles, when present. Adds in the
    # priority-role list float to the top in their listed order; everything
    # else keeps its original (synergy-then-top) ordering. Stable sort
    # preserves intra-bucket order.
    if diagnosis and diagnosis.priority_roles:
        priority_index = {r: i for i, r in enumerate(diagnosis.priority_roles)}
        def _rank(r: SwapRecommendation) -> int:
            role = r.evidence.get("role", "unknown")
            return priority_index.get(role, len(priority_index) + 1)
        add_recs.sort(key=_rank)

    # Apply the add_limit after re-ranking so the surfaced top-N reflects
    # the re-ordered list, not the pre-ranked one.
    recs.extend(add_recs[:add_limit])

    # Cuts — cards in deck not in EDHREC's top-cards or high-synergy lists.
    # Inverse of the adds path: if the rest of the meta isn't running this,
    # it's probably off-archetype. Conservative — top cards by EDHREC are
    # color staples that not all decks need.
    edhrec_known = {c.name.lower() for c in edhrec_page.top_cards} \
                 | {c.name.lower() for c in edhrec_page.high_synergy_cards}

    for card in deck_cards:
        # Don't recommend cutting any land (basic, dual, fetch, shock,
        # MDFC, utility) or universal staples. The manabase is a
        # deliberate construction; a missing reference doesn't mean
        # the user should pull a $200 ABU dual.
        if is_land(card) or is_universal_staple(card):
            continue
        if card.lower() not in edhrec_known:
            recs.append(SwapRecommendation(
                card=card,
                action="cut",
                reason="not in EDHREC's top-cards or high-synergy lists for this commander",
                evidence={"source": "edhrec.absence"},
            ))
            if sum(1 for r in recs if r.action == "cut") >= cut_limit:
                break

    return recs


# --- Bracket-peers recommender (sources from other tuned builds) ----------

# How many reference decks to pull when sourcing from Moxfield's
# top-liked decks at the user's bracket. Five is the sweet spot:
# enough that frequency math has signal ("in 5/5 references" reads
# as 'unanimous'), not so many that one cluster of similar builds
# dominates. Configurable via the public function signature.
DEFAULT_BRACKET_PEERS_N = 5


def _peer_card_frequency(decks):
    """Build per-card reference-frequency across a list of Moxfield
    deck JSONs.

    Returns ``(frequency_counter, case_map)`` where:
      - ``frequency_counter`` maps lowercased card name → number of
        references that contain it. **Each deck contributes at most
        once per card** (so basic lands present 30× in a deck don't
        inflate the count — what we want is the "shows up in N of
        the M references" signal).
      - ``case_map`` maps lowercased name → display-cased name
        (first-seen wins, mirrors how source decks render the card).

    Extracted from ``_bracket_peers_recommendations`` and
    ``_collect_bracket_peer_summary_for_prompt`` which previously
    duplicated this logic. The duplication had a subtle divergence:
    the prompt-summary path counted Forest once per occurrence
    instead of once per deck, inflating the frequency of basics in
    the Claude prompt. The unified helper set-ifies per deck so the
    same correct semantics apply everywhere.
    """
    from collections import Counter
    freq: Counter = Counter()
    case_map: dict[str, str] = {}
    for deck in decks:
        cards = _extract_main_cards_from_moxfield_json(deck)
        seen_this_deck: set[str] = set()
        for c in cards:
            lc = c.lower()
            if lc not in case_map:
                case_map[lc] = c
            if lc in seen_this_deck:
                continue
            seen_this_deck.add(lc)
            freq[lc] += 1
    return freq, case_map


def _extract_main_cards_from_moxfield_json(deck_json: dict) -> list[str]:
    """Pull the mainboard card names out of a Moxfield deck JSON.

    Moxfield's response shape is ``boards.mainboard.cards`` keyed by
    internal card UUIDs, each value an object whose ``card.name`` field
    is the canonical card name. We don't care about quantity here — each
    name counts once (multi-copies aren't a thing in singleton
    Commander anyway, except for basics, which the staples filter
    drops).
    """
    boards = deck_json.get("boards") or {}
    mainboard = (boards.get("mainboard") or {}).get("cards") or {}
    out: list[str] = []
    for entry in mainboard.values():
        if not isinstance(entry, dict):
            continue
        card = entry.get("card") or {}
        name = (card.get("name") or "").strip()
        if name:
            out.append(name)
    return out


def _default_min_refs(total_refs: int) -> int:
    """Default frequency floor for bracket-peers adds.

    Singletons (cards in 1 of N references) are noise — they reflect
    one builder's idiosyncrasy, not a tuned-archetype consensus. Default
    to "at least majority of references, but never less than 2" so a
    small reference set (N=2 or 3) still produces some recommendations.
    """
    import math
    return max(2, math.ceil(total_refs / 2))


def _bracket_peers_recommendations(
    commander_name: str,
    bracket: int,
    deck_cards: set[str],
    n: int = DEFAULT_BRACKET_PEERS_N,
    add_limit: int = DEFAULT_ADD_LIMIT,
    cut_limit: int = DEFAULT_CUT_LIMIT,
    min_refs: Optional[int] = None,
    diagnosis: Optional["DeckDiagnosis"] = None,
) -> tuple[list[SwapRecommendation], int]:
    """Source swap recommendations from the top-N highest-liked Moxfield
    decks for ``commander_name`` at ``bracket``.

    The Ur-Dragon B4 audit (2026-05-13) surfaced why this exists: EDHREC's
    commander page averages inclusion% across all brackets and includes
    precons, so it recommended generic ramp for a deck that was already
    swimming in ramp and cut archetype-specific tools (Moat in a
    flying-tribal deck, Last March of the Ents as the deck's card draw).
    Sourcing from other tuned builds at the same bracket produces
    archetype-appropriate suggestions by construction — what 5 other
    people who've tuned this commander at this bracket consider
    essential.

    Returns ``(recommendations, ref_count)``. Empty list + ``0`` when no
    references could be fetched (caller falls back to a sparser source).

    Frequency thresholds:
      - **Adds** include cards present in any reference but missing
        from the user's deck. Each rec carries ``in_n_references`` so
        callers rank by confidence; the reason string already names the
        ratio.
      - **Cuts** are user-deck cards absent from every reference.
        Universal staples and basic lands are excluded from both
        directions (they're noise in either).
    """
    decks = find_top_liked_decks_for_commander(
        commander_name, bracket=bracket, n=n,
    )
    if not decks:
        return [], 0

    # Shared frequency helper — counts each deck at most once per
    # card, so basics don't inflate the signal. case_map preserves
    # first-seen capitalization.
    freq, case_map = _peer_card_frequency(decks)
    total_refs = len(decks)
    deck_cards_lc = {c.lower() for c in deck_cards}
    # Extend case_map with the user's own cards so cuts can render the
    # user's casing (the peer-cardlists may not include cards that are
    # in the user's deck and absent from every reference).
    for c in deck_cards:
        lc = c.lower()
        if lc not in case_map:
            case_map[lc] = c

    # Drop singletons (cards in only 1 of N references) — they're one
    # builder's quirk, not a tuned-archetype consensus. Default floor is
    # majority-of-references (never below 2). Phase A gap #3.
    effective_min_refs = (
        min_refs if min_refs is not None else _default_min_refs(total_refs)
    )

    # Adds: any card appearing in ≥ effective_min_refs references,
    # missing from user, not a universal staple. Sort by frequency
    # desc, then alphabetical.
    add_candidates_lc = [
        lc for lc in freq
        if lc not in deck_cards_lc
        and freq[lc] >= effective_min_refs
        and not is_universal_staple(case_map[lc])
        and not is_basic_land(case_map[lc])
    ]
    add_candidates_lc.sort(
        key=lambda lc: (-freq[lc], case_map[lc].lower()),
    )

    add_recs: list[SwapRecommendation] = []
    for lc in add_candidates_lc:
        name = case_map[lc]
        n_refs = freq[lc]
        role = _role_for_card(name)
        label = render_frequency_label(n_refs, total_refs)
        add_recs.append(SwapRecommendation(
            card=name,
            action="add",
            reason=(
                f"in {n_refs}/{total_refs} reference decks "
                f"({label}) for {commander_name} at B{bracket}"
            ),
            evidence={
                "in_n_references": n_refs,
                "total_references": total_refs,
                "frequency_label": label,
                "role": role,
                "source": "bracket_peers",
            },
        ))

    # Phase A gap #5: re-rank by diagnosis priority roles when
    # available, matching the heuristic path's behavior. If the deck's
    # weakness signals say "no closer / high draw rate", finisher-tagged
    # adds float to the top regardless of which reference they came
    # from. Stable sort preserves frequency-desc within each priority
    # bucket.
    if diagnosis and getattr(diagnosis, "priority_roles", None):
        priority_index = {r: i for i, r in enumerate(diagnosis.priority_roles)}
        def _rank(rec: SwapRecommendation) -> int:
            role_str = (rec.evidence or {}).get("role", "unknown")
            return priority_index.get(role_str, len(priority_index) + 1)
        add_recs.sort(key=_rank)

    recs: list[SwapRecommendation] = list(add_recs[:add_limit])

    # Cuts: user cards absent from every reference, with the universal-
    # staples filter applied so we don't recommend cutting Sol Ring.
    any_ref_lc = set(freq.keys())
    cut_candidates = [
        case_map[lc] for lc in (deck_cards_lc - any_ref_lc)
        if lc in case_map
        and not is_universal_staple(case_map[lc])
        # Skip ALL lands (basic + nonbasic + fetch + shock + MDFC).
        # Regression caught 2026-05-13: bracket_peers recommended
        # cutting Savannah from a 5-color Ur-Dragon deck because the
        # top-5 references happened to use different specific duals.
        # Manabase decisions are deliberate, not auto-recommended.
        and not is_land(case_map[lc])
    ]
    cut_candidates.sort(key=str.lower)
    for name in cut_candidates[:cut_limit]:
        recs.append(SwapRecommendation(
            card=name,
            action="cut",
            reason=(
                f"absent from all {total_refs} reference decks for "
                f"{commander_name} at B{bracket}"
            ),
            evidence={
                "in_n_references": 0,
                "total_references": total_refs,
                "source": "bracket_peers",
            },
        ))

    return recs, total_refs


def _collect_bracket_peer_summary_for_prompt(
    commander_name: str,
    bracket: int,
    n: int = DEFAULT_BRACKET_PEERS_N,
) -> Optional[dict]:
    """Pull top-N bracket-matched references and produce a compact
    frequency summary suitable for inclusion in the Claude prompt.

    Sourced from ``find_top_liked_decks_for_commander`` (same fetcher
    powering the standalone bracket_peers source). The summary shape
    is designed for LLM consumption — minimal metadata + a sorted
    frequency table — so Claude can reason about "this card is in
    5/5 references but missing from this deck" at a glance, without
    parsing full decklists.

    Returns ``None`` when no references could be fetched (caller's
    Claude prompt falls back to EDHREC-only context).
    """
    decks = find_top_liked_decks_for_commander(
        commander_name, bracket=bracket, n=n,
    )
    if not decks:
        return None

    # Shared frequency helper — same semantics as the standalone
    # bracket_peers source. Each deck counts at most once per card,
    # which keeps the in_n_refs count meaningful: "Moat in 5/5
    # references" rather than "Forest in 150 occurrences."
    freq, case_map = _peer_card_frequency(decks)

    # Top-100 most frequent cards is a generous cap — keeps the
    # prompt compact while covering virtually every relevant card
    # across 5 references.
    sorted_lc = sorted(freq, key=lambda lc: (-freq[lc], case_map[lc].lower()))
    cards_by_frequency = [
        {"name": case_map[lc], "in_n_refs": freq[lc]}
        for lc in sorted_lc[:100]
    ]
    ref_metadata = [
        {"public_id": d.get("publicId"), "name": d.get("name", "")}
        for d in decks
    ]
    return {
        "ref_count": len(decks),
        "ref_metadata": ref_metadata,
        "cards_by_frequency": cards_by_frequency,
    }


# --- LLM-aided variant (Claude) -------------------------------------------

_CLAUDE_ADVISOR_SYSTEM = """You are a deck-tuning advisor for Magic: the Gathering Commander. \
Given:
  - The deck's commander(s)
  - The current decklist
  - Performance signals from prior simulations (win rate, draw rate, fastest loss, etc.)
  - EDHREC inclusion% / synergy% data for this commander (aggregate across all brackets)
  - When available: `bracket_peer_references` — top-N highest-liked
    Moxfield decks for the same commander **at the same bracket**.
    These are tuned builds. Their card overlap is the strongest
    signal for what belongs in a deck at this power level.

Think in two passes:

1. **Adds — what's missing?** Scan `bracket_peer_references.cards_by_frequency`.
   Cards appearing in a majority of the references but NOT in the
   `current_decklist` are the strongest add candidates. EDHREC data
   is a fallback when peer references aren't available or the peer
   set is small. Don't recommend universal staples (Sol Ring, Arcane
   Signet, Command Tower, basic lands) — they're either already
   present or deliberately excluded.

2. **Cuts — what's not pulling weight?** Cards in `current_decklist`
   that don't appear in any peer reference AND aren't role-essential
   are candidates. **NEVER cut any land** (basic, dual, fetch,
   shock, MDFC, utility) — manabase decisions are deliberate.
   **NEVER cut universal staples**. Cut card-disadvantage pieces or
   off-archetype slots first.

Return JSON ONLY (no prose, no markdown):

{
  "rationale": "one paragraph diagnosing the weakness and explaining the swap strategy, citing references by name when relevant",
  "added": ["Card A", "Card B", ...],
  "removed": ["Card X", "Card Y", ...]
}

Constraints:
- Recommend 4-8 adds and 4-8 removes (the deck must stay at 99 cards in the [Main]).
- Each `added` card SHOULD appear in `bracket_peer_references` when present, OR in EDHREC's data; rare deviations need strong synergy reason in the rationale.
- Each `removed` card MUST be in the current decklist.
- **Don't recommend cutting ANY land** (basic, dual, fetch, shock, MDFC, utility).
- Don't recommend cutting Sol Ring, Arcane Signet, Command Tower, or other universal staples.
- Match adds and removes 1-for-1 by count.
- Focus the rationale on the weakness signals + reference-deck consensus, not generic deck-building advice.
"""


DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"


def _claude_swap_recommendations(
    deck_filename: str,
    bracket: int,
    deck_cards: set[str],
    diagnosis: DeckDiagnosis,
    edhrec_page: CommanderPage,
    model: str = DEFAULT_CLAUDE_MODEL,
    bracket_peers_summary: Optional[dict] = None,
) -> tuple[list[SwapRecommendation], str]:
    """LLM-aided variant. Falls back via NotImplementedError when no API key
    or SDK — caller catches and degrades to heuristic.

    ``model`` lets callers pick a tier (haiku for cheap, sonnet for default
    quality, opus for deepest reasoning). The default tracks
    ``DEFAULT_CLAUDE_MODEL`` so existing callers don't change behavior;
    cost-sensitive runs can pass ``model='claude-haiku-4-5'`` for a
    ~3-5x cheaper request.

    ``bracket_peers_summary`` (optional) carries a compact
    frequency-keyed view of the top-N highest-liked Moxfield decks
    for this commander at this bracket. When present, Claude is
    instructed to prioritize adds from cards in the majority of peer
    references that are missing from the user's deck — strongest
    archetype-specific signal available. Add ~3-4K input tokens to
    the request (~$0.01 extra on Sonnet) but produces materially
    better recommendations than EDHREC-only context.
    """
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise NotImplementedError("claude advisor requires ANTHROPIC_API_KEY")
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise NotImplementedError("claude advisor requires `pip install anthropic`") from exc

    # Compact the EDHREC payload — full pages are too large.
    edhrec_compact = {
        "commander": edhrec_page.commander_name,
        "deck_count": edhrec_page.deck_count,
        "top_cards": [
            {"name": c.name, "inclusion_pct": c.inclusion_pct, "synergy_pct": c.synergy_pct}
            for c in edhrec_page.top_cards[:50]
        ],
        "high_synergy_cards": [
            {"name": c.name, "inclusion_pct": c.inclusion_pct, "synergy_pct": c.synergy_pct}
            for c in edhrec_page.high_synergy_cards[:30]
        ],
    }
    user_payload = {
        "deck_filename": deck_filename,
        "bracket": bracket,
        "current_decklist": sorted(deck_cards),
        "performance": asdict(diagnosis),
        "edhrec_signals": edhrec_compact,
    }
    # Include bracket-peer references only when actually available.
    # Omitting the key entirely (rather than sending an empty/null
    # value) keeps the prompt smaller for obscure commanders where
    # no references could be fetched.
    if bracket_peers_summary:
        user_payload["bracket_peer_references"] = bracket_peers_summary
    user_message = json.dumps(user_payload, indent=2)

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=_CLAUDE_ADVISOR_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text
    if not text.strip():
        raise RuntimeError("claude advisor: empty response")

    # Tolerate code-fence wrapping despite the instruction.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if "```" in cleaned else cleaned
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    parsed = json.loads(cleaned)
    rationale = str(parsed.get("rationale", ""))
    recs: list[SwapRecommendation] = []
    for name in parsed.get("added", []) or []:
        recs.append(SwapRecommendation(
            card=str(name), action="add",
            reason="recommended by Claude advisor", evidence={"source": "claude"},
        ))
    for name in parsed.get("removed", []) or []:
        recs.append(SwapRecommendation(
            card=str(name), action="cut",
            reason="recommended by Claude advisor", evidence={"source": "claude"},
        ))
    return recs, rationale


# --- Public entry ----------------------------------------------------------

def advise(
    deck_path: Path,
    bracket: int,
    use_claude: bool = False,
    source: Optional[str] = None,
    deck_dir: Path = DECK_DIR,
    match_dir: Path = MATCH_DIR,
    claude_model: str = DEFAULT_CLAUDE_MODEL,
    budget: bool = False,
) -> AdviceReport:
    """Generate swap recommendations for one deck.

    ``source`` selects the recommendation backend:

    - ``"heuristic"`` (default when neither ``use_claude`` nor
      ``source`` is set) — EDHREC inclusion% / synergy% over the
      commander's aggregate page. Fast, deterministic, no LLM token
      cost. Worst on tuned high-bracket decks because EDHREC averages
      across all brackets.
    - ``"bracket_peers"`` — top-N highest-liked Moxfield decks for
      this commander **at this bracket**, frequency-ranked. Stays
      archetype-appropriate because the source decks are by definition
      tuned for the same goal. Falls back to heuristic when no
      references can be fetched.
    - ``"claude"`` (or legacy ``use_claude=True``) — LLM-aided
      synthesis. Most expressive; requires ``ANTHROPIC_API_KEY``.

    ``claude_model`` selects the Anthropic tier when source is
    ``"claude"``; defaults to Sonnet. Pass ``"claude-haiku-4-5"`` to
    cut cost ~3-5x on routine audits.

    ``use_claude=True`` is preserved as a legacy alias for
    ``source="claude"`` so existing callers don't break.
    """
    # Resolve the effective backend. Explicit ``source`` wins; otherwise
    # ``use_claude=True`` maps to claude; otherwise heuristic.
    if source is None:
        source = "claude" if use_claude else "heuristic"
    if source not in ("heuristic", "claude", "bracket_peers"):
        raise ValueError(
            f"source must be one of heuristic/claude/bracket_peers, "
            f"got {source!r}",
        )

    if not deck_path.is_absolute():
        deck_path = deck_dir / deck_path
    if not deck_path.exists():
        raise FileNotFoundError(f"deck not found: {deck_path}")

    # Resolve commander names + EDHREC page. We always fetch the page
    # because heuristic and claude both consume it, AND because
    # bracket_peers may need it as a fallback when no Moxfield
    # references are returned.
    commanders = _parse_commander_names_from_dck(deck_path)
    if not commanders:
        raise ValueError(f"no commanders found in {deck_path.name}")
    primary_commander = commanders[0]

    # Build diagnosis from prior matches.
    diagnosis = _aggregate_match_history(deck_path.name, match_dir=match_dir)

    # Pull current cards.
    main_cards = set(_read_main_cards(deck_path))

    # Pick backend.
    rationale_override: Optional[str] = None
    fallback_reason: Optional[str] = None
    edhrec_page: Optional[CommanderPage] = None
    bracket_peer_ref_count: int = 0  # set by claude path when refs shipped
    recs: list[SwapRecommendation]

    def _fetch_edhrec_lazy() -> Optional[CommanderPage]:
        """Lazy-fetch EDHREC only when a backend that needs it actually
        runs. bracket_peers avoids the round-trip on the happy path."""
        nonlocal edhrec_page
        if edhrec_page is None:
            edhrec_page = fetch_commander_page(primary_commander)
        return edhrec_page

    if source == "bracket_peers":
        peer_recs, ref_count = _bracket_peers_recommendations(
            commander_name=primary_commander,
            bracket=bracket,
            deck_cards=main_cards,
            diagnosis=diagnosis,
        )
        if peer_recs:
            recs = peer_recs
            # source stays "bracket_peers"
        else:
            # No references — fall back to heuristic so we still emit
            # something useful. Surface the cause so the UI can show
            # the user why the better backend wasn't used.
            fallback_reason = (
                f"no bracket-peer references found for "
                f"{primary_commander!r} at B{bracket} — "
                f"falling back to EDHREC heuristic"
            )
            print(f"  WARN: {fallback_reason}.", flush=True)
            page = _fetch_edhrec_lazy()
            recs = _heuristic_swap_recommendations(
                main_cards, page, diagnosis=diagnosis,
            )
            source = "heuristic"
    elif source == "claude":
        page = _fetch_edhrec_lazy()
        # Enrich Claude's context with top-N bracket-peer references.
        # Best-effort — when Moxfield can't return references (obscure
        # commander, network blip), Claude falls back to EDHREC-only
        # context. The peer data is what lets the LLM reason about
        # "what does this deck have vs. tuned same-bracket peers?"
        # rather than just "what does EDHREC's all-bracket average
        # say?" — the same fix that drove the standalone bracket_peers
        # source.
        try:
            peer_summary = _collect_bracket_peer_summary_for_prompt(
                primary_commander, bracket=bracket,
                n=DEFAULT_BRACKET_PEERS_N,
            )
        except Exception:  # noqa: BLE001
            peer_summary = None
        try:
            recs, rationale_override = _claude_swap_recommendations(
                deck_path.name, bracket, main_cards, diagnosis, page,
                model=claude_model,
                bracket_peers_summary=peer_summary,
            )
            # Record how many peer refs actually shipped to Claude so
            # the UI can disclose 'Claude analyst (N peer refs)' on
            # the source pill. Only set when the LLM call succeeded —
            # a fallback to heuristic shouldn't claim peer enrichment.
            if peer_summary:
                bracket_peer_ref_count = int(
                    peer_summary.get("ref_count", 0) or 0,
                )
            # source stays "claude"
        except NotImplementedError as exc:
            fallback_reason = f"claude advisor unavailable: {exc}"
            print(f"  WARN: {fallback_reason}; falling back to heuristic.",
                  flush=True)
            recs = _heuristic_swap_recommendations(
                main_cards, page, diagnosis=diagnosis,
            )
            source = "heuristic"
        except Exception as exc:  # noqa: BLE001
            # Concrete cause helps diagnose: AuthenticationError (bad
            # key), APIConnectionError (network), JSONDecodeError
            # (model returned non-JSON), etc.
            fallback_reason = (
                f"claude advisor failed ({type(exc).__name__}: {exc})"
            )
            print(f"  WARN: {fallback_reason}; falling back to heuristic.",
                  flush=True)
            recs = _heuristic_swap_recommendations(
                main_cards, page, diagnosis=diagnosis,
            )
            source = "heuristic"
    else:
        page = _fetch_edhrec_lazy()
        recs = _heuristic_swap_recommendations(main_cards, page)

    # Resolve deck_id from the .dck Moxfield= line if present.
    deck_id: Optional[str] = None
    try:
        text = deck_path.read_text(encoding="utf-8")
        import re
        m = re.search(r"^Moxfield=(.+)$", text, re.MULTILINE)
        if m:
            deck_id = m.group(1).strip()
    except OSError:
        pass

    # Curated manabase safety net — prepend any color-identity-
    # appropriate ABU dual / fetch / shock / bond land the user is
    # missing. User feedback (2026-05-13): "all decks should have
    # dual lands and bond lands and fetch lands." The source-specific
    # paths (heuristic / bracket_peers / claude) recommend lands only
    # when they happen to appear in references; this fills the gap
    # deterministically. Best-effort: a failed commander lookup falls
    # through silently — the existing recs are unaffected.
    #
    # Tribal lands (Cavern of Souls, Path of Ancestry, etc.) layer
    # on top when detect_tribal_type identifies a primary creature
    # type in the commander's oracle (e.g. The Ur-Dragon → "Dragon").
    try:
        commander_card = lookup_card(primary_commander)
        if commander_card:
            ci = commander_card.get("color_identity") or []
            ci_set = {c.upper() for c in ci if isinstance(c, str)}
            tribe = detect_tribal_type(
                commander_card.get("oracle_text", "") or "",
                commander_card.get("type_line", "") or "",
            )
            manabase_recs = _missing_manabase_recommendations(
                main_cards, ci_set, tribe=tribe, budget=budget,
            )
            # Prepend rather than append so manabase upgrades surface
            # at the top of the rec list — they're foundational, not
            # speculative.
            recs = list(manabase_recs) + list(recs)
    except Exception:  # noqa: BLE001
        # Commander lookup failure shouldn't break the audit.
        pass

    # Drop add recommendations whose role bucket is already saturated
    # in the user's deck. The Ur-Dragon B4 audit (2026-05-13) lost an
    # A/B sim partly because the advisor recommended 5 more ramp
    # pieces to a deck already running 13. ``count_deck_roles`` is
    # disk-cached behind ``lookup_card`` so this round-trip is
    # near-free on repeat audits of the same deck.
    role_counts = count_deck_roles(main_cards)
    recs, skipped_for_saturation = _filter_for_saturation(recs, role_counts)

    # Validate every recommended card name against Scryfall. Catches
    # Claude hallucinations before Forge silently rejects the deck.
    # Heuristic recs come from EDHREC and should all resolve; cache hits
    # make this near-free.
    _validate_card_names(recs)

    report = AdviceReport(
        deck_filename=deck_path.name,
        deck_id=deck_id,
        bracket=bracket,
        commander_names=commanders,
        diagnosis=diagnosis,
        recommendations=recs,
        source=source,
        timestamp=datetime.now(timezone.utc).isoformat(),
        fallback_reason=fallback_reason,
        skipped_for_saturation=skipped_for_saturation,
        bracket_peer_ref_count=bracket_peer_ref_count,
    )
    if rationale_override:
        report.diagnosis.pattern_summary = rationale_override
    return report


# --- CLI -------------------------------------------------------------------

def _format_report_text(report: AdviceReport) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f" Improvement advice — {report.deck_filename}")
    lines.append("=" * 60)
    lines.append(f"Bracket: B{report.bracket}")
    lines.append(f"Commander(s): {', '.join(report.commander_names)}")
    lines.append(f"Source: {report.source}")
    lines.append("")
    lines.append("Diagnosis:")
    lines.append(f"  {report.diagnosis.pattern_summary or '(no signal)'}")
    if report.diagnosis.weakness_signals:
        lines.append("  Signals:")
        for s in report.diagnosis.weakness_signals:
            lines.append(f"    - {s}")
    if report.diagnosis.priority_roles:
        lines.append(f"  Priority roles (re-ranked adds): "
                     f"{' > '.join(report.diagnosis.priority_roles)}")
    lines.append("")
    adds = [r for r in report.recommendations if r.action == "add"]
    cuts = [r for r in report.recommendations if r.action == "cut"]
    lines.append(f"Recommended adds ({len(adds)}):")
    # Group adds by role for readability — diagnosis-prioritized roles
    # appear first, then everything else in default order.
    from collections import defaultdict
    by_role: dict[str, list[SwapRecommendation]] = defaultdict(list)
    for r in adds:
        role = r.evidence.get("role", "unknown") if r.evidence else "unknown"
        by_role[role].append(r)
    role_order = (
        list(report.diagnosis.priority_roles)
        + [r for r in by_role if r not in report.diagnosis.priority_roles]
    )
    for role in role_order:
        if not by_role.get(role):
            continue
        marker = "★" if role in report.diagnosis.priority_roles else " "
        lines.append(f"  {marker} [{role}]")
        for r in by_role[role]:
            lines.append(f"      + {r.card}  ({r.reason})")
    lines.append("")
    lines.append(f"Recommended cuts ({len(cuts)}):")
    for r in cuts:
        lines.append(f"  - {r.card}  ({r.reason})")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="commander-advise",
        description="Suggest swaps for a deck without using a browser-Claude session.",
    )
    p.add_argument("--user", required=True, help="Filename of the user deck (under commander/).")
    p.add_argument("--bracket", type=int, required=True)
    p.add_argument(
        "--source",
        choices=("heuristic", "bracket_peers", "claude"),
        default=None,
        help=(
            "Recommendation backend. 'heuristic' (default): EDHREC "
            "aggregate. 'bracket_peers': top-5 Moxfield decks at this "
            "bracket. 'claude': LLM-aided synthesis (needs "
            "ANTHROPIC_API_KEY)."
        ),
    )
    p.add_argument(
        "--use-claude", action="store_true",
        help="Legacy alias for --source claude.",
    )
    p.add_argument(
        "--claude-model", default=None,
        help=(
            "When --source claude, pick the Anthropic tier. Default "
            "is Sonnet 4.5; use 'claude-haiku-4-5' for ~3-5x cheaper "
            "routine audits."
        ),
    )
    p.add_argument("--output", help="Write JSON manifest here (audit_manifest schema).")
    args = p.parse_args(argv)

    # If both --source and --use-claude are passed, --source wins
    # (it can name backends --use-claude can't). Otherwise the legacy
    # flag falls through to source=claude.
    effective_source = args.source
    if effective_source is None and args.use_claude:
        effective_source = "claude"

    advise_kwargs = {"source": effective_source}
    if args.claude_model:
        advise_kwargs["claude_model"] = args.claude_model
    report = advise(Path(args.user), args.bracket, **advise_kwargs)
    text = _format_report_text(report)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))

    if args.output:
        Path(args.output).write_text(
            json.dumps(report.to_manifest(), indent=2),
            encoding="utf-8",
        )
        print(f"\nWrote manifest to {args.output}")
        print("Feed it into commander-iterate via --manifest after snapshotting v2.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

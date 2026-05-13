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
    commander-advise --user "..." --bracket 3 --use-claude
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
from .knowledge_log import DEFAULT_DB_PATH, iterations_for_deck
from .scryfall_client import _parse_commander_names_from_dck, lookup_card
from .staples import (
    classify_role,
    is_basic_land,
    is_universal_staple,
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
    source: str = "heuristic"         # "heuristic" | "claude" | "ollama"
    timestamp: str = ""
    # When `source` falls back to "heuristic" because a requested LLM
    # backend was unavailable or threw, this captures the user-visible
    # reason. None on the happy path. Populated even on success when
    # the fallback was specifically requested.
    fallback_reason: Optional[str] = None

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
        # Don't recommend cutting basic lands or universal staples.
        if is_basic_land(card) or is_universal_staple(card):
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


# --- LLM-aided variant (Claude) -------------------------------------------

_CLAUDE_ADVISOR_SYSTEM = """You are a deck-tuning advisor for Magic: the Gathering Commander. \
Given:
  - The deck's commander(s)
  - The current decklist
  - Performance signals from prior simulations (win rate, draw rate, fastest loss, etc.)
  - EDHREC inclusion% / synergy% data for this commander

Recommend a structured set of swaps that addresses the weakness signals. \
Return JSON ONLY (no prose, no markdown):

{
  "rationale": "one paragraph diagnosing the weakness and explaining the swap strategy",
  "added": ["Card A", "Card B", ...],
  "removed": ["Card X", "Card Y", ...]
}

Constraints:
- Recommend 4-8 adds and 4-8 removes (the deck must stay at 99 cards in the [Main]).
- Each `added` card SHOULD appear in EDHREC's data unless you have strong synergy reason.
- Each `removed` card MUST be in the current decklist.
- Don't recommend cutting basic lands, Sol Ring, Arcane Signet, or Command Tower.
- Match adds and removes 1-for-1 by count.
- Focus the rationale on the weakness signals, not generic deck-building advice.
"""


DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"


def _claude_swap_recommendations(
    deck_filename: str,
    bracket: int,
    deck_cards: set[str],
    diagnosis: DeckDiagnosis,
    edhrec_page: CommanderPage,
    model: str = DEFAULT_CLAUDE_MODEL,
) -> tuple[list[SwapRecommendation], str]:
    """LLM-aided variant. Falls back via NotImplementedError when no API key
    or SDK — caller catches and degrades to heuristic.

    ``model`` lets callers pick a tier (haiku for cheap, sonnet for default
    quality, opus for deepest reasoning). The default tracks
    ``DEFAULT_CLAUDE_MODEL`` so existing callers don't change behavior;
    cost-sensitive runs can pass ``model='claude-haiku-4-5'`` for a
    ~3-5x cheaper request."""
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
    user_message = json.dumps({
        "deck_filename": deck_filename,
        "bracket": bracket,
        "current_decklist": sorted(deck_cards),
        "performance": asdict(diagnosis),
        "edhrec_signals": edhrec_compact,
    }, indent=2)

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
    deck_dir: Path = DECK_DIR,
    match_dir: Path = MATCH_DIR,
    claude_model: str = DEFAULT_CLAUDE_MODEL,
) -> AdviceReport:
    """Generate swap recommendations for one deck. Default uses heuristic
    over EDHREC data; pass `use_claude=True` to escalate to LLM-aided
    synthesis (falls back to heuristic if API unavailable).

    ``claude_model`` selects the Anthropic tier when ``use_claude=True``;
    defaults to Sonnet for general quality. Pass ``"claude-haiku-4-5"``
    to cut cost ~3-5x on routine audits."""
    if not deck_path.is_absolute():
        deck_path = deck_dir / deck_path
    if not deck_path.exists():
        raise FileNotFoundError(f"deck not found: {deck_path}")

    # Resolve commander names + EDHREC page.
    commanders = _parse_commander_names_from_dck(deck_path)
    if not commanders:
        raise ValueError(f"no commanders found in {deck_path.name}")
    edhrec_page = fetch_commander_page(commanders[0])

    # Build diagnosis from prior matches.
    diagnosis = _aggregate_match_history(deck_path.name, match_dir=match_dir)

    # Pull current cards.
    main_cards = set(_read_main_cards(deck_path))

    # Pick backend.
    source = "heuristic"
    rationale_override: Optional[str] = None
    fallback_reason: Optional[str] = None
    recs: list[SwapRecommendation]
    if use_claude:
        try:
            recs, rationale_override = _claude_swap_recommendations(
                deck_path.name, bracket, main_cards, diagnosis, edhrec_page,
                model=claude_model,
            )
            source = "claude"
        except NotImplementedError as exc:
            fallback_reason = f"claude advisor unavailable: {exc}"
            print(f"  WARN: {fallback_reason}; falling back to heuristic.",
                  flush=True)
            recs = _heuristic_swap_recommendations(main_cards, edhrec_page, diagnosis=diagnosis)
        except Exception as exc:  # noqa: BLE001
            # Concrete cause helps diagnose: AuthenticationError (bad
            # key), APIConnectionError (network), JSONDecodeError
            # (model returned non-JSON), etc.
            fallback_reason = (
                f"claude advisor failed ({type(exc).__name__}: {exc})"
            )
            print(f"  WARN: {fallback_reason}; falling back to heuristic.",
                  flush=True)
            recs = _heuristic_swap_recommendations(main_cards, edhrec_page, diagnosis=diagnosis)
    else:
        recs = _heuristic_swap_recommendations(main_cards, edhrec_page)

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
    p.add_argument("--use-claude", action="store_true",
                   help="Synthesize via Claude API rather than heuristic.")
    p.add_argument("--output", help="Write JSON manifest here (audit_manifest schema).")
    args = p.parse_args(argv)

    report = advise(Path(args.user), args.bracket, use_claude=args.use_claude)
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

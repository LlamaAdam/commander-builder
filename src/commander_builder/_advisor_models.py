"""Internal dataclasses for ``improvement_advisor``.

Extracted out of ``improvement_advisor.py`` (1,267 lines → manageable
chunks) so the per-source recommender modules
(``_advisor_manabase``, ``_advisor_bracket_peers``,
``_advisor_claude``, ``_advisor_heuristic``) can share these types
without circular imports through the orchestrator.

External consumers should keep importing from
``commander_builder.improvement_advisor`` — the orchestrator
re-exports everything so the public surface didn't change.

The module is underscore-prefixed to signal "internal layout" — if
you're reading test code or new feature work, prefer the public
``improvement_advisor`` re-export.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .edhrec_client import AverageDeck


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
    # Adds the ownership filter dropped in 'exclude' mode (the user
    # registered a collection and asked for owned-only suggestions).
    # Each entry: {card, reason}. Same disclosure contract as
    # skipped_for_saturation — the UI can say "skipped 4 adds you
    # don't own" instead of silently shortening the list. Always
    # empty when no collection is registered or in 'flag' mode.
    skipped_for_ownership: list[dict] = field(default_factory=list)
    # Non-zero only when ``source="claude"`` AND the Claude path
    # successfully fetched bracket-peer references before the LLM
    # call. Lets the UI disclose 'Claude analyst (5 peer refs)' on
    # the source pill so users can tell when the LLM had archetype-
    # specific data vs. just EDHREC averages.
    bracket_peer_ref_count: int = 0
    # EDHREC's bracket-specific sample build for this commander. When
    # present, the audit UI surfaces a collapsible "Average deck
    # preview" panel showing the FULL ~75-100 card reference list (not
    # just the cards being recommended for adds). None when EDHREC has
    # no published average deck for this commander+bracket, or when
    # the fetch failed. Kept as the raw dataclass so the route can
    # project it however the UI needs without coupling the report
    # to the projection shape.
    average_deck: Optional["AverageDeck"] = None
    # Card-name → EDHREC section header map (lowercase keys) sourced
    # from the commander page's ``category_lists``. Used by the audit
    # route to categorize average-deck preview entries (Creatures /
    # Lands / Instants / Sorceries / Mana Artifacts / Game Changers /
    # ...) so the UI can group the preview list. Empty dict when no
    # commander page was fetched or no categories were parsed.
    edhrec_categories: dict[str, str] = field(default_factory=dict)

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


@dataclass
class AdvicePhase:
    """One step of the streaming audit pipeline.

    The streaming endpoint (``/api/audit/stream``) drives
    ``_advise_steps()`` which yields these in order:

    - ``"diagnosis"``: emitted as soon as match-history aggregation
      completes (~10ms). Carries the parsed ``DeckDiagnosis`` so the
      UI can show "weak vs early aggression" / "no closer" pills
      while the source-specific recommender is still working.
    - ``"manabase"``: emitted after the curated manabase essentials
      filter runs (~50ms). Carries the prepended manabase
      recommendations so shock/fetch/bond/tribal lands surface
      first, before the slow Claude path returns.
    - ``"primary"``: emitted after the source-specific recommender
      finishes — heuristic (~100ms), bracket_peers (~2-3s), or
      claude (~6-8s). Carries the primary recommendations,
      effective ``source`` (may differ from requested if a fallback
      happened), and the rationale-override string from Claude when
      present.
    - ``"complete"``: emitted last, after the saturation filter +
      Scryfall hallucination validator. Carries the full
      ``AdviceReport`` so the client can finalize state.
    - ``"error"``: emitted instead of ``complete`` on a hard
      failure. Carries ``{"reason": str, "where": str}``.

    Non-streaming callers can collect the whole iterator and
    assemble a final ``AdviceReport`` — that's how the existing
    ``advise()`` synchronous entry point works today.
    """
    phase: str
    data: dict

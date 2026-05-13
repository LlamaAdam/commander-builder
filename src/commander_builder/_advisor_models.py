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
from typing import Optional


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

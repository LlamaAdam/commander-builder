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
    commander-advise --user "..." --bracket 3 --budget          # skip ABU duals + fetches
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


# Dataclasses live in _advisor_models so the per-source recommender
# modules can share them without circular imports through this
# orchestrator. Re-exported here so external imports
# (`from commander_builder.improvement_advisor import AdviceReport`)
# stay valid.
from ._advisor_models import AdviceReport, DeckDiagnosis, SwapRecommendation


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


# Signal-to-roles map + _signals_to_priority_roles + heuristic
# recommender all live in _advisor_heuristic. Re-exported here so
# external imports (and the legacy in-file `_aggregate_match_history`
# that calls `_signals_to_priority_roles`) still resolve.
from ._advisor_heuristic import (  # noqa: E402
    MIN_INCLUSION_PCT_FOR_ADD,
    MIN_SYNERGY_PCT,
    _heuristic_swap_recommendations,
    _signals_to_priority_roles,
)


# Kept here only to avoid touching the constant during the chunked
# refactor — the orchestrator's _aggregate_match_history uses
# _signals_to_priority_roles which now lives in _advisor_heuristic.


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


# Manabase recommender extracted to its own module so the dataclass
# import surface is narrow. External callers continue to use
# `from .improvement_advisor import _missing_manabase_recommendations`
# via this re-export.
from ._advisor_manabase import _missing_manabase_recommendations  # noqa: E402,F401


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
    """Wrapper preserved for backward-compatible public imports.

    The actual implementation lives in ``_advisor_role_helpers``
    so per-source recommender modules can import from there
    without circular references through this orchestrator.
    """
    from ._advisor_role_helpers import _role_for_card as _impl
    return _impl(card_name)




# --- Bracket-peers recommender (sources from other tuned builds) ----------
#
# The bracket-peers source + Claude-prompt peer summary live in
# _advisor_bracket_peers. Re-exported here so existing
# `from commander_builder.improvement_advisor import _peer_card_frequency`
# style imports keep working.
from ._advisor_bracket_peers import (  # noqa: E402
    DEFAULT_BRACKET_PEERS_N,
    _bracket_peers_recommendations,
    _collect_bracket_peer_summary_for_prompt,
    _default_min_refs,
    _extract_main_cards_from_moxfield_json,
    _peer_card_frequency,
)



# --- LLM-aided variant (Claude) -------------------------------------------
#
# System prompt + _claude_swap_recommendations live in _advisor_claude
# so the per-source modules each have ~one file. Re-exported here.
from ._advisor_claude import (  # noqa: E402
    DEFAULT_CLAUDE_MODEL,
    _CLAUDE_ADVISOR_SYSTEM,
    _claude_swap_recommendations,
)

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
    p.add_argument(
        "--budget", action="store_true",
        help=(
            "Skip $200+ ABU duals + $25-60 fetch lands from manabase "
            "recommendations. Shocks, bond lands, and utility fixers "
            "still surface. For users explicitly opting out of the "
            "most expensive cards."
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
    if args.budget:
        advise_kwargs["budget"] = True
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

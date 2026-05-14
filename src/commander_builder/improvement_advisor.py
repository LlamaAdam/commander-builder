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

from .edhrec_client import (
    AverageDeck,
    CardEntry,
    CommanderPage,
    fetch_average_deck,
    fetch_commander_page,
    fetch_tag_page,
    tribe_tag_slug,
)
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
from ._advisor_models import (
    AdviceReport,
    AdvicePhase,
    DeckDiagnosis,
    SwapRecommendation,
)


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


# Post-recommendation filters extracted to _advisor_filters. Plus the
# manabase recommender. All re-exported here so external callers
# don't see the move.
from ._advisor_filters import (  # noqa: E402,F401
    _filter_for_saturation,
    _validate_card_names,
)
from ._advisor_manabase import _missing_manabase_recommendations  # noqa: E402,F401


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

    Implementation note: this synchronous entry point collects all
    phases from ``_advise_steps()`` and assembles the final report.
    Callers that want to render partial progress (e.g. the streaming
    ``/api/audit/stream`` endpoint) should drive ``_advise_steps()``
    directly and consume each ``AdvicePhase`` as it arrives.
    """
    final_report: Optional[AdviceReport] = None
    for phase in _advise_steps(
        deck_path, bracket,
        use_claude=use_claude, source=source,
        deck_dir=deck_dir, match_dir=match_dir,
        claude_model=claude_model, budget=budget,
    ):
        if phase.phase == "complete":
            final_report = phase.data.get("report")
        elif phase.phase == "error":
            # Re-raise to preserve the legacy exception contract — the
            # synchronous entry point has always thrown for missing
            # deck files / no commanders. The streaming endpoint
            # catches this same condition at the generator level and
            # emits an ``error`` phase instead.
            err_type = phase.data.get("type", "RuntimeError")
            err_msg = phase.data.get("reason", "unknown error")
            if err_type == "FileNotFoundError":
                raise FileNotFoundError(err_msg)
            if err_type == "ValueError":
                raise ValueError(err_msg)
            raise RuntimeError(err_msg)
    if final_report is None:
        raise RuntimeError(
            "advise() pipeline ended without emitting a 'complete' phase",
        )
    return final_report


def _advise_steps(
    deck_path: Path,
    bracket: int,
    use_claude: bool = False,
    source: Optional[str] = None,
    deck_dir: Path = DECK_DIR,
    match_dir: Path = MATCH_DIR,
    claude_model: str = DEFAULT_CLAUDE_MODEL,
    budget: bool = False,
):
    """Generator version of :func:`advise` — yields ``AdvicePhase``
    events as each stage of the pipeline completes.

    Event sequence (happy path):

    1. ``diagnosis`` — deck context resolved (~10ms): commanders,
       parsed deck cards, prior-match diagnosis.
    2. ``manabase`` — curated manabase essentials computed (~50ms):
       ABU duals / fetches / shocks / bond / tribal lands the deck
       is missing for its color identity.
    3. ``primary`` — source-specific recommendations (~100ms to 8s
       depending on source): the heuristic / bracket_peers / claude
       output. Includes ``effective_source`` which may differ from
       the requested source on fallback.
    4. ``complete`` — saturation filter + Scryfall hallucination
       validator have run; the final ``AdviceReport`` is assembled
       and ready to send.

    Failure modes:

    - File-not-found / no-commanders / bad-source-value: yields a
      single ``error`` phase with ``type`` set to the original
      exception class name. The synchronous ``advise()`` re-raises
      to preserve the legacy exception contract.
    - Mid-pipeline source failures (Claude API down, network blip
      on Moxfield, EDHREC slug mismatch) are NOT errors — they fall
      back to the heuristic and emit a normal ``primary`` event
      with a populated ``fallback_reason``.

    Test guidance: tests that need to inspect a single phase can
    iterate this directly and break early. The synchronous wrapper
    is the legacy contract; this generator is the source of truth.
    """
    # --- Validate inputs early so the error phase fires before any
    # work runs (saves a Scryfall round-trip on bad input).
    if source is None:
        source = "claude" if use_claude else "heuristic"
    if source not in ("heuristic", "claude", "bracket_peers"):
        yield AdvicePhase("error", {
            "type": "ValueError",
            "reason": (
                f"source must be one of heuristic/claude/bracket_peers, "
                f"got {source!r}"
            ),
            "where": "input_validation",
        })
        return

    if not deck_path.is_absolute():
        deck_path = deck_dir / deck_path
    if not deck_path.exists():
        yield AdvicePhase("error", {
            "type": "FileNotFoundError",
            "reason": f"deck not found: {deck_path}",
            "where": "input_validation",
        })
        return

    commanders = _parse_commander_names_from_dck(deck_path)
    if not commanders:
        yield AdvicePhase("error", {
            "type": "ValueError",
            "reason": f"no commanders found in {deck_path.name}",
            "where": "commander_parse",
        })
        return
    primary_commander = commanders[0]

    diagnosis = _aggregate_match_history(deck_path.name, match_dir=match_dir)
    main_cards = set(_read_main_cards(deck_path))

    # --- Phase 1: diagnosis. Streamed immediately so the UI can
    # render weakness pills + "scanning for X" placeholders while
    # the slow source-specific call is still in flight.
    yield AdvicePhase("diagnosis", {
        "deck_filename": deck_path.name,
        "bracket": bracket,
        "commander_names": commanders,
        "diagnosis": asdict(diagnosis),
    })

    # --- Phase 2: manabase essentials. Cheap (one Scryfall lookup
    # for commander + a catalog walk), so we run this before the
    # slow source-specific recommender even though it's prepended
    # later. Streaming it now means manabase shock/fetch suggestions
    # appear in the UI in well under a second.
    manabase_recs: list[SwapRecommendation] = []
    tribe: Optional[str] = None
    try:
        commander_card = lookup_card(primary_commander)
        if commander_card:
            ci = commander_card.get("color_identity") or []
            ci_set = {c.upper() for c in ci if isinstance(c, str)}
            tribe = detect_tribal_type(
                commander_card.get("oracle_text", "") or "",
                commander_card.get("type_line", "") or "",
            )
            manabase_recs = list(_missing_manabase_recommendations(
                main_cards, ci_set, tribe=tribe, budget=budget,
            ))
    except Exception:  # noqa: BLE001
        # Commander lookup failure shouldn't break the audit — emit
        # an empty manabase event so the UI doesn't hang waiting.
        pass
    yield AdvicePhase("manabase", {
        "recommendations": [asdict(r) for r in manabase_recs],
        "tribe": tribe,
    })

    # --- Phase 3: source-specific recommendations. The slow phase
    # — claude can take 6-8s for the LLM round trip. We yield as
    # soon as the call returns.
    rationale_override: Optional[str] = None
    fallback_reason: Optional[str] = None
    edhrec_page: Optional[CommanderPage] = None
    average_deck: Optional[AverageDeck] = None
    tag_page: Optional[CommanderPage] = None
    bracket_peer_ref_count: int = 0
    recs: list[SwapRecommendation]
    effective_source = source  # mutate on fallback

    def _fetch_edhrec_lazy() -> Optional[CommanderPage]:
        nonlocal edhrec_page
        if edhrec_page is None:
            edhrec_page = fetch_commander_page(primary_commander)
        return edhrec_page

    def _fetch_tag_page_lazy() -> Optional[CommanderPage]:
        """Pull the EDHREC ``/tags/<tribe>`` page when the deck has
        a detected tribal type. Tag pages share the commander
        page's 14-section structure and surface the BROADER
        archetype pool — top Dragon staples, Dragon-tribal-
        specific synergies, Game Changer Dragons, etc. — that
        complement the commander-specific recs.

        Best-effort: returns None for non-tribal decks or tribes
        not in the curated ``_TRIBE_TO_TAG_SLUG`` map.
        """
        nonlocal tag_page
        if tag_page is None and tribe:
            slug = tribe_tag_slug(tribe)
            if slug:
                try:
                    tag_page = fetch_tag_page(slug)
                except Exception:  # noqa: BLE001
                    tag_page = None
        return tag_page

    def _fetch_avg_deck_lazy() -> Optional[AverageDeck]:
        """Pull EDHREC's bracket-specific "average deck" — a curated
        ~73-98 card reference deck (vs. the 200+ uncategorized
        cards on the commander page itself).

        Adds ~1-2s per audit but the signal is uniquely strong:
        the average deck is a coherent SAMPLE BUILD, not a flat
        ranking. For cuts it acts as a high-confidence "yes this
        card belongs in this archetype" signal; for adds it
        surfaces cards that the typical tuned deck runs but the
        user's deck doesn't.

        Best-effort: returns None when EDHREC doesn't publish an
        average deck for this commander/bracket combo (newly-
        released or very obscure commanders). The heuristic
        gracefully degrades to the commander-page data only.
        """
        nonlocal average_deck
        if average_deck is None:
            try:
                average_deck = fetch_average_deck(
                    primary_commander, bracket=bracket,
                )
            except Exception:  # noqa: BLE001
                average_deck = None
        return average_deck

    if source == "bracket_peers":
        peer_recs, ref_count = _bracket_peers_recommendations(
            commander_name=primary_commander,
            bracket=bracket,
            deck_cards=main_cards,
            diagnosis=diagnosis,
        )
        if peer_recs:
            recs = peer_recs
        else:
            fallback_reason = (
                f"no bracket-peer references found for "
                f"{primary_commander!r} at B{bracket} — "
                f"falling back to EDHREC heuristic"
            )
            print(f"  WARN: {fallback_reason}.", flush=True)
            page = _fetch_edhrec_lazy()
            recs = _heuristic_swap_recommendations(
                main_cards, page, diagnosis=diagnosis,
                average_deck=_fetch_avg_deck_lazy(),
                tag_page=_fetch_tag_page_lazy(),
            )
            effective_source = "heuristic"
    elif source == "claude":
        page = _fetch_edhrec_lazy()
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
            if peer_summary:
                bracket_peer_ref_count = int(
                    peer_summary.get("ref_count", 0) or 0,
                )
        except NotImplementedError as exc:
            fallback_reason = f"claude advisor unavailable: {exc}"
            print(f"  WARN: {fallback_reason}; falling back to heuristic.",
                  flush=True)
            recs = _heuristic_swap_recommendations(
                main_cards, page, diagnosis=diagnosis,
                average_deck=_fetch_avg_deck_lazy(),
                tag_page=_fetch_tag_page_lazy(),
            )
            effective_source = "heuristic"
        except Exception as exc:  # noqa: BLE001
            fallback_reason = (
                f"claude advisor failed ({type(exc).__name__}: {exc})"
            )
            print(f"  WARN: {fallback_reason}; falling back to heuristic.",
                  flush=True)
            recs = _heuristic_swap_recommendations(
                main_cards, page, diagnosis=diagnosis,
                average_deck=_fetch_avg_deck_lazy(),
                tag_page=_fetch_tag_page_lazy(),
            )
            effective_source = "heuristic"
    else:
        page = _fetch_edhrec_lazy()
        recs = _heuristic_swap_recommendations(
            main_cards, page,
            average_deck=_fetch_avg_deck_lazy(),
            tag_page=_fetch_tag_page_lazy(),
        )

    yield AdvicePhase("primary", {
        "recommendations": [asdict(r) for r in recs],
        "requested_source": source,
        "effective_source": effective_source,
        "fallback_reason": fallback_reason,
        "rationale_override": rationale_override,
        "bracket_peer_ref_count": bracket_peer_ref_count,
    })

    # --- Phase 4: finalize. Prepend manabase, apply saturation
    # filter, validate names. The UI can show "finalizing..." here
    # since this is the post-processing step.
    deck_id: Optional[str] = None
    try:
        text = deck_path.read_text(encoding="utf-8")
        import re
        m = re.search(r"^Moxfield=(.+)$", text, re.MULTILINE)
        if m:
            deck_id = m.group(1).strip()
    except OSError:
        pass

    # Prepend manabase so curated essentials surface at the top,
    # then deduplicate so the same card never appears in both the
    # manabase + primary slices.
    #
    # Real failure mode caught in the 2026-05-13 live-browser audit:
    # manabase prepends shock lands (Steam Vents, Blood Crypt, etc.)
    # for a 5-color deck missing them, AND bracket_peers ALSO
    # recommends those shock lands because they appear in 4/5 peer
    # references and aren't in the user's deck. Without dedup, the
    # combined ``recs`` list contains "Steam Vents" twice; the audit
    # response renders it twice; the proposed .dck has ``1 Steam
    # Vents`` listed twice (illegal in singleton Commander) and Forge
    # rejects the deck.
    #
    # Manabase wins on collision because (a) it's prepended (first
    # to appear), and (b) its source tag (``manabase_essentials``)
    # is more specific than a generic peer reference for the UI's
    # source-badge rendering.
    seen_lc: set[str] = set()
    deduped: list[SwapRecommendation] = []
    for rec in list(manabase_recs) + list(recs):
        # Only de-dup add candidates — cuts are user-deck cards
        # already present and naturally singleton.
        if rec.action == "add":
            key = rec.card.lower()
            if key in seen_lc:
                continue
            seen_lc.add(key)
        deduped.append(rec)
    recs = deduped

    role_counts = count_deck_roles(main_cards)
    recs, skipped_for_saturation = _filter_for_saturation(recs, role_counts)

    _validate_card_names(recs)

    # Structured per-recommendation logging (opt-in via the
    # ``COMMANDER_BUILDER_LOG_DECISIONS`` env var). One line per
    # rec lands in ``_audit_decisions.log`` next to the existing
    # ``_js_errors.log`` and ``_forge_py_correlation.csv``. Pattern
    # drift (e.g. another Cyclonic-Rift-shaped misclassification)
    # then surfaces via grep instead of via Chrome screenshot.
    from ._advisor_logging import log_decisions as _log_decisions
    _log_decisions(
        deck_path=deck_path,
        commander_names=commanders,
        effective_source=effective_source,
        recommendations=recs,
        fallback_reason=fallback_reason,
    )

    report = AdviceReport(
        deck_filename=deck_path.name,
        deck_id=deck_id,
        bracket=bracket,
        commander_names=commanders,
        diagnosis=diagnosis,
        recommendations=recs,
        source=effective_source,
        timestamp=datetime.now(timezone.utc).isoformat(),
        fallback_reason=fallback_reason,
        skipped_for_saturation=skipped_for_saturation,
        bracket_peer_ref_count=bracket_peer_ref_count,
    )
    if rationale_override:
        report.diagnosis.pattern_summary = rationale_override

    yield AdvicePhase("complete", {"report": report})


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

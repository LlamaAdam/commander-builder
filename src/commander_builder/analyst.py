"""LLM analyst — assigns verdicts to iteration outcomes.

Phase 2's "did this swap actually help?" voice. Inputs are a
`ComparisonReport` (from `compare_versions.py`) plus the audit's swap manifest;
output is a structured verdict the iteration loop uses to decide whether to
keep the swap, revert it, or treat as neutral.

Verdict taxonomy:

  "kept"      — sim shows clear improvement; swap stays
  "reverted"  — sim shows regression; old version restored
  "neutral"   — within noise threshold; user decides

The analyst itself is just a function. It can be implemented three ways:

  1. Heuristic-only (no LLM)        — `heuristic_verdict()` below
  2. Claude API (high quality)       — `claude_verdict()` (live;
     falls back to NotImplementedError if anthropic SDK or
     ANTHROPIC_API_KEY missing — router degrades to heuristic)
  3. Local Ollama (cost saving)      — `ollama_verdict()` (live;
     falls back to NotImplementedError if Ollama isn't reachable)

The default `analyze()` runs the heuristic and falls back to higher-quality
sources only when the heuristic is uncertain. This keeps the loop running
without API access and saves tokens on obvious cases.

Routing thresholds are tunable via `AnalystConfig`. The router itself is plain
Python — no framework lock-in.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Optional

from ._llm_json import extract_json_object


# --- Verdict statistics ----------------------------------------------------

def binomial_two_sided_p(k: int, n: int) -> float:
    """Exact two-sided binomial test of ``k`` successes in ``n`` trials
    against the null hypothesis p = 0.5.

    Returns P(|X - n/2| >= |k - n/2|) for X ~ Binomial(n, 0.5) — the
    probability that a truly neutral swap would produce a head-to-head
    split at least this lopsided by chance. Symmetric in k vs n-k, so the
    same function scores improvements and regressions.

    Why an exact test and not a Wilson interval or scipy: scipy is not a
    dependency of this project, the decisive-game counts here are small
    (tens, not thousands) where normal approximations are at their worst,
    and under the p=0.5 null the exact tail sum is ~5 lines of
    ``math.comb``. The old fixed absolute-margin thresholds were
    game-count-invariant: with 20 decisive games, P(|new-old| >= 4) under
    the null is ~0.50, so half of all *neutral* swaps earned a confident
    kept/reverted verdict. A p-value scales the bar with the sample size.

    Edge cases: n <= 0 returns 1.0 (no evidence, never significant).
    """
    if n <= 0:
        return 1.0
    # |2k - n| == 2 * |k - n/2|; integer math avoids float comparisons.
    dev = abs(2 * k - n)
    tail = sum(
        math.comb(n, i) for i in range(n + 1) if abs(2 * i - n) >= dev
    )
    return tail / (2 ** n)


# --- Inputs and outputs ----------------------------------------------------

@dataclass
class AnalystInput:
    """Everything the analyst needs to render a verdict."""
    deck_name: str
    bracket: int
    audit_manifest: dict     # {added: [...], removed: [...], rationale: "..."}
    sim_report: dict         # ComparisonReport.to_dict()


@dataclass
class Verdict:
    label: str               # "kept" | "reverted" | "neutral"
    confidence: float        # 0-1
    reasoning: str           # human-readable explanation
    lessons: list[str] = field(default_factory=list)  # transferable observations
    source: str = "heuristic"  # which path produced this verdict

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# --- Routing config --------------------------------------------------------

@dataclass
class AnalystConfig:
    """Knobs for the verdict router. Defaults are empirical guesses; tune as
    we accumulate iteration data.

    2026-08-14 — significance-based verdicts. ``heuristic_verdict`` now
    scores the head-to-head split with an exact two-sided binomial test
    against p=0.5 (see ``binomial_two_sided_p``); a strong kept/reverted
    verdict requires ``p < alpha``. The two ``margin_*`` knobs below are
    DEPRECATED: they are accepted for backward compatibility (existing
    callers may still construct AnalystConfig with them) but are no
    longer read anywhere — a fixed absolute margin is game-count-
    invariant and mislabels ~50% of neutral swaps at 20 decisive games.
    Tune ``alpha`` instead.
    """
    margin_strong_threshold: int = 4   # DEPRECATED (unread): superseded by `alpha`
    margin_noise_threshold: int = 2    # DEPRECATED (unread): superseded by `alpha`
    alpha: float = 0.05                # two-sided significance bar for kept/reverted
    min_decisive_games: int = 8        # head-to-head decisive games needed for any verdict
    use_claude: bool = False           # Set True when ANTHROPIC_API_KEY is wired.
    use_ollama: bool = False           # Set True when local model is running.
    claude_model: str = "claude-sonnet-4-5"
    ollama_model: str = "llama3.2:3b"
    ollama_url: str = "http://localhost:11434/api/generate"


# --- Public entry ----------------------------------------------------------

def analyze(input_: AnalystInput, config: Optional[AnalystConfig] = None) -> Verdict:
    """Route an analyst request through the configured ladder.

    The router prefers cheaper sources and only escalates when the heuristic
    can't render confidently. Override `config.use_claude` / `use_ollama` once
    those backends are wired."""
    config = config or AnalystConfig()

    heuristic = heuristic_verdict(input_, config)
    # Strong signal from heuristic — stop here.
    if heuristic.confidence >= 0.75:
        return heuristic

    # Heuristic is uncertain; try the configured LLM backends in order.
    #
    # Two distinct failure modes, handled differently ON PURPOSE:
    #
    #   NotImplementedError  — backend not wired (no key / SDK / daemon).
    #                          Expected in many configs; quiet fall-through.
    #   any other Exception  — backend IS wired but the call failed:
    #                          unparseable/truncated model output
    #                          (LLMJsonError), API/network errors, SDK
    #                          surprises. Previously these escaped and
    #                          crashed the whole iteration loop mid-run.
    #                          Now: log LOUDLY (an operator must see that
    #                          a paid backend is misbehaving) and degrade
    #                          to the heuristic verdict so the pipeline
    #                          keeps moving on empirical data.
    if config.use_ollama:
        try:
            return ollama_verdict(input_, config)
        except NotImplementedError:
            pass  # Fall through to Claude or back to heuristic.
        except Exception as exc:  # noqa: BLE001 — see contract above
            print(
                f"WARNING: ollama_verdict failed "
                f"({type(exc).__name__}: {exc}); "
                f"falling back to claude/heuristic verdict.",
                flush=True,
            )
    if config.use_claude:
        try:
            return claude_verdict(input_, config)
        except NotImplementedError:
            pass
        except Exception as exc:  # noqa: BLE001 — see contract above
            print(
                f"WARNING: claude_verdict failed "
                f"({type(exc).__name__}: {exc}); "
                f"falling back to heuristic verdict.",
                flush=True,
            )

    # No LLM available — return the (low-confidence) heuristic anyway. Caller
    # decides whether to flag for manual review.
    return heuristic


# --- Heuristic backend (no LLM, deterministic) -----------------------------

def heuristic_verdict(input_: AnalystInput, config: AnalystConfig) -> Verdict:
    """Render a verdict from the head-to-head numbers alone. Cheap,
    deterministic, handles the obvious cases well.

    Decisive convention (2026-08-14 fix): decisive = old_wins + new_wins —
    HEAD-TO-HEAD decisive games only, matching knowledge_log's win-rate
    denominator and ``_proposer_sim._verdict_from_ab``. The old
    ``total_games - draws`` counted games won by the two FILLER seats in
    the 4-player pod (roughly half of all pod games), so the
    min_decisive_games gate effectively always passed even when the A/B
    pair had won only 2-3 games between them.

    Significance (2026-08-14 fix): the kept/reverted call is an exact
    two-sided binomial test of the split against p=0.5
    (``binomial_two_sided_p``), strong only when p < ``config.alpha``.
    Confidence mapping: strong verdicts carry ``min(0.97, 1 - p)`` —
    always >= 1 - alpha = 0.95, above the router's 0.75 escalation bar,
    same role the old fixed 0.85 played. Non-significant splits stay
    "neutral" at 0.4 (below the bar, so the router may escalate to an
    LLM) and the draws-dominated/inconclusive gate stays at 0.3."""
    sim = input_.sim_report
    old_wins = sim.get("old_stats", {}).get("wins", 0)
    new_wins = sim.get("new_stats", {}).get("wins", 0)
    total = sim.get("total_games", 0)
    draws = sim.get("draws", 0)
    decisive = old_wins + new_wins   # head-to-head only; fillers/draws excluded
    delta = new_wins - old_wins

    # Inconclusive sim: too few head-to-head decisive games to read a
    # signal (draws and filler-seat wins took the rest).
    if decisive < config.min_decisive_games:
        return Verdict(
            label="neutral",
            confidence=0.3,
            reasoning=(
                f"Inconclusive: only {decisive}/{total} games were decisive "
                f"head-to-head wins ({draws} draws; the rest went to filler "
                f"seats). Below the {config.min_decisive_games}-game "
                "minimum for a reliable verdict."
            ),
            lessons=[
                "decks_drew_too_often: consider stronger finisher cards "
                "or a different filler pair to break stalemates",
            ],
            source="heuristic",
        )

    p_value = binomial_two_sided_p(new_wins, decisive)

    # Statistically significant improvement / regression.
    if p_value < config.alpha and delta != 0:
        label = "kept" if delta > 0 else "reverted"
        verb = "won" if delta > 0 else "lost"
        return Verdict(
            label=label,
            confidence=min(0.97, 1.0 - p_value),
            reasoning=(
                f"New version {verb} {new_wins}-{old_wins} (signed margin "
                f"{delta:+d}) over {decisive} head-to-head decisive games; "
                f"exact binomial p={p_value:.4f} < alpha={config.alpha}."
            ),
            lessons=_extract_swap_lessons(input_.audit_manifest, label),
            source="heuristic",
        )

    # Not significant — heuristic uncertain. Confidence stays low so the
    # router escalates to LLM when one is configured.
    return Verdict(
        label="neutral",
        confidence=0.4,
        reasoning=(
            f"Within noise: {new_wins}-{old_wins} over {decisive} head-to-head "
            f"decisive games is not statistically significant (exact binomial "
            f"p={p_value:.4f} >= alpha={config.alpha}). Could be variance."
        ),
        lessons=[],
        source="heuristic",
    )


def _extract_swap_lessons(manifest: dict, label: str) -> list[str]:
    """Generate transferable observations from the swap.

    These are simple facts the analyst saw — not deep insights. The eventual
    Claude/Ollama path produces richer lessons (e.g. 'aggressive draw spells
    underperform when the pod includes Atraxa Infect') but the heuristic just
    notes the cards involved so Phase 3 can correlate."""
    added = manifest.get("added", []) or []
    removed = manifest.get("removed", []) or []
    if label == "kept":
        return [f"swap_kept: added {len(added)}, removed {len(removed)}"]
    if label == "reverted":
        return [f"swap_reverted: added {len(added)}, removed {len(removed)}"]
    return []


# --- Claude backend --------------------------------------------------------

# System prompt for `claude_verdict`. Stable across calls so prompt caching
# at the SDK level reuses the prefix and saves tokens.
_CLAUDE_VERDICT_SYSTEM = """You are the analyst step in a closed-loop deck improvement pipeline. \
Given a Magic: the Gathering Commander deck swap proposal and the empirical \
result of head-to-head Forge simulation between the old and new versions, \
render a structured verdict.

Output JSON ONLY (no prose, no markdown). Schema:
{
  "label": "kept" | "reverted" | "neutral",
  "confidence": 0.0-1.0,
  "reasoning": "one paragraph explaining the verdict",
  "lessons": ["transferable observation 1", "..."]
}

The sim summary reports signed_margin = new_wins - old_wins (positive \
means the NEW version won more head-to-head games), the winner, draws, \
and h2h_decisive = old_wins + new_wins (games the compared pair actually \
won; draws and games won by the two filler seats are excluded).

Verdict rules:
- "kept": signed_margin > 0 AND the split is statistically meaningful at \
the reported h2h_decisive count — judge it like an exact binomial test of \
new_wins/h2h_decisive against a 50/50 null (e.g. 16-4 over 20 decisive is \
significant, p~0.01; 12-8 over 20 is NOT, p~0.5 — a coin would do that \
half the time). A meaningful qualitative gain (avg ending life much \
higher, fewer eliminations) can also justify "kept".
- "reverted": signed_margin < 0 under the same significance standard.
- "neutral": the split is within binomial noise for the sample size, OR \
the sim was inconclusive (few h2h_decisive games: most games drew or \
went to filler seats).

Confidence: 0.85+ for statistically clear signals; 0.5-0.7 for noisy \
cases; below 0.5 when the sim itself doesn't carry signal (e.g. >50% \
draws or very few h2h_decisive games).

Lessons should be transferable observations another iteration could learn \
from — patterns about cards, archetypes, or deck-tuning. Not just \
restatements of the numbers.
"""


def _parse_verdict_payload(text: str, backend: str) -> dict:
    """Recover a verdict JSON object from possibly-fenced or
    prose-prefixed model output via the shared ``_llm_json`` extractor.

    Raises ``LLMJsonError`` (NOT NotImplementedError) on garbage or
    truncated output. The distinction matters: NotImplementedError means
    "backend not wired" and ``analyze()`` falls through SILENTLY; a
    parse failure means the backend IS wired but returned junk — that
    must surface as a LOUD warning in ``analyze()`` (which catches it
    and degrades to the heuristic) rather than being indistinguishable
    from a missing API key. The old in-house fence-strip + greedy-regex
    fallback lived here; it's now the shared extractor so all LLM parse
    sites behave identically."""
    return extract_json_object(text, context=backend)


def _safe_confidence(value: object) -> float:
    """Coerce a model-supplied confidence to float, defaulting to 0.5 on a
    non-numeric value (e.g. the model writes \"high\" instead of 0.9) so the
    verdict doesn't crash with ValueError."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5


def _summarize_h2h(sim: dict) -> dict:
    """Build the shared head-to-head core of the LLM-facing sim summary.

    2026-08-14 fix: the old summaries forwarded ``sim['margin']`` —
    ``ComparisonReport.margin`` is ``abs(new - old)``, so a model reading
    "margin: 6" on a 6-game REGRESSION would write confidently wrong
    lessons; the ollama summary also omitted the winner. The core now
    carries the SIGNED margin (new_wins - old_wins, computed from the win
    counts rather than trusted from the report), the winner (derived the
    same way when the report doesn't say), draws, and h2h_decisive
    (old_wins + new_wins — the head-to-head sample size the verdict
    prompt's significance language refers to)."""
    old_wins = sim.get("old_stats", {}).get("wins", 0)
    new_wins = sim.get("new_stats", {}).get("wins", 0)
    winner = sim.get("winner")
    if winner is None:
        winner = (
            "new" if new_wins > old_wins
            else "old" if old_wins > new_wins
            else "tie"
        )
    return {
        "total_games": sim.get("total_games", 0),
        "draws": sim.get("draws", 0),
        "old_wins": old_wins,
        "new_wins": new_wins,
        "signed_margin": new_wins - old_wins,
        "h2h_decisive": old_wins + new_wins,
        "winner": winner,
    }


def claude_verdict(input_: AnalystInput, config: AnalystConfig) -> Verdict:
    """Render a verdict via the Claude API.

    Falls back to NotImplementedError if `anthropic` SDK isn't installed or
    `ANTHROPIC_API_KEY` is missing — the router catches and degrades to the
    heuristic. When wired, uses prompt caching on the system prompt so repeat
    iteration calls reuse the cached prefix."""
    import os

    if "ANTHROPIC_API_KEY" not in os.environ:
        raise NotImplementedError(
            "claude_verdict requires ANTHROPIC_API_KEY to be set."
        )
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise NotImplementedError(
            "claude_verdict requires `pip install anthropic` (in the [claude] extras)."
        ) from exc

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Compact the sim report for the model — the full ComparisonReport JSON
    # has per-pod telemetry the analyst doesn't need.
    sim = input_.sim_report
    summary = {
        **_summarize_h2h(sim),
        "old_stats": {
            k: sim.get("old_stats", {}).get(k)
            for k in ("avg_ending_life", "avg_damage_taken",
                      "avg_turns_when_won", "avg_turns_when_lost",
                      "fastest_elimination_turn", "eliminations")
        },
        "new_stats": {
            k: sim.get("new_stats", {}).get(k)
            for k in ("avg_ending_life", "avg_damage_taken",
                      "avg_turns_when_won", "avg_turns_when_lost",
                      "fastest_elimination_turn", "eliminations")
        },
    }
    user_message = json.dumps({
        "deck_name": input_.deck_name,
        "bracket": input_.bracket,
        "audit_manifest": input_.audit_manifest,
        "sim_summary": summary,
    }, indent=2)

    response = client.messages.create(
        model=config.claude_model,
        max_tokens=1024,
        system=_CLAUDE_VERDICT_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    # Anthropic returns content as a list of content blocks; the first text
    # block is what we want.
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text
    if not text.strip():
        raise NotImplementedError("claude_verdict: empty response from API")

    parsed = _parse_verdict_payload(text, "claude_verdict")
    label = parsed.get("label", "neutral")
    if label not in {"kept", "reverted", "neutral"}:
        label = "neutral"
    return Verdict(
        label=label,
        confidence=_safe_confidence(parsed.get("confidence", 0.5)),
        reasoning=str(parsed.get("reasoning", "")),
        lessons=list(parsed.get("lessons", []) or []),
        source="claude",
    )


# --- Ollama backend --------------------------------------------------------

def ollama_verdict(input_: AnalystInput, config: AnalystConfig) -> Verdict:
    """Render a verdict via a local Ollama model.

    POSTs to the local Ollama HTTP API (`http://localhost:11434/api/generate`
    by default). Free at runtime — no API tokens — but quality depends on
    which model is pulled. Default `llama3.2:3b` is small and fast; for
    higher-fidelity verdicts use a larger model in `config.ollama_model`.

    Falls back to NotImplementedError if the daemon isn't reachable; router
    degrades to the heuristic. Connection failure shows up as a
    ConnectionError or URLError on the urlopen call."""
    import urllib.error
    import urllib.request

    sim = input_.sim_report
    # Same signed-margin/winner/draws/decisive core as claude_verdict —
    # see _summarize_h2h for why the ABSOLUTE report margin is never
    # forwarded to a model.
    summary = _summarize_h2h(sim)
    instruction = (
        _CLAUDE_VERDICT_SYSTEM
        + "\n\nDeck swap to evaluate:\n"
        + json.dumps({
            "deck_name": input_.deck_name,
            "bracket": input_.bracket,
            "audit_manifest": input_.audit_manifest,
            "sim_summary": summary,
        }, indent=2)
    )
    body = json.dumps({
        "model": config.ollama_model,
        "prompt": instruction,
        "stream": False,
        "format": "json",  # Ollama's "force JSON output" mode
    }).encode("utf-8")
    req = urllib.request.Request(
        config.ollama_url, data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise NotImplementedError(
            f"Ollama daemon not reachable at {config.ollama_url}: {exc}"
        ) from exc

    text = payload.get("response", "")
    if not text:
        raise NotImplementedError("ollama_verdict: empty response from daemon")
    parsed = _parse_verdict_payload(text, "ollama_verdict")
    label = parsed.get("label", "neutral")
    if label not in {"kept", "reverted", "neutral"}:
        label = "neutral"
    return Verdict(
        label=label,
        confidence=_safe_confidence(parsed.get("confidence", 0.5)),
        reasoning=str(parsed.get("reasoning", "")),
        lessons=list(parsed.get("lessons", []) or []),
        source="ollama",
    )

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
  2. Claude API (high quality)       — `claude_verdict()` (stub for now)
  3. Local Ollama (cost saving)      — `ollama_verdict()` (stub for now)

The default `analyze()` runs the heuristic and falls back to higher-quality
sources only when the heuristic is uncertain. This keeps the loop running
without API access and saves tokens on obvious cases.

Routing thresholds are tunable via `AnalystConfig`. The router itself is plain
Python — no framework lock-in.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional


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
    we accumulate iteration data."""
    margin_strong_threshold: int = 4   # |new_wins - old_wins| ≥ 4 in 20 games → strong signal
    margin_noise_threshold: int = 2    # |delta| ≤ 2 → noise; punt to LLM if available
    min_decisive_games: int = 8        # If most games drew, the sim isn't conclusive
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
    if config.use_ollama:
        try:
            return ollama_verdict(input_, config)
        except NotImplementedError:
            pass  # Fall through to Claude or back to heuristic.
    if config.use_claude:
        try:
            return claude_verdict(input_, config)
        except NotImplementedError:
            pass

    # No LLM available — return the (low-confidence) heuristic anyway. Caller
    # decides whether to flag for manual review.
    return heuristic


# --- Heuristic backend (no LLM, deterministic) -----------------------------

def heuristic_verdict(input_: AnalystInput, config: AnalystConfig) -> Verdict:
    """Render a verdict from numeric thresholds alone. Cheap, deterministic,
    handles the obvious cases well. Confidence drops when the sim was
    inconclusive (mostly draws) or the margin sits in the noise band."""
    sim = input_.sim_report
    old_wins = sim.get("old_stats", {}).get("wins", 0)
    new_wins = sim.get("new_stats", {}).get("wins", 0)
    total = sim.get("total_games", 0)
    draws = sim.get("draws", 0)
    decisive = total - draws
    delta = new_wins - old_wins

    lessons: list[str] = []

    # Inconclusive sim: too many draws to read a signal.
    if decisive < config.min_decisive_games:
        return Verdict(
            label="neutral",
            confidence=0.3,
            reasoning=(
                f"Inconclusive: only {decisive}/{total} games were decisive "
                f"({draws} draws). Below the {config.min_decisive_games}-game "
                "minimum for a reliable verdict."
            ),
            lessons=[
                "decks_drew_too_often: consider stronger finisher cards "
                "or a different filler pair to break stalemates",
            ],
            source="heuristic",
        )

    # Strong improvement.
    if delta >= config.margin_strong_threshold:
        return Verdict(
            label="kept",
            confidence=0.85,
            reasoning=f"New version won {new_wins}-{old_wins} (margin {delta}) over {decisive} decisive games.",
            lessons=_extract_swap_lessons(input_.audit_manifest, "kept"),
            source="heuristic",
        )

    # Strong regression.
    if delta <= -config.margin_strong_threshold:
        return Verdict(
            label="reverted",
            confidence=0.85,
            reasoning=f"New version lost {new_wins}-{old_wins} (margin {-delta}) over {decisive} decisive games.",
            lessons=_extract_swap_lessons(input_.audit_manifest, "reverted"),
            source="heuristic",
        )

    # Noise band — heuristic uncertain. Confidence stays low so the router
    # escalates to LLM when one is configured.
    return Verdict(
        label="neutral",
        confidence=0.4,
        reasoning=(
            f"Within noise: delta {delta} over {decisive} decisive games is below "
            f"the {config.margin_strong_threshold}-game threshold. Could be variance."
        ),
        lessons=lessons,
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


# --- Claude backend (stub) -------------------------------------------------

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

Verdict rules:
- "kept": clear improvement (margin >=4 wins / 20 games, OR meaningful \
qualitative gain — e.g. avg ending life much higher, fewer eliminations).
- "reverted": clear regression. Same threshold inverted.
- "neutral": within noise OR sim was inconclusive (most games drew).

Confidence: 0.85+ for clear signals; 0.5-0.7 for noisy cases; below 0.5 \
when the sim itself doesn't carry signal (e.g. >50% draws).

Lessons should be transferable observations another iteration could learn \
from — patterns about cards, archetypes, or deck-tuning. Not just \
restatements of the numbers.
"""


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
        "total_games": sim.get("total_games", 0),
        "draws": sim.get("draws", 0),
        "old_wins": sim.get("old_stats", {}).get("wins", 0),
        "new_wins": sim.get("new_stats", {}).get("wins", 0),
        "margin": sim.get("margin", 0),
        "winner": sim.get("winner"),
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
        raise RuntimeError("claude_verdict: empty response from API")

    parsed = json.loads(text)
    label = parsed.get("label", "neutral")
    if label not in {"kept", "reverted", "neutral"}:
        label = "neutral"
    return Verdict(
        label=label,
        confidence=float(parsed.get("confidence", 0.5)),
        reasoning=str(parsed.get("reasoning", "")),
        lessons=list(parsed.get("lessons", []) or []),
        source="claude",
    )


# --- Ollama backend (stub) -------------------------------------------------

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
    summary = {
        "total_games": sim.get("total_games", 0),
        "draws": sim.get("draws", 0),
        "old_wins": sim.get("old_stats", {}).get("wins", 0),
        "new_wins": sim.get("new_stats", {}).get("wins", 0),
        "margin": sim.get("margin", 0),
    }
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
        raise RuntimeError("ollama_verdict: empty response from daemon")
    parsed = json.loads(text)
    label = parsed.get("label", "neutral")
    if label not in {"kept", "reverted", "neutral"}:
        label = "neutral"
    return Verdict(
        label=label,
        confidence=float(parsed.get("confidence", 0.5)),
        reasoning=str(parsed.get("reasoning", "")),
        lessons=list(parsed.get("lessons", []) or []),
        source="ollama",
    )

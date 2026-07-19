"""LLM proposer -- produces a swap manifest for an iteration cycle.

Phase 2's "what should change?" voice. Inputs are a deck (filename or
Moxfield URL); output is a structured `audit_manifest.json` that
`iteration_loop.run_one_iteration` can ingest.

Three implementations sharing one interface:

  ManualProposer  -- wraps the existing prompt-paste workflow. Reads a
                    pre-generated manifest from disk. The path you're on
                    today; the only proposer that requires no API access.
  ClaudeProposer  -- invokes Claude with `prompts/moxfield_audit_v3.md` as
                    system, deck content as user input. Currently a stub
                    that raises NotImplementedError until the anthropic SDK
                    is installed and ANTHROPIC_API_KEY is set.
  OllamaProposer  -- local model variant. Stub. Lower-quality but free.

The router `propose()` picks based on `ProposerConfig`. Default is Manual
because it's the only path that works without external dependencies; flip
`use_claude=True` once the SDK + key are wired.

A `ProposerOutput` is what `iteration_loop` actually consumes:

    {
        "added": [...],
        "removed": [...],
        "rationale": "...",
        "audit_version": "v3",
        "audit_timestamp": "...",
        "deck_id": "...",        # Moxfield publicId or local stem
        "source": "manual" | "claude" | "ollama",
    }
"""

from __future__ import annotations

# When run as ``python -m commander_builder.proposer``, Python only
# registers this file as ``__main__`` — it does NOT also enter it in
# ``sys.modules`` as ``commander_builder.proposer``. So when a sibling
# module (``_proposer_cli``) later does ``from .proposer import …``,
# Python re-executes proposer.py from scratch as ``commander_builder.
# proposer``, which then triggers the same import chain and ends in
# ``ImportError: cannot import name 'auto_curate_main' from partially
# initialized module``. Aliasing the module object under both names up
# front breaks the loop: the sibling import finds the already-loaded
# (partially-initialized) module and pulls already-defined names from it.
import sys as _sys
if __name__ == "__main__" and "commander_builder.proposer" not in _sys.modules:
    _sys.modules["commander_builder.proposer"] = _sys.modules["__main__"]

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"


# --- Inputs / outputs ------------------------------------------------------

@dataclass
class ProposerInput:
    """Everything a proposer might need to render a manifest."""
    deck_path: Path
    bracket: int
    deck_id: Optional[str] = None        # Moxfield publicId; resolved by caller
    moxfield_url: Optional[str] = None
    deck_text: Optional[str] = None      # .dck contents; loaded if None


@dataclass
class ProposerOutput:
    """The structured manifest. Schema mirrors `audit_manifest.json` from
    the Moxfield audit prompt's Closing Summary so the format stays
    interchangeable between manual paste and programmatic call."""
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    rationale: str = ""
    audit_version: str = "v3"
    audit_timestamp: Optional[str] = None
    deck_id: Optional[str] = None
    source: str = "manual"

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["audit_timestamp"] is None:
            d["audit_timestamp"] = datetime.now(timezone.utc).isoformat()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# --- Routing config --------------------------------------------------------

@dataclass
class ProposerConfig:
    """Knobs for the proposer router. Mirrors `analyst.AnalystConfig` shape
    so they're interchangeable in user-facing CLIs."""
    use_claude: bool = False
    use_ollama: bool = False
    claude_model: str = "claude-sonnet-4-5"
    ollama_model: str = "llama3.2:3b"
    ollama_url: str = "http://localhost:11434/api/generate"
    # Path the manual proposer reads from. Default is the `audit_manifest.json`
    # convention used in `docs/audit_workflow.md`.
    manifest_path: Optional[Path] = None


# --- Public entry ----------------------------------------------------------

def propose(input_: ProposerInput, config: Optional[ProposerConfig] = None) -> ProposerOutput:
    """Route a proposer call through the configured ladder.

    Order: Claude (if enabled) -> Ollama (if enabled) -> Manual fallback.
    Manual is the safety net because it's the only path guaranteed to work
    without external deps. If you set `use_claude=True` but the SDK isn't
    installed, the call falls back to manual rather than crashing the loop."""
    config = config or ProposerConfig()

    if config.use_claude:
        try:
            return claude_propose(input_, config)
        except NotImplementedError:
            pass  # Fall through to next backend.
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN: claude_propose failed ({type(exc).__name__}); "
                  f"falling back to ollama/manual.")
    if config.use_ollama:
        try:
            return ollama_propose(input_, config)
        except NotImplementedError:
            pass
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN: ollama_propose failed ({type(exc).__name__}); "
                  f"falling back to manual.")
    return manual_propose(input_, config)


# --- Manual backend (works today, no API needed) ---------------------------

def manual_propose(input_: ProposerInput, config: ProposerConfig) -> ProposerOutput:
    """Read a hand-prepared `audit_manifest.json` from disk and convert it
    into a `ProposerOutput`. The user produces the manifest by running the
    Moxfield audit prompt in a separate Claude session (the workflow in
    `docs/audit_workflow.md`).

    Default manifest path is `<deck_path>.audit_manifest.json` next to the
    deck file. Override via `config.manifest_path`."""
    path = config.manifest_path
    if path is None:
        # Convention: `[USER] My Deck v2 [B3].dck.audit_manifest.json` lives
        # next to the .dck so `iteration_loop` can find it without flags.
        path = input_.deck_path.parent / f"{input_.deck_path.name}.audit_manifest.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No manifest at {path}. Either run the Moxfield audit prompt and "
            f"save the result here, or set config.manifest_path to point at it."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return ProposerOutput(
        added=list(data.get("added", [])),
        removed=list(data.get("removed", [])),
        rationale=str(data.get("rationale", "")),
        audit_version=str(data.get("audit_version", "v3")),
        audit_timestamp=data.get("audit_timestamp"),
        deck_id=input_.deck_id or data.get("deck_id"),
        source="manual",
    )


# --- Claude backend (stub, ready to be filled in) --------------------------

_CLAUDE_SYSTEM_PROMPT_FILE = PROMPTS_DIR / "moxfield_audit_v3.md"


def claude_propose(input_: ProposerInput, config: ProposerConfig) -> ProposerOutput:
    """Render a swap manifest via the Claude API.

    Uses `prompts/moxfield_audit_v3.md` as the system prompt and the deck
    contents as the user message. Token cost on a real audit is non-trivial
    (~$0.10-$0.50 per deck depending on context length); the system prompt
    is large but stable, so SDK-level prompt caching reuses the prefix.

    Falls back to NotImplementedError without ANTHROPIC_API_KEY or without
    the `anthropic` SDK installed; the router catches and degrades to manual.
    """
    # NOTE: claude_propose deliberately stays SDK-only and degrades to the
    # manual proposer when no API key is present (this is the propose() router
    # contract -- ClaudeProposer is opt-in/interactive). The UNATTENDED curator
    # path (auto_propose) is the one wired to the subscription `claude` CLI,
    # because it must run without a key. Keep these two paths' semantics
    # distinct on purpose.
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise NotImplementedError(
            "claude_propose requires ANTHROPIC_API_KEY to be set."
        )
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise NotImplementedError(
            "claude_propose requires `pip install anthropic` (in the [claude] extras)."
        ) from exc

    if not _CLAUDE_SYSTEM_PROMPT_FILE.exists():
        raise FileNotFoundError(
            f"audit prompt missing: {_CLAUDE_SYSTEM_PROMPT_FILE}. "
            f"Reinstall the project or restore prompts/moxfield_audit_v3.md."
        )
    system_prompt = _CLAUDE_SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    deck_text = input_.deck_text or input_.deck_path.read_text(encoding="utf-8")

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    user_msg = (
        f"Run the audit workflow on the following deck. Use bracket "
        f"{input_.bracket} as the target. Skip Step 5.6 (the goldfish sim) -- "
        f"compare_versions handles that empirically downstream.\n\n"
        f"Deck (Forge .dck format):\n```\n{deck_text}\n```\n\n"
        f"After completing the audit, return ONLY the audit_manifest JSON "
        f"object documented in the Closing Summary. No prose, no markdown "
        f"code fences. Just the JSON."
    )

    response = client.messages.create(
        model=config.claude_model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text
    if not text.strip():
        raise RuntimeError("claude_propose: empty response from API")

    # Tolerate Claude wrapping JSON in code fences despite the instruction.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)
        cleaned = cleaned[1] if len(cleaned) > 1 else text
        # Strip optional language tag like ```json
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    manifest = json.loads(cleaned)
    return ProposerOutput(
        added=list(manifest.get("added", []) or []),
        removed=list(manifest.get("removed", []) or []),
        rationale=str(manifest.get("rationale", "")),
        audit_version=str(manifest.get("audit_version", "v3")),
        audit_timestamp=manifest.get("audit_timestamp"),
        deck_id=input_.deck_id or manifest.get("deck_id"),
        source="claude",
    )


# --- Ollama backend (stub) -------------------------------------------------

def ollama_propose(input_: ProposerInput, config: ProposerConfig) -> ProposerOutput:
    """Render a swap manifest via a local Ollama model.

    POSTs to `config.ollama_url` with the audit prompt as instruction +
    deck contents. Free at runtime; quality depends on the model. The audit
    workflow's blind-build-then-diff approach is meaningfully harder than
    the analyst's verdict task, so expect lower fidelity than `claude_propose`
    on a small local model. Useful as a fallback when API access is
    unavailable.

    Falls back to NotImplementedError if the daemon isn't reachable."""
    import urllib.error
    import urllib.request

    if not _CLAUDE_SYSTEM_PROMPT_FILE.exists():
        raise FileNotFoundError(
            f"audit prompt missing: {_CLAUDE_SYSTEM_PROMPT_FILE}"
        )
    system_prompt = _CLAUDE_SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")
    deck_text = input_.deck_text or input_.deck_path.read_text(encoding="utf-8")

    instruction = (
        f"{system_prompt}\n\n"
        f"Run the audit on this deck. Target bracket: {input_.bracket}. Skip "
        f"Step 5.6 -- compare_versions handles that downstream.\n\n"
        f"Deck (Forge .dck format):\n{deck_text}\n\n"
        f"Output ONLY the audit_manifest JSON. No prose, no code fences."
    )
    body = json.dumps({
        "model": config.ollama_model,
        "prompt": instruction,
        "stream": False,
        "format": "json",
    }).encode("utf-8")
    req = urllib.request.Request(
        config.ollama_url, data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise NotImplementedError(
            f"Ollama daemon not reachable at {config.ollama_url}: {exc}"
        ) from exc

    text = payload.get("response", "")
    if not text:
        raise RuntimeError("ollama_propose: empty response from daemon")
    manifest = json.loads(text)
    return ProposerOutput(
        added=list(manifest.get("added", []) or []),
        removed=list(manifest.get("removed", []) or []),
        rationale=str(manifest.get("rationale", "")),
        audit_version=str(manifest.get("audit_version", "v3")),
        audit_timestamp=manifest.get("audit_timestamp"),
        deck_id=input_.deck_id or manifest.get("deck_id"),
        source="ollama",
    )


# ===========================================================================
# Auto-propose curator path -- used by `commander-iterate --auto-propose`
# without a pre-built manifest.
# ===========================================================================
#
# The three proposers above generate manifests from scratch: deck -> Claude ->
# audit_manifest. That's the right shape when a human is running an audit
# session, but for unattended overnight refinement we already have a wide
# candidate set from the EDHREC heuristic advisor (advise.AdviceReport).
# What's missing is a small, focused curator that picks N adds + N cuts
# from that pool with bracket-aware guardrails.
#
# Surface (used by tests + commander-auto-curate CLI):
#
#   Proposal(adds, cuts, rationale, source, dropped_for_bracket).to_dict()
#   auto_propose(deck_path, bracket, advice_report, max_adds, max_cuts)
#     -> Proposal
#   apply_proposal_to_deck(src_path, proposal, dry_run=False) -> out_path
#   enforce_bracket_caps(adds, bracket) -> (kept, dropped)
#
# All Claude calls are exercised through the same Anthropic SDK as
# claude_propose above; tests mock by injecting a fake ``anthropic`` module.


import re as _re


@dataclass
class Proposal:
    """A curated swap manifest from ``auto_propose()``.

    Distinct from ``ProposerOutput`` so the curator path can carry
    metadata the audit-from-scratch path doesn't have (``dropped_for_
    bracket``, the audited bracket itself). Serializable via ``to_dict()``
    so iteration_loop / knowledge_log can persist it alongside the
    iteration row without bespoke encoders.

    Fields split into REQUESTED (what Claude asked for) and APPLIED
    (what actually landed in the .dck after balancing/padding):

      adds / cuts                -- what Claude proposed.
      dropped_for_bracket        -- game-changers stripped at B1/B2.
      applied_adds / applied_cuts -- what landed after balancing via
                                    min(adds, cuts). Populated by
                                    apply_proposal_to_deck.
      dropped_for_balance        -- adds OR cuts sliced off because
                                    the lists were unequal length.
      padded_count + padded_breakdown -- basics synthesized to bring
                                        the proposed deck to 99 main.
    """
    adds: list[str] = field(default_factory=list)
    cuts: list[str] = field(default_factory=list)
    rationale: str = ""
    source: str = "claude-auto"
    # Cards Claude wanted to add but enforce_bracket_caps stripped because
    # of the bracket's game-changer rule. At B1/B2 ALL game-changers are
    # stripped; at B3/B4 the deck-level 3-card cap kicks in and adds
    # beyond ``3 - current_gc_count`` get dropped. Surfaced so the
    # iteration log can record "Claude wanted Smothering Tithe at B2 --
    # filtered" or "the deck already has 3 GCs, Mana Drain was dropped"
    # without losing the signal.
    dropped_for_bracket: list[str] = field(default_factory=list)
    # Cards Claude proposed for cut that match the user's protected list
    # ([metadata] Protect= entries + --protect CLI flags). Sliced before
    # ``cuts`` is returned, so the curator never wastes a slot on a pet
    # card the user locked. Surfaced so the iteration log + CLI summary
    # can show "Claude wanted to cut Goblin Lackey but you protected it."
    dropped_for_protection: list[str] = field(default_factory=list)
    # Cards Claude proposed as ADDS that violate the deck's color
    # identity (e.g. a green creature for a mono-red Goblin deck).
    # The curator system prompt asks for color-identity respect but
    # Claude occasionally hallucinates off-color picks. Filtering them
    # out post-response prevents auto-curate from writing an illegal
    # .dck file (Commander disallows off-color cards; Forge refuses
    # to load such decks). Same defensive pattern as bracket caps +
    # protection.
    dropped_for_color_identity: list[str] = field(default_factory=list)
    # Populated by apply_proposal_to_deck. Empty until that call.
    applied_adds: list[str] = field(default_factory=list)
    applied_cuts: list[str] = field(default_factory=list)
    dropped_for_balance: list[str] = field(default_factory=list)
    padded_count: int = 0
    padded_breakdown: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# Post-response curator filters live in ``_proposer_filters`` so the
# orchestrator stays focused. Re-exported here for back-compat with
# imports like ``from commander_builder.proposer import
# enforce_bracket_caps``.
from ._proposer_filters import (  # noqa: E402
    _BRACKET_NO_GAME_CHANGERS_THRESHOLD,
    _load_game_changers,
    _safe_lookup_card,
    enforce_bracket_caps,
    enforce_color_identity,
)


_AUTO_PROPOSE_SYSTEM_PROMPT = """You are a Commander deck curator. The user
will give you a target bracket (1=Exhibition, 2=Core, 3=Upgraded,
4=Optimized, 5=cEDH), a deck's current contents, and a CANDIDATE POOL of
adds + cuts already filtered by the EDHREC heuristic advisor.

Your job: pick a small, applicable subset of those candidates that will
genuinely improve the deck at the target bracket. Respect these rules:

- THE CAPS ARE CEILINGS, NOT TARGETS. The user will tell you the maximum
  number of adds and cuts you may propose. DO NOT fill the cap just
  because you can. If the deck only needs 3 changes, propose 3. If it
  needs zero, propose zero. Padding a proposal to "use up the slots"
  produces worse decks -- every unnecessary swap dilutes the signal of
  your real recommendations and increases the user's verification cost.
  Quality beats quantity, always.
- Stay within color identity (do NOT add off-color cards).
- Honor the bracket: at B1/B2, avoid game-changers (the user's pipeline
  will strip them anyway, but don't waste a slot recommending them).
- Pick adds and cuts that pair -- if you add a finisher, cut a redundant
  filler, not a key utility piece.
- NEVER propose cutting a card in the user's PROTECTED CARDS list. Those
  are locked pet cards; any cut you propose against them gets stripped
  by the post-filter and wastes a curator slot. Pick a different cut.
- Justify briefly. Two sentences max.

Return ONLY a JSON object with this exact shape:

  {"adds": ["Card 1", "Card 2", ...],
   "cuts": ["Cut 1", "Cut 2", ...],
   "rationale": "one or two sentences explaining the swap intent"}

If your honest assessment is that the deck needs no changes, return
empty lists: ``{"adds": [], "cuts": [], "rationale": "..."}``. That's
a valid response. The system records "zero changes proposed" cleanly.

OUTPUT FORMAT IS STRICT. Your ENTIRE response must be the JSON object
and nothing else. Do NOT write any prose, analysis, explanation, or
reasoning before the JSON. Do NOT write headers like "## Assessment"
or "**Deck Analysis:**". Do NOT think out loud. The "rationale" field
INSIDE the JSON is the only place your reasoning belongs. The first
character of your response must be ``{`` and the last must be ``}``.
No code fences. No markdown. Just the raw JSON object.
"""


def _extract_curator_json(text: str) -> Optional[dict]:
    """Pull the curator's JSON object out of a Claude response.

    The system prompt asks for a raw JSON object but real responses
    sometimes have:
      - Prose preamble ("Looking at this deck, I think...")
      - Markdown code fences (```json ... ```)
      - Trailing prose after the object
      - Multiple ``{...}`` blocks if the curator includes an example
        in its rationale

    Strategy: try ``json.loads(text)`` first (covers the happy path
    where the response IS just JSON). If that fails, locate the first
    ``{`` and find its matching ``}`` by counting braces (handles
    nested objects inside ``rationale`` strings). If parsing that
    substring also fails, return None so the caller surfaces a
    diagnostic.

    Strings inside JSON can legally contain ``{`` and ``}``, so a
    naive find-last-} approach mis-parses. The brace-counter respects
    string context (skips braces inside double-quoted runs).
    """
    cleaned = text.strip()

    # Path 1: try the whole response as JSON. Common case under the
    # strict prompt.
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Path 2: strip markdown code fences if present, then retry.
    if "```" in cleaned:
        fenced = cleaned.split("```", 2)
        if len(fenced) >= 2:
            inner = fenced[1]
            # Strip optional language tag like ```json
            inner = inner.split("\n", 1)[1] if "\n" in inner else inner
            inner = inner.rsplit("```", 1)[0].strip()
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass

    # Path 3: find the first balanced ``{...}`` block in the text.
    # Walks character-by-character respecting string context so a
    # ``{`` inside a JSON string doesn't confuse the counter.
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        end = -1
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            candidate = cleaned[start:end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        # That block didn't parse — search for the next ``{`` and try
        # again. Handles "prose with { in it } then the real JSON".
        start = cleaned.find("{", start + 1)

    return None


def _claude_cli_available() -> bool:
    import shutil
    return shutil.which("claude") is not None


def _curator_complete_via_cli(system: str, user_msg: str, *,
                              model: Optional[str] = None,
                              timeout: int = 240) -> str:
    """Run the curator turn through the subscription `claude` CLI instead of
    the Anthropic SDK.

    Why: this project commonly runs under a Claude Max *subscription* (auth via
    the `claude` CLI's on-disk credentials) with NO ANTHROPIC_API_KEY. The SDK
    path hard-requires an API key (= separate per-token billing). Routing the
    curator through the CLI keeps curation free under the subscription.

    Implementation notes:
      - system + user are concatenated and sent on STDIN (not as argv), so we
        never hit Windows' ~8KB command-line limit on the (large) deck prompt.
      - ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are scrubbed from the child
        env so the CLI always uses subscription auth, never flips to API
        billing, even if a key is present for other tooling.
      - No --model is forced; the CLI's configured default (a capable
        subscription model) is used. `model` is accepted for parity/logging.
    """
    import shutil
    import subprocess

    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` CLI not found on PATH (needed for "
                           "subscription-mode curation without an API key).")

    import time as _time

    prompt = f"{system}\n\n---\n\n{user_msg}"
    # Scrub everything that could redirect the subscription CLI to a BILLED
    # endpoint or inject an API key — not just the two token vars. The CLI
    # honors ANTHROPIC_BASE_URL / ANTHROPIC_API_URL (proxy/redirect) and the
    # CLAUDE_CODE_USE_BEDROCK / CLAUDE_CODE_USE_VERTEX toggles (cloud-billed
    # backends). Dropping every ANTHROPIC_* key plus those toggles keeps the
    # invariant: invoking `claude` must use the logged-in subscription, never
    # inherit billing config from a parent shell that also does API work.
    _BILLING_REDIRECT_VARS = {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"}
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ANTHROPIC_") and k not in _BILLING_REDIRECT_VARS}
    cmd = [claude, "-p", "--output-format", "json"]

    # One retry: the subscription CLI occasionally returns a transient error
    # (rate-limit blip, empty result) under sustained batch use. A single
    # retry reclaims most of those without masking a real, persistent failure.
    last_err = ""
    for attempt in range(2):
        try:
            proc = subprocess.run(
                cmd, input=prompt, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude CLI timed out after {timeout}s") from exc

        if proc.returncode != 0:
            last_err = (f"claude CLI failed rc={proc.returncode}: "
                        f"{(proc.stderr or proc.stdout or '')[-300:]}")
        else:
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                # Some CLI versions emit bare text; accept that as the result.
                text = proc.stdout.strip()
                if text:
                    return text
                last_err = "claude CLI returned empty non-JSON output"
                data = None
            if isinstance(data, dict):
                if data.get("is_error"):
                    last_err = f"claude CLI reported error: {data.get('result')}"
                else:
                    result = str(data.get("result") or "")
                    if result.strip():
                        return result
                    last_err = "claude CLI returned empty result"

        if attempt == 0:
            _time.sleep(3)  # brief backoff before the single retry

    raise RuntimeError(f"curator CLI failed after retry: {last_err}")


def auto_propose(
    deck_path: Path,
    bracket: int,
    advice_report: dict,
    *,
    max_adds: int = 5,
    max_cuts: int = 5,
    model: str = "claude-sonnet-4-5",
    protected_cards: "Iterable[str]" = (),
    mode: str = "polish",
) -> Proposal:
    """Curate an advisor's candidate pool into a small applicable Proposal.

    Inputs:
      deck_path        -- .dck file; contents are sent to Claude as context.
      bracket          -- target bracket (1-5). Drives game-changer
                         enforcement.
      advice_report    -- dict shape from ``AdviceReport.to_manifest()``.
                         We feed the ``added`` + ``removed`` lists to
                         Claude as the candidate pool plus ``rationale``
                         for hint.
      max_adds         -- hard cap on returned adds (default 5).
      max_cuts         -- hard cap on returned cuts (default 5).
      model            -- Anthropic model id (default sonnet-4-5).
      protected_cards  -- names the user has locked against cuts. Listed
                         in the Claude prompt so the curator doesn't
                         waste a slot proposing to cut a pet card; any
                         that slip through anyway are post-filtered to
                         ``Proposal.dropped_for_protection``. Case-
                         insensitive match. Same pattern as bracket caps.
      mode             -- curation intensity hint passed to the curator
                         prompt: "polish" (small targeted swaps), "overhaul"
                         (major revision), or "free" (Claude decides
                         based on deck's needs). The hard cap still
                         applies via max_adds / max_cuts; mode only
                         tunes Claude's pairing aggressiveness within
                         the cap. Default 'polish'.

    Side effects: none. ``apply_proposal_to_deck`` does the file write.

    Raises:
      RuntimeError on missing ANTHROPIC_API_KEY, missing anthropic SDK,
        or an empty/unparseable Claude response. Caller should surface
        the message to the user (the curator path is unattended; a
        silent fallback would mask a real problem).
    """
    # Auth strategy: prefer the SDK when ANTHROPIC_API_KEY is set to a NON-EMPTY
    # value (per-token billing, explicit opt-in). Otherwise fall back to the
    # subscription `claude` CLI so curation works under a Claude Max plan.
    # NOTE: a present-but-empty ANTHROPIC_API_KEY ('') is treated as "no key"
    # -- this environment deliberately sets it empty to prevent the SDK from
    # ever billing, so membership-in-os.environ is the wrong test; use truthiness.
    _use_cli = not os.environ.get("ANTHROPIC_API_KEY")
    if _use_cli and not _claude_cli_available():
        raise RuntimeError(
            "auto_propose needs either ANTHROPIC_API_KEY (SDK path) or the "
            "`claude` CLI on PATH (subscription path). Neither is available."
        )
    if not _use_cli:
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover -- covered via stub
            raise RuntimeError(
                "auto_propose requires `pip install anthropic` "
                "(in the [claude] extras)."
            ) from exc

    deck_text = deck_path.read_text(encoding="utf-8")
    candidates_added = list(advice_report.get("added", []) or [])
    candidates_removed = list(advice_report.get("removed", []) or [])
    advisor_rationale = str(advice_report.get("rationale", ""))
    protected_list = list(protected_cards)
    protected_lower = {p.lower() for p in protected_list}

    # Curation-intensity hint to Claude. The hard caps (max_adds /
    # max_cuts) clip the output; this block tells the curator how
    # WILLING to spend the cap budget it should be. None of these
    # hints REQUIRE filling the cap -- see the system prompt's
    # "CAPS ARE CEILINGS NOT TARGETS" rule, which always wins.
    _MODE_HINTS = {
        "polish": (
            "MODE: POLISH. Make conservative targeted swaps. Only pair "
            "your highest-confidence picks -- leave the deck's identity "
            "intact. Prefer fewer changes if the deck is already close "
            "to ideal at this bracket. Zero changes is a valid answer "
            "when the deck doesn't need anything."
        ),
        "overhaul": (
            "MODE: OVERHAUL. The user has explicitly signaled they're "
            "open to substantial revision, so don't be timid about "
            "pairing more than a handful of swaps WHEN THE DECK "
            "WARRANTS IT. But the cap is still a ceiling, not a target "
            "-- propose only changes you'd genuinely make. A focused "
            "5-swap overhaul beats a padded 15-swap one. Use the larger "
            "budget when the deck has many misalignments; don't use it "
            "when the deck is already tight."
        ),
        "free": (
            "MODE: FREE. No specific intensity hint -- pick the number "
            "of changes that genuinely improve the deck. If the deck "
            "is well-tuned already, ship a small proposal (or zero). "
            "If it has many misalignments, propose more. Match the "
            "proposal size to the deck's actual needs."
        ),
    }
    mode_hint = _MODE_HINTS.get(mode, _MODE_HINTS["polish"])

    # Build a PROTECTED CARDS block in the user message so Claude knows
    # not to propose them for cut. Skip the block entirely when the
    # list is empty -- keeps the prompt clean for the common case.
    protected_block = ""
    if protected_list:
        protected_block = (
            "PROTECTED CARDS (locked -- NEVER propose for cut):\n"
            + "\n".join(f"- {c}" for c in protected_list)
            + "\n\n"
        )

    user_msg = (
        f"Target bracket: {bracket}\n"
        f"Deck file: {deck_path.name}\n"
        f"{mode_hint}\n\n"
        f"CURRENT DECK (Forge .dck format):\n```\n{deck_text}\n```\n\n"
        + protected_block
        + f"CANDIDATE ADDS (from EDHREC heuristic advisor):\n"
        + "\n".join(f"- {c}" for c in candidates_added)
        + "\n\nCANDIDATE CUTS (from EDHREC heuristic advisor):\n"
        + "\n".join(f"- {c}" for c in candidates_removed)
        + f"\n\nADVISOR RATIONALE: {advisor_rationale}\n\n"
        f"You may propose up to {max_adds} adds and {max_cuts} cuts. "
        f"These are CEILINGS, not targets -- if the deck only needs 3 "
        f"changes, propose 3. If it needs zero, propose zero "
        f"(empty lists, valid response). Quality beats quantity. "
        f"Return ONLY the JSON object."
    )

    if _use_cli:
        text = _curator_complete_via_cli(
            system=_AUTO_PROPOSE_SYSTEM_PROMPT, user_msg=user_msg, model=model,
        )
    else:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_AUTO_PROPOSE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
    if not text.strip():
        raise RuntimeError("auto_propose: empty response from Claude")

    payload = _extract_curator_json(text)
    if payload is None:
        raise RuntimeError(
            f"auto_propose: Claude returned unparseable JSON. "
            f"Raw payload (first 200 chars): {text[:200]!r}"
        )

    raw_adds = list(payload.get("adds", []) or [])
    raw_cuts = list(payload.get("cuts", []) or [])
    rationale = str(payload.get("rationale", "")).strip()

    # Bracket-cap filter BEFORE applying max_adds so the cap counts only
    # bracket-allowed cards. At B3/B4 the WotC guideline caps the deck
    # at 3 game-changers total; we count the current deck's GCs so the
    # curator can only add what remains under the cap. At B1/B2 the
    # count is ignored (all GCs are stripped); at B5 the cap is no-op.
    from ._proposer_filters import count_game_changers_in_deck
    current_gc_count = count_game_changers_in_deck(deck_text)
    kept_adds, dropped_for_bracket = enforce_bracket_caps(
        raw_adds, bracket,
        current_game_changer_count=current_gc_count,
    )

    # Color-identity filter: strip any add whose color identity isn't
    # a subset of the deck's commander CI. The curator system prompt
    # asks Claude to respect color identity, but it occasionally
    # hallucinates off-color picks (especially on partner decks /
    # colorless commanders). Filtering after-the-fact prevents an
    # illegal .dck from landing on disk -- Forge refuses to load decks
    # with off-color cards.
    #
    # Lookups go through scryfall_client (disk-cached) so this adds
    # ~0 wall time on a warm cache. Done AFTER bracket caps to keep
    # the dropped_for_color_identity bucket distinct from
    # dropped_for_bracket -- the user can debug each reason separately.
    #
    # Disambiguate "deck is colorless" from "couldn't resolve commander":
    # color_identity_for_commander returns "" for both cases. We need
    # to distinguish so a test-fixture commander like "Test Commander"
    # (not in Scryfall) doesn't reject every add against a phantom
    # colorless CI. If the commander resolves to a real card, the CI
    # (including "") is authoritative; if no commander resolves, pass
    # None so enforce_color_identity skips the filter entirely.
    from .scryfall_client import (
        _parse_commander_names_from_dck,
        color_identity_for_commander,
        lookup_card,
    )
    commander_names = _parse_commander_names_from_dck(deck_path)
    commander_resolved = bool(commander_names) and any(
        _safe_lookup_card(lookup_card, n) for n in commander_names
    )
    deck_color_identity: Optional[str]
    if commander_resolved:
        deck_color_identity = color_identity_for_commander(deck_path)
    else:
        deck_color_identity = None
    kept_adds, dropped_for_color_identity = enforce_color_identity(
        kept_adds, deck_color_identity,
    )

    # Protection filter: strip any cut Claude proposed against the
    # user's locked list. Same pattern as bracket caps -- done BEFORE
    # max_cuts slicing so the cap counts only allowed cuts. Claude
    # was told about the list in the prompt; this is the safety net
    # for the rare case it ignored the instruction.
    kept_cuts: list[str] = []
    dropped_for_protection: list[str] = []
    for c in raw_cuts:
        if c.lower() in protected_lower:
            dropped_for_protection.append(c)
        else:
            kept_cuts.append(c)

    capped_adds = kept_adds[:max_adds]
    capped_cuts = kept_cuts[:max_cuts]

    return Proposal(
        adds=capped_adds,
        cuts=capped_cuts,
        rationale=rationale,
        source="claude-auto",
        dropped_for_bracket=dropped_for_bracket,
        dropped_for_protection=dropped_for_protection,
        dropped_for_color_identity=dropped_for_color_identity,
    )


# Filename version-bump regex. Matches optional `v<N>` immediately before
# an optional `[B<N>]` bracket suffix and the `.dck` extension:
#   [USER] Foo [B3].dck       -> group1='[USER] Foo', v=None,  bracket='3'
#   [USER] Foo v3 [B3].dck    -> group1='[USER] Foo', v='3',   bracket='3'
#   MyDeck.dck                -> no match (handled by fallback path)
_VERSION_BRACKET_RE = _re.compile(
    r"^(?P<base>.+?)(?:\s+v(?P<ver>\d+))?\s+\[B(?P<bracket>\d+)\]\.dck$"
)
_VERSION_NO_BRACKET_RE = _re.compile(
    r"^(?P<base>.+?)(?:\s+v(?P<ver>\d+))?\.dck$"
)


def _bump_version_filename(name: str) -> str:
    """Compute the next-version filename for a .dck path.

    Rules:
      ``[USER] Foo [B3].dck``      -> ``[USER] Foo v2 [B3].dck``
      ``[USER] Foo v3 [B3].dck``   -> ``[USER] Foo v4 [B3].dck``
      ``MyDeck.dck``               -> ``MyDeck v2.dck`` (no bracket suffix)

    The bracket suffix is preserved as the last token so tooling that
    filters ``*[B3].dck`` keeps finding the file after the bump.
    """
    m = _VERSION_BRACKET_RE.match(name)
    if m:
        base = m.group("base")
        ver = int(m.group("ver") or 1)
        bracket = m.group("bracket")
        return f"{base} v{ver + 1} [B{bracket}].dck"

    m = _VERSION_NO_BRACKET_RE.match(name)
    if m:
        base = m.group("base")
        ver = int(m.group("ver") or 1)
        return f"{base} v{ver + 1}.dck"

    # Last-resort fallback for paths that don't end in .dck -- just
    # append ' v2' before whatever extension is there.
    if "." in name:
        stem, _, ext = name.rpartition(".")
        return f"{stem} v2.{ext}"
    return f"{name} v2"


def apply_proposal_to_deck(
    src_path: Path,
    proposal: Proposal,
    *,
    dry_run: bool = False,
) -> Path:
    """Write a new .dck file with ``proposal.adds`` appended and
    ``proposal.cuts`` removed from the [Main] section.

    Returns the path of the new file. In ``dry_run`` mode returns the
    path it WOULD have written without touching disk; the proposal's
    ``applied_*`` fields are still populated so the CLI can show what
    would have landed.

    The new file lives next to the source with a bumped version
    (``[USER] Foo [B3].dck`` -> ``[USER] Foo v2 [B3].dck``). Bracket
    suffix preserved as the last token.

    Deck-legality invariants -- shared with the audit-endpoint path
    via ``web/_helpers._apply_swaps_to_dck`` and ``_pad_main_to_99``:

      1. Adds and cuts are balanced via ``min(len(adds), len(cuts))``.
         Both lists are sliced to the smaller length so the resulting
         deck stays at the same mainboard size. Bigger of the two
         contributes its surplus to ``proposal.dropped_for_balance``
         so the iteration log records what Claude wanted but didn't
         apply.

      2. If the SOURCE deck was already short of 99 mainboard (some
         imports land at 71-95), the proposed deck inherits the
         deficit and gets padded with basics matching the deck's
         existing color distribution. Padded count + breakdown
         surface on ``proposal.padded_count`` /
         ``proposal.padded_breakdown``.

    This mirrors the invariants the web UI's /api/audit endpoint
    already enforces. The two flows are now in sync -- same balancing,
    same padding, same legal output.

    Mutation rules within the file:
      - [metadata], [Commander], and other sections pass through.
      - [Main] is rebuilt: drop any line whose card name matches a
        kept cut (case-insensitive); append `1 <name>` lines for each
        kept add.
      - Card-line matching handles edition codes (``|CLB|871``).
    """
    # Lazy import -- proposer.py is library-level, web/_helpers is the
    # web layer's internal helpers module. The helpers themselves are
    # pure Python (no Flask coupling) so the import is clean, but
    # importing at module-load time would create a circular risk if
    # the web layer ever imports proposer.
    from ._advisor_models import SwapRecommendation
    from .web._helpers import _apply_swaps_to_dck, _pad_main_to_99

    out_path = src_path.parent / _bump_version_filename(src_path.name)

    # Build SwapRecommendation-shape objects so _apply_swaps_to_dck
    # accepts them. The helper only reads .card and .action -- the
    # reason field is unused on this path but required by the dataclass.
    #
    # Defense-in-depth: even if auto_propose's protection filter let
    # something through (e.g. proposal was hand-constructed or came
    # from a test path), strip protected cuts here too. The protect
    # list lives in the deck's [metadata] section so we read it
    # straight from disk -- no extra arg-passing needed.
    from .web._helpers import read_protected_cards
    src_text_for_protect = src_path.read_text(encoding="utf-8")
    protected_lower = {p.lower() for p in read_protected_cards(src_text_for_protect)}
    cuts_to_apply: list[str] = []
    for c in proposal.cuts:
        if c.lower() in protected_lower:
            if c not in proposal.dropped_for_protection:
                proposal.dropped_for_protection.append(c)
        else:
            cuts_to_apply.append(c)

    recs: list[SwapRecommendation] = [
        SwapRecommendation(card=c, action="add", reason="claude-auto")
        for c in proposal.adds
    ] + [
        SwapRecommendation(card=c, action="cut", reason="claude-auto")
        for c in cuts_to_apply
    ]

    text = src_path.read_text(encoding="utf-8")
    proposed_text, applied_adds, applied_cuts, kept_count = _apply_swaps_to_dck(
        text, recs,
    )

    # Pad the proposed deck to 99 mainboard if it's short. The audit
    # endpoint does this same step for the same reason: some imports
    # ship sub-99 main and Forge refuses to load them.
    post_swap_main = kept_count + len(applied_adds)
    proposed_text, padded_count, padded_breakdown = _pad_main_to_99(
        proposed_text, post_swap_main,
    )

    # _apply_swaps_to_dck deliberately passes the [metadata] section
    # through untouched, so at this point the v2 text still carries the
    # SOURCE deck's Name=. Forge reports Name= (not the filename) in its
    # Match Result lines, so a stale Name= makes the old and new decks
    # indistinguishable to every name-keyed consumer (compare_versions,
    # pool_curator, the Forge deck picker). Stamp the output file's own
    # stem so log_parser._normalize maps results back to THIS file — the
    # invariant is documented in dck_meta.
    from .dck_meta import rewrite_name
    proposed_text = rewrite_name(proposed_text, out_path.stem)

    # Record what actually happened so the CLI summary + iteration
    # log can distinguish "Claude wanted X" from "the new .dck has Y".
    proposal.applied_adds = list(applied_adds)
    proposal.applied_cuts = list(applied_cuts)
    applied_add_set = {a.lower() for a in applied_adds}
    applied_cut_set = {c.lower() for c in applied_cuts}
    proposal.dropped_for_balance = (
        [a for a in proposal.adds if a.lower() not in applied_add_set]
        + [c for c in proposal.cuts if c.lower() not in applied_cut_set]
    )
    proposal.padded_count = padded_count
    proposal.padded_breakdown = dict(padded_breakdown)

    if dry_run:
        return out_path

    out_path.write_text(proposed_text, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# commander-auto-curate CLI -- unattended advisor -> curator -> apply pipeline
# ---------------------------------------------------------------------------

# A/B sim helpers live in _proposer_sim. Re-exported for back-compat
# with tests + callers that import _verdict_from_ab, _pick_filler_decks,
# _run_sim_and_record, _ab_to_iteration_fields, _log_auto_curate_iteration
# directly from ``commander_builder.proposer``.
from ._proposer_sim import (  # noqa: E402,F401
    _DEFAULT_SIM_MARGIN,
    _ab_to_iteration_fields,
    _log_auto_curate_iteration,
    _pick_filler_decks,
    _run_sim_and_record,
    _verdict_from_ab,
)


# auto_curate_main lives in _proposer_cli so this module stays focused on
# the dataclasses + the auto_propose pipeline. Re-exported for back-compat
# with the commander-auto-curate console_scripts entry point + any
# direct callers.
from ._proposer_cli import auto_curate_main  # noqa: E402,F401




if __name__ == "__main__":
    raise SystemExit(auto_curate_main())

"""LLM proposer — produces a swap manifest for an iteration cycle.

Phase 2's "what should change?" voice. Inputs are a deck (filename or
Moxfield URL); output is a structured `audit_manifest.json` that
`iteration_loop.run_one_iteration` can ingest.

Three implementations sharing one interface:

  ManualProposer  — wraps the existing prompt-paste workflow. Reads a
                    pre-generated manifest from disk. The path you're on
                    today; the only proposer that requires no API access.
  ClaudeProposer  — invokes Claude with `prompts/moxfield_audit_v3.md` as
                    system, deck content as user input. Currently a stub
                    that raises NotImplementedError until the anthropic SDK
                    is installed and ANTHROPIC_API_KEY is set.
  OllamaProposer  — local model variant. Stub. Lower-quality but free.

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

    Order: Claude (if enabled) → Ollama (if enabled) → Manual fallback.
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
        f"{input_.bracket} as the target. Skip Step 5.6 (the goldfish sim) — "
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
        f"Step 5.6 — compare_versions handles that downstream.\n\n"
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
# Auto-propose curator path — used by `commander-iterate --auto-propose`
# without a pre-built manifest.
# ===========================================================================
#
# The three proposers above generate manifests from scratch: deck → Claude →
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
#     → Proposal
#   apply_proposal_to_deck(src_path, proposal, dry_run=False) → out_path
#   enforce_bracket_caps(adds, bracket) → (kept, dropped)
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

      adds / cuts                — what Claude proposed.
      dropped_for_bracket        — game-changers stripped at B1/B2.
      applied_adds / applied_cuts — what landed after balancing via
                                    min(adds, cuts). Populated by
                                    apply_proposal_to_deck.
      dropped_for_balance        — adds OR cuts sliced off because
                                    the lists were unequal length.
      padded_count + padded_breakdown — basics synthesized to bring
                                        the proposed deck to 99 main.
    """
    adds: list[str] = field(default_factory=list)
    cuts: list[str] = field(default_factory=list)
    rationale: str = ""
    source: str = "claude-auto"
    # Cards Claude wanted to add but enforce_bracket_caps stripped because
    # they're WotC-designated game-changers and the target bracket is below
    # the threshold. Surfaced so the iteration log can record "Claude wanted
    # Smothering Tithe at B2 — filtered" without losing the signal.
    dropped_for_bracket: list[str] = field(default_factory=list)
    # Cards Claude proposed for cut that match the user's protected list
    # ([metadata] Protect= entries + --protect CLI flags). Sliced before
    # ``cuts`` is returned, so the curator never wastes a slot on a pet
    # card the user locked. Surfaced so the iteration log + CLI summary
    # can show "Claude wanted to cut Goblin Lackey but you protected it."
    dropped_for_protection: list[str] = field(default_factory=list)
    # Populated by apply_proposal_to_deck. Empty until that call.
    applied_adds: list[str] = field(default_factory=list)
    applied_cuts: list[str] = field(default_factory=list)
    dropped_for_balance: list[str] = field(default_factory=list)
    padded_count: int = 0
    padded_breakdown: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# Threshold below which game-changers get filtered out. Comes from the WotC
# bracket guidelines: B1 (Exhibition), B2 (Core) — no game-changers allowed.
# B3 (Upgraded) and B4 (Optimized) permit up to 3; B5 (cEDH) is unbounded.
# We currently only enforce the binary 'allowed at all' line — the 3-card
# cap at B3/B4 is a follow-up.
_BRACKET_NO_GAME_CHANGERS_THRESHOLD = 3


def _load_game_changers() -> set[str]:
    """Return the WotC-designated game-changers set.

    Wrapping ``game_changers.load_game_changers()`` here gives tests a
    proposer-local symbol to monkeypatch without depending on the
    game_changers module's HTTP cache lifecycle. Production calls fall
    through to the real loader (disk-cached scrape of WotC's bracket
    guidelines page)."""
    from .game_changers import load_game_changers
    return load_game_changers()


def enforce_bracket_caps(
    adds: list[str], bracket: int,
) -> tuple[list[str], list[str]]:
    """Split ``adds`` into (kept, dropped) by the bracket cap rule.

    Below B3 (i.e. B1 + B2), game-changers are stripped from adds and
    returned separately so the caller can log them. At B3+ this is a
    no-op pass-through — game-changers are allowed and the WotC 3-card
    cap is enforced elsewhere (deck-level audit, not the curator).

    Card-name comparison is case-insensitive: the game-changers set
    holds the canonical Scryfall casing, but EDHREC scrape / Moxfield
    export sometimes vary, so we fold both sides before comparing.
    """
    if bracket >= _BRACKET_NO_GAME_CHANGERS_THRESHOLD:
        return list(adds), []

    gc_set = _load_game_changers()
    gc_lower = {g.lower() for g in gc_set}

    kept: list[str] = []
    dropped: list[str] = []
    for card in adds:
        if card.lower() in gc_lower:
            dropped.append(card)
        else:
            kept.append(card)
    return kept, dropped


_AUTO_PROPOSE_SYSTEM_PROMPT = """You are a Commander deck curator. The user
will give you a target bracket (1=Exhibition, 2=Core, 3=Upgraded,
4=Optimized, 5=cEDH), a deck's current contents, and a CANDIDATE POOL of
adds + cuts already filtered by the EDHREC heuristic advisor.

Your job: pick a small, applicable subset of those candidates that will
genuinely improve the deck at the target bracket. Respect these rules:

- Stay within color identity (do NOT add off-color cards).
- Honor the bracket: at B1/B2, avoid game-changers (the user's pipeline
  will strip them anyway, but don't waste a slot recommending them).
- Pick adds and cuts that pair — if you add a finisher, cut a redundant
  filler, not a key utility piece.
- NEVER propose cutting a card in the user's PROTECTED CARDS list. Those
  are locked pet cards; any cut you propose against them gets stripped
  by the post-filter and wastes a curator slot. Pick a different cut.
- Justify briefly. Two sentences max.

Return ONLY a JSON object with this exact shape:

  {"adds": ["Card 1", "Card 2", ...],
   "cuts": ["Cut 1", "Cut 2", ...],
   "rationale": "one or two sentences explaining the swap intent"}

No prose around the JSON. No code fences. Just the object.
"""


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
      deck_path        — .dck file; contents are sent to Claude as context.
      bracket          — target bracket (1-5). Drives game-changer
                         enforcement.
      advice_report    — dict shape from ``AdviceReport.to_manifest()``.
                         We feed the ``added`` + ``removed`` lists to
                         Claude as the candidate pool plus ``rationale``
                         for hint.
      max_adds         — hard cap on returned adds (default 5).
      max_cuts         — hard cap on returned cuts (default 5).
      model            — Anthropic model id (default sonnet-4-5).
      protected_cards  — names the user has locked against cuts. Listed
                         in the Claude prompt so the curator doesn't
                         waste a slot proposing to cut a pet card; any
                         that slip through anyway are post-filtered to
                         ``Proposal.dropped_for_protection``. Case-
                         insensitive match. Same pattern as bracket caps.
      mode             — curation intensity hint passed to the curator
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
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError(
            "auto_propose requires ANTHROPIC_API_KEY in the environment. "
            "Set it before invoking commander-iterate --auto-propose."
        )
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover — covered via stub
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
    # max_cuts) still clip the output; this block just nudges the
    # curator to pair more or fewer changes within that budget.
    _MODE_HINTS = {
        "polish": (
            "MODE: POLISH. Make conservative targeted swaps. Only pair "
            "your highest-confidence picks — leave the deck's identity "
            "intact. Prefer fewer changes if the deck is already close "
            "to ideal at this bracket."
        ),
        "overhaul": (
            "MODE: OVERHAUL. The user explicitly wants a substantial "
            "revision. Pair as many high-quality swaps as the cap "
            "allows; treat this as a major retune. Still respect color "
            "identity, bracket caps, and the protected list."
        ),
        "free": (
            "MODE: FREE. No specific intensity hint — pick the number "
            "of changes that genuinely improve the deck. If the deck "
            "is well-tuned already, ship a small proposal; if it has "
            "many misalignments, propose more."
        ),
    }
    mode_hint = _MODE_HINTS.get(mode, _MODE_HINTS["polish"])

    # Build a PROTECTED CARDS block in the user message so Claude knows
    # not to propose them for cut. Skip the block entirely when the
    # list is empty — keeps the prompt clean for the common case.
    protected_block = ""
    if protected_list:
        protected_block = (
            "PROTECTED CARDS (locked — NEVER propose for cut):\n"
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
        f"Pick up to {max_adds} adds and {max_cuts} cuts. "
        f"Return ONLY the JSON object."
    )

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

    cleaned = text.strip()
    # Tolerate Claude wrapping JSON in code fences despite the prompt.
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)
        cleaned = cleaned[1] if len(cleaned) > 1 else text
        # Strip optional language tag like ```json
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0].strip()

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"auto_propose: Claude returned unparseable JSON: {exc}. "
            f"Raw payload (first 200 chars): {text[:200]!r}"
        ) from exc

    raw_adds = list(payload.get("adds", []) or [])
    raw_cuts = list(payload.get("cuts", []) or [])
    rationale = str(payload.get("rationale", "")).strip()

    # Bracket-cap filter BEFORE applying max_adds so the cap counts only
    # bracket-allowed cards.
    kept_adds, dropped_for_bracket = enforce_bracket_caps(raw_adds, bracket)

    # Protection filter: strip any cut Claude proposed against the
    # user's locked list. Same pattern as bracket caps — done BEFORE
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
    )


# Filename version-bump regex. Matches optional `v<N>` immediately before
# an optional `[B<N>]` bracket suffix and the `.dck` extension:
#   [USER] Foo [B3].dck       → group1='[USER] Foo', v=None,  bracket='3'
#   [USER] Foo v3 [B3].dck    → group1='[USER] Foo', v='3',   bracket='3'
#   MyDeck.dck                → no match (handled by fallback path)
_VERSION_BRACKET_RE = _re.compile(
    r"^(?P<base>.+?)(?:\s+v(?P<ver>\d+))?\s+\[B(?P<bracket>\d+)\]\.dck$"
)
_VERSION_NO_BRACKET_RE = _re.compile(
    r"^(?P<base>.+?)(?:\s+v(?P<ver>\d+))?\.dck$"
)


def _bump_version_filename(name: str) -> str:
    """Compute the next-version filename for a .dck path.

    Rules:
      ``[USER] Foo [B3].dck``      → ``[USER] Foo v2 [B3].dck``
      ``[USER] Foo v3 [B3].dck``   → ``[USER] Foo v4 [B3].dck``
      ``MyDeck.dck``               → ``MyDeck v2.dck`` (no bracket suffix)

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

    # Last-resort fallback for paths that don't end in .dck — just
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
    (``[USER] Foo [B3].dck`` → ``[USER] Foo v2 [B3].dck``). Bracket
    suffix preserved as the last token.

    Deck-legality invariants — shared with the audit-endpoint path
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
    already enforces. The two flows are now in sync — same balancing,
    same padding, same legal output.

    Mutation rules within the file:
      - [metadata], [Commander], and other sections pass through.
      - [Main] is rebuilt: drop any line whose card name matches a
        kept cut (case-insensitive); append `1 <name>` lines for each
        kept add.
      - Card-line matching handles edition codes (``|CLB|871``).
    """
    # Lazy import — proposer.py is library-level, web/_helpers is the
    # web layer's internal helpers module. The helpers themselves are
    # pure Python (no Flask coupling) so the import is clean, but
    # importing at module-load time would create a circular risk if
    # the web layer ever imports proposer.
    from ._advisor_models import SwapRecommendation
    from .web._helpers import _apply_swaps_to_dck, _pad_main_to_99

    out_path = src_path.parent / _bump_version_filename(src_path.name)

    # Build SwapRecommendation-shape objects so _apply_swaps_to_dck
    # accepts them. The helper only reads .card and .action — the
    # reason field is unused on this path but required by the dataclass.
    #
    # Defense-in-depth: even if auto_propose's protection filter let
    # something through (e.g. proposal was hand-constructed or came
    # from a test path), strip protected cuts here too. The protect
    # list lives in the deck's [metadata] section so we read it
    # straight from disk — no extra arg-passing needed.
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
# commander-auto-curate CLI — unattended advisor → curator → apply pipeline
# ---------------------------------------------------------------------------

def auto_curate_main(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``commander-auto-curate``.

    End-to-end unattended pipeline:
      1. Run the improvement advisor on the deck (sync EDHREC fetch).
      2. Hand the AdviceReport to ``auto_propose()`` so Claude curates
         it down to a small, applicable proposal.
      3. Apply the proposal to a new versioned .dck file (or print it
         under ``--dry-run`` and leave disk untouched).
      4. Print a summary the user can scan when the overnight batch
         lands.

    Exits non-zero with a clear message on missing key, missing SDK,
    or unparseable Claude response so a batch driver can skip the
    deck rather than misinterpret silent zero-changes as success.
    """
    import argparse

    p = argparse.ArgumentParser(
        prog="commander-auto-curate",
        description=(
            "Run advisor → Claude curator → apply, all in one go. "
            "Designed for unattended overnight batch refinement."
        ),
    )
    p.add_argument("deck_path", type=Path, help="Path to the .dck file to audit.")
    p.add_argument("--bracket", type=int, required=True,
                   help="Target bracket (1-5). Drives game-changer enforcement.")
    p.add_argument(
        "--mode", choices=["polish", "overhaul", "free"], default="polish",
        help=(
            "Curation intensity preset (default 'polish'). "
            "polish=5 adds + 5 cuts (safe for unattended overnight runs). "
            "overhaul=15 + 15 (deliberate major revision). "
            "free=unbounded (trust Claude to pick the right count). "
            "Override individual caps with --max-adds / --max-cuts."
        ),
    )
    # Defaults are None so we can tell whether the user passed an
    # explicit cap (which overrides the mode preset) or left it at
    # the mode's recommended value. argparse-of-the-classics: a
    # sentinel beats reading sys.argv directly.
    p.add_argument("--max-adds", type=int, default=None,
                   help="Hard cap on returned adds. Overrides --mode's "
                        "add cap when set. Default: preset value for "
                        "the active --mode (polish=5, overhaul=15, "
                        "free=999).")
    p.add_argument("--max-cuts", type=int, default=None,
                   help="Hard cap on returned cuts. Same override "
                        "semantics as --max-adds.")
    p.add_argument("--source", default="heuristic",
                   choices=["heuristic", "bracket_peers", "claude"],
                   help="Advisor backend (default heuristic).")
    p.add_argument("--model", default="claude-sonnet-4-5",
                   help="Anthropic model id for the curator step.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the proposal but don't write the new .dck.")
    p.add_argument("--json", action="store_true",
                   help="Emit the proposal as JSON on stdout instead "
                        "of human-readable summary.")
    p.add_argument("--no-log", action="store_true",
                   help="Skip writing the iteration to knowledge_log "
                        "(default: persist a pending iteration row).")
    p.add_argument("--db-path",
                   help="Override the knowledge_log SQLite path "
                        "(default: vendor/knowledge_log.sqlite).")
    p.add_argument("--protect", action="append", default=[],
                   metavar="CARD",
                   help="Lock a card against cuts. Repeatable. Unioned "
                        "with [metadata] Protect= entries in the .dck "
                        "and any --protect-from file.")
    p.add_argument("--protect-from", default=None, metavar="PATH",
                   help="Path to a file with one card name per line, "
                        "all protected against cuts. Unioned with "
                        "--protect and [metadata] Protect=.")
    args = p.parse_args(argv)

    if not args.deck_path.exists():
        print(f"ERROR: deck not found: {args.deck_path}", flush=True)
        return 2
    if not (1 <= args.bracket <= 5):
        print(f"ERROR: bracket must be 1-5, got {args.bracket}", flush=True)
        return 2

    # Resolve effective caps from the mode preset + any explicit
    # overrides. The preset is the discoverable default ("I want a
    # polish run / overhaul / let Claude decide") and the explicit
    # flags are the fine-tune for users who want a specific number.
    _MODE_CAPS = {
        "polish":   (5,   5),    # conservative; safe for unattended
        "overhaul": (15,  15),   # deliberate major revision
        "free":     (999, 999),  # effectively unbounded
    }
    preset_adds, preset_cuts = _MODE_CAPS[args.mode]
    effective_max_adds = args.max_adds if args.max_adds is not None else preset_adds
    effective_max_cuts = args.max_cuts if args.max_cuts is not None else preset_cuts
    if effective_max_adds < 0 or effective_max_cuts < 0:
        print(
            f"ERROR: --max-adds / --max-cuts must be non-negative, "
            f"got adds={effective_max_adds} cuts={effective_max_cuts}",
            flush=True,
        )
        return 2
    if not args.json:
        if (args.max_adds is None and args.max_cuts is None):
            print(
                f"      mode={args.mode!r} → up to {effective_max_adds} "
                f"adds and {effective_max_cuts} cuts",
                flush=True,
            )
        else:
            print(
                f"      mode={args.mode!r} + overrides → "
                f"max adds={effective_max_adds}, max cuts={effective_max_cuts}",
                flush=True,
            )

    # Step 1: advisor. Imported lazily so the CLI startup stays cheap when
    # the user only wanted --help.
    from .improvement_advisor import advise
    if not args.json:
        print(f"[1/3] Running advisor on {args.deck_path.name} (B{args.bracket})...",
              flush=True)
    report = advise(
        deck_path=args.deck_path,
        bracket=args.bracket,
        source=args.source,
    )
    advice_dict = report.to_manifest()
    candidate_add_count = len(advice_dict.get("added", []))
    candidate_cut_count = len(advice_dict.get("removed", []))
    if not args.json:
        print(f"      advisor produced {candidate_add_count} candidate adds, "
              f"{candidate_cut_count} candidate cuts", flush=True)

    # Resolve the protected-cards set from all three sources:
    #   - [metadata] Protect= entries in the .dck (persistent, per-deck)
    #   - --protect CLI flag (repeatable, ad-hoc override)
    #   - --protect-from <file> (bulk reusable list)
    # Order-preserving union so the prompt + summary read in a stable
    # order; case-insensitive dedup.
    from .web._helpers import read_protected_cards
    protected_combined: list[str] = []
    seen_lower: set[str] = set()
    def _add_protected(name: str) -> None:
        n = name.strip()
        if not n:
            return
        key = n.lower()
        if key in seen_lower:
            return
        seen_lower.add(key)
        protected_combined.append(n)

    deck_text_for_protect = args.deck_path.read_text(encoding="utf-8")
    for c in read_protected_cards(deck_text_for_protect):
        _add_protected(c)
    for c in args.protect:
        _add_protected(c)
    if args.protect_from:
        pf = Path(args.protect_from)
        if not pf.exists():
            print(f"ERROR: --protect-from file not found: {pf}", flush=True)
            return 2
        for line in pf.read_text(encoding="utf-8").splitlines():
            _add_protected(line)

    if not args.json and protected_combined:
        print(f"      {len(protected_combined)} protected cards locked "
              f"against cuts", flush=True)

    # Step 2: curator.
    if not args.json:
        print(f"[2/3] Curating via {args.model}...", flush=True)
    try:
        proposal = auto_propose(
            deck_path=args.deck_path,
            bracket=args.bracket,
            advice_report=advice_dict,
            max_adds=effective_max_adds,
            max_cuts=effective_max_cuts,
            model=args.model,
            protected_cards=protected_combined,
            mode=args.mode,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 3

    # Step 3: apply (or dry-run).
    if not args.json:
        verb = "would write" if args.dry_run else "writing"
        print(f"[3/3] {verb} new .dck...", flush=True)
    out_path = apply_proposal_to_deck(
        args.deck_path, proposal, dry_run=args.dry_run,
    )

    # Step 3b: persist iteration to knowledge_log. Skipped on dry-run
    # (no actual deck to point at) and on --no-log (opt-out). Failures
    # to write the log are NON-fatal — the .dck is already on disk;
    # the user shouldn't lose that work because of a knowledge_log
    # quirk. Surface the failure in the summary instead.
    iteration_id: Optional[int] = None
    log_error: Optional[str] = None
    if not args.dry_run and not args.no_log:
        try:
            iteration_id = _log_auto_curate_iteration(
                src_deck_path=args.deck_path,
                new_deck_path=out_path,
                bracket=args.bracket,
                proposal=proposal,
                db_path=Path(args.db_path) if args.db_path else None,
            )
        except Exception as exc:  # noqa: BLE001
            log_error = f"{type(exc).__name__}: {exc}"

    if args.json:
        print(json.dumps({
            "input_deck": str(args.deck_path),
            "output_deck": str(out_path),
            "dry_run": args.dry_run,
            "mode": args.mode,
            "max_adds": effective_max_adds,
            "max_cuts": effective_max_cuts,
            "proposal": proposal.to_dict(),
            "iteration_id": iteration_id,
            "log_error": log_error,
        }, indent=2))
        return 0

    print()
    # Surface what Claude REQUESTED vs what actually LANDED. The two
    # can differ when adds and cuts are unbalanced — apply_proposal_
    # to_deck slices both to min() so the deck stays the right size.
    print(f"Adds requested ({len(proposal.adds)}) → applied ({len(proposal.applied_adds)}):")
    for c in proposal.applied_adds:
        print(f"  + {c}")
    print(f"Cuts requested ({len(proposal.cuts)}) → applied ({len(proposal.applied_cuts)}):")
    for c in proposal.applied_cuts:
        print(f"  - {c}")
    if proposal.dropped_for_bracket:
        print(f"Dropped for B{args.bracket} (game-changers): "
              f"{len(proposal.dropped_for_bracket)}")
        for c in proposal.dropped_for_bracket:
            print(f"  ! {c}")
    if proposal.dropped_for_protection:
        print(f"Dropped because protected (user-locked): "
              f"{len(proposal.dropped_for_protection)}")
        for c in proposal.dropped_for_protection:
            print(f"  🔒 {c}")
    if proposal.dropped_for_balance:
        print(f"Dropped to keep deck size legal "
              f"(adds/cuts unbalanced): {len(proposal.dropped_for_balance)}")
        for c in proposal.dropped_for_balance:
            print(f"  ~ {c}")
    if proposal.padded_count:
        breakdown_str = ", ".join(
            f"{n}× {b}" for b, n in proposal.padded_breakdown.items()
        )
        print(f"Padded with basics: +{proposal.padded_count} ({breakdown_str})")
    print()
    print(f"Rationale: {proposal.rationale}")
    print()
    if args.dry_run:
        print(f"DRY RUN — would have written: {out_path}")
    else:
        print(f"Wrote: {out_path}")
        if iteration_id is not None:
            print(f"Logged iteration #{iteration_id} (pending)")
        elif args.no_log:
            print("(skipped knowledge_log per --no-log)")
        elif log_error:
            # Non-fatal: deck is on disk, history just lost this row.
            print(f"WARN: knowledge_log write failed: {log_error}")
    return 0


def _log_auto_curate_iteration(
    src_deck_path: Path,
    new_deck_path: Path,
    bracket: int,
    proposal: "Proposal",
    db_path: Optional[Path] = None,
) -> int:
    """Persist a 'pending' Iteration row recording this auto-curate run.

    Reads the moxfield publicId out of the new .dck (falls back to the
    filename stem). Hooks the new row's parent_id to the most recent
    prior iteration of the same deck so the iteration chain stays
    threaded — important for the upcoming knowledge_log graph view.

    Verdict is 'pending' — we haven't actually played the new deck yet.
    Phase 2's analyst path (or a follow-up Forge sim) updates verdict
    + sim_report once results land.
    """
    from .iteration_loop import resolve_deck_id
    from .knowledge_log import (
        DEFAULT_DB_PATH,
        Iteration,
        iterations_for_deck,
        record_iteration,
    )

    effective_db = db_path or DEFAULT_DB_PATH

    deck_id = resolve_deck_id(new_deck_path, fallback=new_deck_path.stem)
    deck_name = new_deck_path.stem

    # Thread the iteration chain: find the latest existing iteration for
    # this deck_id and set it as parent. If none exists, parent_id stays
    # None (this becomes v1 in the log).
    prior = iterations_for_deck(deck_id, db_path=effective_db)
    parent_id = prior[-1].id if prior else None

    deck_snapshot = new_deck_path.read_text(encoding="utf-8")
    # Record what ACTUALLY LANDED in the .dck — these are the changes
    # that produced the new deck snapshot. ``requested_*`` fields
    # preserve Claude's intent for analysis (which adds did the curator
    # want but balancing dropped?) without conflating the two.
    audit_manifest = {
        "added": list(proposal.applied_adds),
        "removed": list(proposal.applied_cuts),
        "rationale": proposal.rationale,
        "source": proposal.source,
        "dropped_for_bracket": list(proposal.dropped_for_bracket),
        "dropped_for_protection": list(proposal.dropped_for_protection),
        "dropped_for_balance": list(proposal.dropped_for_balance),
        "padded_count": proposal.padded_count,
        "padded_breakdown": dict(proposal.padded_breakdown),
        "requested_adds": list(proposal.adds),
        "requested_cuts": list(proposal.cuts),
        "src_deck": src_deck_path.name,
    }

    it = Iteration(
        deck_id=deck_id,
        deck_name=deck_name,
        bracket=bracket,
        parent_id=parent_id,
        audit_version="claude-auto",
        audit_manifest=audit_manifest,
        verdict="pending",
        deck_snapshot=deck_snapshot,
    )
    return record_iteration(it, db_path=effective_db)


if __name__ == "__main__":
    raise SystemExit(auto_curate_main())

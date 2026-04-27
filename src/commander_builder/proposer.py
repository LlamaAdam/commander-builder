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

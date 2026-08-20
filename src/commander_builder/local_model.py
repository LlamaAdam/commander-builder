"""Local-model tier — narrow classification tasks with the oracle text SUPPLIED.

WHAT THIS IS (and what the previous local path got wrong)
=========================================================
The repo has shipped two "local Ollama" functions since the analyst /
proposer split: ``analyst.ollama_verdict`` and ``proposer.ollama_propose``.
Both were dead code — nothing in ``src/`` ever set ``use_ollama`` — and
``ollama_propose`` in particular handed a 3B local model the 706-line
``prompts/moxfield_audit_v3.md`` (a browser-workflow prompt written for
Claude, opening with "STEP 0 — ASK ME FIRST") and expected a complete swap
manifest back. That is not a tuning problem; it is a category error. A
small local model asked to recall the Commander card pool from memory
fabricates.

Decision A4 retires that ambition and replaces it with the opposite
shape:

  * **Narrow tasks only.** One card, one label. One deck's derived
    signals, one label. Never "propose swaps", never "render a verdict".
  * **Recall is never required.** Every prompt CARRIES the oracle text /
    signal set the answer depends on. The model reads supplied text and
    picks from a closed list; it is never asked what a card does.
  * **A closed taxonomy, validated locally.** The answer must be a member
    of the EXISTING taxonomy (``staples`` roles, ``archetype`` labels).
    Anything else is a malformed response, not a new category.
  * **Every task has a deterministic fallback that already ships.** The
    local tier is an optional second opinion on top of
    ``staples.classify_role_extended`` / ``archetype``'s v2 signal
    classifier — both of which stay the default and stay the answer
    whenever the local tier cannot produce a valid one.

FAILURE CONTRACT (two classes, deliberately different)
======================================================
``LocalModelUnavailable`` — CONFIGURATION is wrong: the daemon is not
    running, or the configured model was never pulled. Raised by
    :meth:`LocalModelClient.preflight` with the exact ``ollama pull ...``
    command to run. Loud on purpose: the operator explicitly opted in
    with ``COMMANDER_BUILDER_LOCAL_MODEL``, and the negative-mode review
    found that the old path turned exactly this case into a silent
    degrade (``HTTPError`` is a subclass of ``URLError``, so a 404 for a
    not-pulled model became "daemon not reachable" and then a quiet
    fall-through).

``None`` — the CALL failed: timeout, transport error, empty body,
    unparseable JSON, or a syntactically fine answer that is not in the
    taxonomy. Callers degrade to the deterministic classifier. A local
    model answer NEVER becomes a silent default, and a rejected answer is
    never "close enough" — it is discarded.

The convenience routers (:func:`role_for_card`, :func:`archetype_for_deck`)
catch the configuration error too, print one WARN, and degrade — a
misconfigured optional tier must not break a pipeline mid-run.

OPT-IN
======
Off by default. ``COMMANDER_BUILDER_LOCAL_MODEL=1`` turns it on, same
truthy spelling as ``card_score.is_enabled`` /
``change_budget.rebuild_tier_enabled``. ``COMMANDER_BUILDER_LOCAL_MODEL_NAME``
and ``COMMANDER_BUILDER_LOCAL_MODEL_URL`` override the model / endpoint
(the ``ProposerConfig.ollama_model`` / ``ollama_url`` pair, promoted from
dataclass-only defaults to real configuration).

IS IT WORTH USING? UNKNOWN — MEASURE IT
=======================================
No accuracy claim is made anywhere in this module. :func:`role_agreement`
and :func:`archetype_agreement` (and the ``agreement`` subcommand of
``python -m commander_builder.local_model``) exist so that question can be
answered with numbers before anything downstream is allowed to depend on
the local tier.

TASK REGISTRY
=============
Each task owns its prompt, its JSON schema, and its validation::

    LocalTask(name, description, build_prompt, schema, validate)

registered into :data:`TASKS` and run via ``client.run(name, **inputs)``.
Adding a task means adding one ``LocalTask`` — no changes to the client,
the failure handling, or the CLI.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence, get_args

from . import archetype as _archetype
from . import staples as _staples
from ._llm_json import try_extract_json_object

# ---------------------------------------------------------------------------
# Configuration surface
# ---------------------------------------------------------------------------

#: Master opt-in. Default OFF is load-bearing: the local tier is
#: unvalidated machinery (see the agreement harness below), and the
#: deterministic classifiers it shadows are the shipped answer.
LOCAL_MODEL_ENV_VAR = "COMMANDER_BUILDER_LOCAL_MODEL"
LOCAL_MODEL_NAME_ENV_VAR = "COMMANDER_BUILDER_LOCAL_MODEL_NAME"
LOCAL_MODEL_URL_ENV_VAR = "COMMANDER_BUILDER_LOCAL_MODEL_URL"

#: Matches the historical ``ProposerConfig.ollama_model`` default. A 3B
#: model is a defensible default HERE precisely because these tasks
#: supply their own evidence — the opposite of the retired proposal path,
#: where MODEL_GUIDE.md's larger recommendations applied because the
#: model had to recall the card pool.
DEFAULT_MODEL = "llama3.2:3b"
DEFAULT_BASE_URL = "http://localhost:11434"

#: One card / one signal-set per call — a slow local generate is a
#: broken one, not a patient one. The retired proposal path used 600s.
DEFAULT_TIMEOUT_SEC = 30.0
PREFLIGHT_TIMEOUT_SEC = 5.0

#: One extra attempt, mirroring ``edhrec_client``'s "retry transport and
#: 5xx, never retry a caller-bug 4xx" policy at a much smaller scale.
DEFAULT_MAX_RETRIES = 1
RETRY_SLEEP_SEC = 0.5
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

#: Oracle text is supplied in full up to this many characters. No real
#: card comes close; the cap exists so a malformed lookup can't push a
#: multi-KB blob into a small model's context.
MAX_ORACLE_CHARS = 1200

_API_SUFFIXES = ("/api/generate", "/api/chat", "/api/tags")


def is_enabled() -> bool:
    """True when the operator has opted into the local-model tier.

    Same truthy-value convention as ``card_score.is_enabled`` /
    ``change_budget.rebuild_tier_enabled`` — one shared spelling of "is
    this flag on" across every opt-in in the codebase.
    """
    return os.environ.get(
        LOCAL_MODEL_ENV_VAR, "",
    ).strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class LocalModelConfig:
    """Endpoint + model + timing knobs for the local tier.

    ``base_url`` accepts either a bare origin (``http://localhost:11434``)
    or a full Ollama endpoint URL (``.../api/generate``) so a value
    carried over from ``ProposerConfig.ollama_url`` still works; the
    suffix is normalized away and the two endpoints are derived.
    """
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    preflight_timeout_sec: float = PREFLIGHT_TIMEOUT_SEC
    max_retries: int = DEFAULT_MAX_RETRIES

    def __post_init__(self) -> None:
        url = (self.base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        for suffix in _API_SUFFIXES:
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        object.__setattr__(self, "base_url", url or DEFAULT_BASE_URL)
        object.__setattr__(self, "model", (self.model or DEFAULT_MODEL).strip())

    @property
    def generate_url(self) -> str:
        return f"{self.base_url}/api/generate"

    @property
    def tags_url(self) -> str:
        return f"{self.base_url}/api/tags"

    @classmethod
    def from_env(cls) -> "LocalModelConfig":
        return cls(
            model=os.environ.get(LOCAL_MODEL_NAME_ENV_VAR, "").strip() or DEFAULT_MODEL,
            base_url=os.environ.get(LOCAL_MODEL_URL_ENV_VAR, "").strip() or DEFAULT_BASE_URL,
        )


class LocalModelUnavailable(RuntimeError):
    """The local tier is opted-in but not usable — daemon down, or the
    configured model is not pulled. Message always names the fix."""


# ---------------------------------------------------------------------------
# Taxonomies — IMPORTED, never copied
# ---------------------------------------------------------------------------
#
# ``archetype`` keeps ``bracket_estimator._TUTOR_CARDS`` in sync by
# importing it rather than restating it; same rule here. A local model
# that answers outside these tuples is malformed by definition, so the
# tuples must BE the shipped taxonomy, not a snapshot of it that can
# drift into rejecting valid roles.

#: Every label ``staples.classify_role_extended`` can return: the base
#: ``_ROLE_PATTERNS`` roles, plus the TWO the classifier produces without a
#: pattern table (``threat`` for an unmatched creature, ``other`` for no
#: match) and the two extended buckets (``land_payoff``,
#: ``win_condition``). Count corrected 2026-08-20 (R2-P25b): this said
#: "the three ... " and then listed two.
#:
#: Those four names are a hand-maintained SNAPSHOT — the honest caveat on
#: the "IMPORTED, never copied" heading above, which holds for the
#: pattern-table roles only. Nothing in ``staples`` exports them as a
#: list, so a new pattern-free bucket added there must be added here too.
#: The net under that: tests/test_local_model.py classifies the whole
#: real-oracle fixture corpus and fails if any role it produces is
#: missing from this tuple.
ROLE_TAXONOMY: tuple[str, ...] = tuple(dict.fromkeys(
    [role for role, _patterns in _staples._ROLE_PATTERNS_COMPILED]
    + ["threat", "other", "land_payoff", "win_condition"]
))

#: ``archetype.Archetype`` is a ``Literal``, so this needs no curation.
ARCHETYPE_TAXONOMY: tuple[str, ...] = tuple(get_args(_archetype.Archetype))


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocalTask:
    """One narrow local-model task.

    ``build_prompt(**inputs) -> str`` — SHORT, purpose-written, and it
        must embed every piece of evidence the answer depends on.
    ``schema`` — JSON Schema handed to Ollama's structured-output
        ``format`` field. An optimization, never a guarantee: not every
        model / daemon version honors it, so ``validate`` re-checks
        everything the schema claims.
    ``validate(obj) -> Optional[Any]`` — the parsed object in, the
        normalized answer out, or ``None`` for "malformed". Returning
        ``None`` must be the response to anything unexpected, including a
        well-formed answer outside the taxonomy.
    """
    name: str
    description: str
    build_prompt: Callable[..., str]
    schema: dict
    validate: Callable[[dict], Optional[Any]]


TASKS: dict[str, LocalTask] = {}


def register_task(task: LocalTask) -> LocalTask:
    """Register a task (idempotent by name; re-registration replaces)."""
    TASKS[task.name] = task
    return task


def get_task(name: str) -> LocalTask:
    try:
        return TASKS[name]
    except KeyError:
        raise KeyError(
            f"unknown local-model task {name!r}; registered: "
            f"{', '.join(sorted(TASKS)) or '(none)'}"
        ) from None


def task_names() -> tuple[str, ...]:
    return tuple(sorted(TASKS))


def _enum_schema(key: str, values: Sequence[str]) -> dict:
    """One-key object schema with a closed value set."""
    return {
        "type": "object",
        "properties": {key: {"type": "string", "enum": list(values)}},
        "required": [key],
        "additionalProperties": False,
    }


def _validate_enum(obj: dict, key: str, allowed: Sequence[str]) -> Optional[str]:
    """Pull ``key`` out of a parsed response, or ``None`` if anything is off.

    Rejects: a non-dict, a missing key, a non-string value, and — the
    case the schema is supposed to prevent but cannot be trusted to —
    a value outside the taxonomy. Whitespace and case are normalized
    because "Ramp\\n" is the same answer as "ramp"; nothing else is.
    """
    if not isinstance(obj, dict):
        return None
    value = obj.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
    if normalized not in allowed:
        return None
    return normalized


# --- Task 1: role tagging --------------------------------------------------

_ROLE_PROMPT = """\
You are tagging one Magic: the Gathering card with exactly one role.

Allowed roles (choose exactly one, copied verbatim):
{roles}

Rules:
- Decide ONLY from the card text below. Do not use outside knowledge.
- Any card whose type line contains "Land" is "land" (or "ramp" if it
  searches a land out of the library).
- A creature with no other strong effect is "threat".
- If nothing fits, answer "other". Do not invent a role.

Type line: {type_line}
Oracle text:
{oracle_text}

Answer with JSON only, no prose: {{"role": "<one role from the list>"}}
"""


def _build_role_prompt(*, oracle_text: str = "", type_line: str = "") -> str:
    text = (oracle_text or "").strip()[:MAX_ORACLE_CHARS] or "(no oracle text)"
    return _ROLE_PROMPT.format(
        roles="\n".join(f"- {r}" for r in ROLE_TAXONOMY),
        type_line=(type_line or "(unknown)").strip(),
        oracle_text=text,
    )


ROLE_TASK = register_task(LocalTask(
    name="role_tag",
    description=(
        "One card's oracle text + type line -> one role from "
        "staples.classify_role_extended's taxonomy."
    ),
    build_prompt=_build_role_prompt,
    schema=_enum_schema("role", ROLE_TAXONOMY),
    validate=lambda obj: _validate_enum(obj, "role", ROLE_TAXONOMY),
))


# --- Task 2: archetype tagging ---------------------------------------------

_ARCHETYPE_PROMPT = """\
You are tagging one Magic: the Gathering Commander deck with exactly one
archetype.

Allowed archetypes (choose exactly one, copied verbatim):
{archetypes}

Rules:
- Decide ONLY from the measured signals below. Do not use outside
  knowledge about any card or commander.
- A deck with a known game-ending combo is "combo".
- A deck with several resource-denial lock pieces is "stax".
- A deck whose answer suite (counterspells, board wipes) dominates is
  "control".
- A deck that is mostly cheap creatures is "aggro".
- Otherwise answer "midrange". "midrange" is the honest default, not a
  failure.

Deck: {deck_name}
Measured signals:
{signals}
{extra}
Answer with JSON only, no prose: {{"archetype": "<one archetype from the list>"}}
"""

#: Signal keys handed to the model, in prompt order, with the plain-English
#: label each gets. Deliberately a SUBSET of
#: ``archetype.derive_archetype_signals``: bookkeeping fields
#: (``oracle_coverage``, ``label``) are either noise or the deterministic
#: answer itself, and feeding the deterministic label into the prompt
#: would make every agreement measurement meaningless.
_ARCHETYPE_SIGNAL_LABELS: tuple[tuple[str, str], ...] = (
    ("game_ending_combos", "game-ending combos detected"),
    ("tutors", "tutor cards"),
    ("stax_cards", "resource-denial (stax) cards"),
    ("stack_count", "counterspells / stack interaction"),
    ("wipe_count", "board wipes"),
    ("instant_share", "share of spells that are instants"),
    ("creature_share", "share of the deck that is creatures"),
    ("avg_cmc", "average mana value"),
    ("tribal_type", "dominant creature type"),
)


def _format_signals(signals: dict) -> str:
    lines = []
    for key, label in _ARCHETYPE_SIGNAL_LABELS:
        value = (signals or {}).get(key)
        # None means "could not be derived" (cold oracle cache), which is
        # NOT the same as zero — say so rather than implying a count.
        rendered = "unknown" if value is None else value
        lines.append(f"- {label}: {rendered}")
    return "\n".join(lines)


def _build_archetype_prompt(
    *,
    signals: Optional[dict] = None,
    deck_name: str = "",
    sample_cards: Optional[Sequence[str]] = None,
) -> str:
    extra = ""
    if sample_cards:
        names = ", ".join(str(n) for n in list(sample_cards)[:20])
        extra = f"Representative cards: {names}\n"
    return _ARCHETYPE_PROMPT.format(
        archetypes="\n".join(f"- {a}" for a in ARCHETYPE_TAXONOMY),
        deck_name=(deck_name or "(unnamed)").strip(),
        signals=_format_signals(signals or {}),
        extra=extra,
    )


ARCHETYPE_TASK = register_task(LocalTask(
    name="archetype_tag",
    description=(
        "One deck's derived oracle signals -> one archetype from "
        "archetype.Archetype."
    ),
    build_prompt=_build_archetype_prompt,
    schema=_enum_schema("archetype", ARCHETYPE_TAXONOMY),
    validate=lambda obj: _validate_enum(obj, "archetype", ARCHETYPE_TAXONOMY),
))


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

def _warn(message: str) -> None:
    print(f"  WARN: local model: {message}", flush=True)


def _model_names(payload: Any) -> set[str]:
    """Model names from an ``/api/tags`` body, defensively."""
    names: set[str] = set()
    if not isinstance(payload, dict):
        return names
    for entry in payload.get("models") or []:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("model")
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
        elif isinstance(entry, str) and entry.strip():
            names.add(entry.strip())
    return names


def _model_is_pulled(model: str, available: Iterable[str]) -> bool:
    """Tag-tolerant membership test.

    ``ollama pull llama3.2`` lands as ``llama3.2:latest`` in the tag
    list, and a user who configured ``llama3.2`` means that model. An
    explicit tag (``llama3.2:3b``) must match exactly — silently using a
    different quantization than the one configured would make any
    agreement measurement unreproducible.
    """
    wanted = (model or "").strip()
    have = {n.strip() for n in available}
    if wanted in have:
        return True
    if ":" not in wanted:
        return f"{wanted}:latest" in have
    return False


class LocalModelClient:
    """Thin client for an Ollama-compatible local endpoint.

    Construct once per run: :meth:`preflight` result is cached on the
    instance, so tagging 100 cards costs one ``/api/tags`` round trip
    rather than 100.
    """

    def __init__(
        self,
        config: Optional[LocalModelConfig] = None,
        *,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config = config or LocalModelConfig.from_env()
        # Resolved at call time (not bound here) when None, so tests that
        # monkeypatch ``urllib.request.urlopen`` work the same way they do
        # for every other HTTP path in this repo.
        self._opener = opener
        self._preflight_ok = False
        #: Per-call outcome tally — the agreement harness reports it so a
        #: low agreement number can be read apart from a low answer rate.
        self.calls = 0
        self.failures = 0

    # -- transport ---------------------------------------------------------

    def _open(self, req, timeout: float):
        opener = self._opener or urllib.request.urlopen
        return opener(req, timeout=timeout)

    def _get_json(self, url: str, *, timeout: float) -> Any:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with self._open(req, timeout) as resp:
            return json.loads(resp.read())

    def _post_generate(self, prompt: str, schema: Optional[dict]) -> Optional[str]:
        """POST one generate call; return the raw response text or None.

        Retries transport failures and retryable 5xx/429 once (see
        ``DEFAULT_MAX_RETRIES``). A deterministic 4xx is never retried —
        it means the request itself is wrong.
        """
        body = json.dumps({
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
            # Structured output when the daemon supports it. ``validate``
            # never trusts it; see LocalTask.schema.
            "format": schema if schema else "json",
            # Classification, not prose: no reason to sample.
            "options": {"temperature": 0},
        }).encode("utf-8")

        attempts = max(1, self.config.max_retries + 1)
        for attempt in range(attempts):
            req = urllib.request.Request(
                self.config.generate_url, data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                with self._open(req, self.config.timeout_sec) as resp:
                    payload = json.loads(resp.read())
                text = payload.get("response") if isinstance(payload, dict) else None
                if not text:
                    raise ValueError("empty response body")
                return text
            except urllib.error.HTTPError as exc:
                # HTTPError BEFORE URLError: it is a subclass, and
                # conflating the two is exactly the bug that made a
                # not-pulled model read as "daemon not reachable".
                if exc.code not in _RETRYABLE_HTTP_CODES or attempt == attempts - 1:
                    _warn(
                        f"{self.config.model}: HTTP {exc.code} from "
                        f"{self.config.generate_url} ({exc.reason}); "
                        f"using the deterministic classifier instead."
                    )
                    return None
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    http.client.HTTPException, ValueError, json.JSONDecodeError) as exc:
                if attempt == attempts - 1:
                    _warn(
                        f"{self.config.model}: call failed after {attempts} "
                        f"attempt(s) ({type(exc).__name__}: {exc}); using the "
                        f"deterministic classifier instead."
                    )
                    return None
            time.sleep(RETRY_SLEEP_SEC)
        return None

    # -- preflight ---------------------------------------------------------

    def preflight(self, *, force: bool = False) -> None:
        """Verify the daemon answers AND the configured model is pulled.

        Raises :class:`LocalModelUnavailable` with the exact command to
        run. Cached after the first success unless ``force=True``.
        """
        if self._preflight_ok and not force:
            return
        try:
            payload = self._get_json(
                self.config.tags_url, timeout=self.config.preflight_timeout_sec,
            )
        except urllib.error.HTTPError as exc:
            raise LocalModelUnavailable(
                f"Ollama daemon at {self.config.base_url} answered HTTP "
                f"{exc.code} ({exc.reason}) for {self.config.tags_url}. That is "
                f"a reachable-but-wrong endpoint, not a missing daemon — check "
                f"{LOCAL_MODEL_URL_ENV_VAR}."
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException, ValueError, json.JSONDecodeError) as exc:
            raise LocalModelUnavailable(
                f"Ollama daemon not reachable at {self.config.base_url} "
                f"({type(exc).__name__}: {exc}). Start it with `ollama serve`, "
                f"point {LOCAL_MODEL_URL_ENV_VAR} at the right host, or unset "
                f"{LOCAL_MODEL_ENV_VAR} to stay on the deterministic "
                f"classifiers."
            ) from exc

        available = _model_names(payload)
        if not _model_is_pulled(self.config.model, available):
            have = ", ".join(sorted(available)) or "(none)"
            raise LocalModelUnavailable(
                f"model {self.config.model!r} is not pulled on the Ollama "
                f"daemon at {self.config.base_url} (pulled models: {have}). "
                f"Run: ollama pull {self.config.model}   — or set "
                f"{LOCAL_MODEL_NAME_ENV_VAR} to one of the models above."
            )
        self._preflight_ok = True

    # -- task execution ----------------------------------------------------

    def run(self, task_name: str, **inputs: Any) -> Optional[Any]:
        """Run one registered task. Returns the validated answer or None.

        ``None`` covers every per-call failure: transport, timeout, empty
        body, unparseable JSON, and a parseable answer that is not in the
        task's taxonomy. Only :class:`LocalModelUnavailable` (bad
        configuration) propagates.
        """
        task = get_task(task_name)
        self.preflight()
        self.calls += 1

        prompt = task.build_prompt(**inputs)
        raw = self._post_generate(prompt, task.schema)
        if raw is None:
            self.failures += 1
            return None

        parsed = try_extract_json_object(raw)
        if parsed is None:
            self.failures += 1
            _warn(
                f"{task.name}: response was not a JSON object "
                f"({len(raw)} chars — prose or truncated); discarding."
            )
            return None

        try:
            value = task.validate(parsed)
        except Exception:  # noqa: BLE001 — a validator must never crash a run
            value = None
        if value is None:
            self.failures += 1
            _warn(
                f"{task.name}: response {parsed!r} is not a valid answer "
                f"(outside the taxonomy or wrong shape); discarding."
            )
            return None
        return value


# ---------------------------------------------------------------------------
# Convenience routers — local tier first, deterministic answer always
# ---------------------------------------------------------------------------

def _resolve_client(
    client: Optional[LocalModelClient], enabled: Optional[bool],
) -> Optional[LocalModelClient]:
    """The client to use, or None when the tier is off.

    ``enabled=None`` reads the env flag; an explicit bool overrides it
    (tests, and the agreement harness, which is itself an explicit
    request to exercise the local tier).
    """
    on = is_enabled() if enabled is None else bool(enabled)
    if not on:
        return None
    return client or LocalModelClient()


def _run_or_none(
    client: LocalModelClient, task_name: str, **inputs: Any,
) -> Optional[Any]:
    """``client.run`` with the configuration error demoted to a WARN.

    A misconfigured OPTIONAL tier must not break a pipeline mid-run; the
    message still names the fix, so it is loud without being fatal.
    """
    try:
        return client.run(task_name, **inputs)
    except LocalModelUnavailable as exc:
        _warn(f"{exc} (falling back to the deterministic classifier)")
        return None


def local_role(
    oracle_text: str,
    type_line: str = "",
    *,
    client: Optional[LocalModelClient] = None,
    enabled: Optional[bool] = None,
) -> Optional[str]:
    """The local tier's role for one card, or None if it can't answer."""
    resolved = _resolve_client(client, enabled)
    if resolved is None:
        return None
    return _run_or_none(
        resolved, "role_tag", oracle_text=oracle_text, type_line=type_line,
    )


def role_for_card(
    oracle_text: str,
    type_line: str = "",
    *,
    client: Optional[LocalModelClient] = None,
    enabled: Optional[bool] = None,
) -> str:
    """Role for one card. Local tier when it answers, deterministic otherwise.

    The deterministic path is ``staples.classify_role_extended`` — the
    canonical classifier the dashboard and advisor already share.
    """
    value = local_role(oracle_text, type_line, client=client, enabled=enabled)
    if value is not None:
        return value
    return _staples.classify_role_extended(oracle_text, type_line)


def deterministic_archetype(
    deck_text: str,
    *,
    deck_path: Optional[Path] = None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> str:
    """The shipped v2 answer for a deck: signal label, then ``midrange``.

    With a ``deck_path`` this is exactly ``archetype.classify`` (filename
    hint included). Without one — the harness's in-memory case — it is the
    v2 signal ladder with the same honest ``midrange`` default.
    """
    if deck_path is not None:
        return _archetype.classify(deck_path)
    try:
        signals = _archetype.derive_archetype_signals(deck_text, lookup=lookup)
    except Exception:  # noqa: BLE001 — classification must not raise
        return "midrange"
    return signals.get("label") or "midrange"


def local_archetype(
    deck_text: str,
    *,
    deck_name: str = "",
    signals: Optional[dict] = None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
    client: Optional[LocalModelClient] = None,
    enabled: Optional[bool] = None,
) -> Optional[str]:
    """The local tier's archetype for one deck, or None if it can't answer.

    Signals are DERIVED LOCALLY (``archetype.derive_archetype_signals``)
    and supplied in the prompt — the model reads measurements, it does not
    recall cards.
    """
    resolved = _resolve_client(client, enabled)
    if resolved is None:
        return None
    if signals is None:
        try:
            signals = _archetype.derive_archetype_signals(deck_text, lookup=lookup)
        except Exception:  # noqa: BLE001
            signals = {}
    return _run_or_none(
        resolved, "archetype_tag", signals=signals, deck_name=deck_name,
    )


def archetype_for_deck(
    deck_text: str,
    *,
    deck_name: str = "",
    deck_path: Optional[Path] = None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
    client: Optional[LocalModelClient] = None,
    enabled: Optional[bool] = None,
) -> str:
    """Archetype for one deck. Local tier when it answers, v2 otherwise."""
    value = local_archetype(
        deck_text, deck_name=deck_name, lookup=lookup,
        client=client, enabled=enabled,
    )
    if value is not None:
        return value
    return deterministic_archetype(deck_text, deck_path=deck_path, lookup=lookup)


# ---------------------------------------------------------------------------
# Agreement harness — the only honest way to decide if this tier is useful
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgreementReport:
    """How often the local tier matched the deterministic classifier.

    This measures AGREEMENT, not accuracy. The deterministic classifier
    is not ground truth — it is the shipped answer. A disagreement is a
    case to look at by hand, in either direction.

    ``answered`` counts items where the local tier produced a valid
    in-taxonomy answer; ``agreed`` counts the subset that matched. Rates
    are ``None`` (never 0.0) when their denominator is zero, so "nothing
    was measured" can't be read as "everything disagreed".
    """
    task: str
    model: str
    total: int
    answered: int
    agreed: int
    disagreements: tuple[tuple[str, str, str], ...] = ()

    @property
    def unanswered(self) -> int:
        return self.total - self.answered

    @property
    def coverage(self) -> Optional[float]:
        """Share of items the local tier answered at all."""
        if self.total <= 0:
            return None
        return self.answered / self.total

    @property
    def agreement(self) -> Optional[float]:
        """Share of ANSWERED items that matched the deterministic label."""
        if self.answered <= 0:
            return None
        return self.agreed / self.answered

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "model": self.model,
            "total": self.total,
            "answered": self.answered,
            "agreed": self.agreed,
            "unanswered": self.unanswered,
            "coverage": self.coverage,
            "agreement": self.agreement,
            "disagreements": [list(d) for d in self.disagreements],
        }

    def render(self) -> str:
        def pct(v: Optional[float]) -> str:
            return "n/a" if v is None else f"{v * 100:.1f}%"
        lines = [
            f"task={self.task}  model={self.model}",
            f"  items measured : {self.total}",
            f"  local answered : {self.answered} ({pct(self.coverage)})",
            f"  agreed         : {self.agreed} of {self.answered} "
            f"({pct(self.agreement)})",
        ]
        if self.disagreements:
            lines.append("  disagreements (item / local / deterministic):")
            lines.extend(
                f"    {item}: {local} vs {det}"
                for item, local, det in self.disagreements
            )
        lines.append(
            "  NOTE: agreement with the deterministic classifier, NOT accuracy."
        )
        return "\n".join(lines)


def measure_agreement(
    task: str,
    items: Iterable[Any],
    *,
    local_fn: Callable[[Any], Optional[str]],
    deterministic_fn: Callable[[Any], str],
    label_fn: Callable[[Any], str],
    model: str = "",
    max_disagreements: int = 50,
) -> AgreementReport:
    """Run both classifiers over ``items`` and tally.

    Pure bookkeeping: the two classifiers and the item labeller are
    injected, which is what makes the arithmetic testable without a
    daemon and what lets the caller measure any future task.
    """
    total = answered = agreed = 0
    disagreements: list[tuple[str, str, str]] = []
    for item in items:
        total += 1
        det = deterministic_fn(item)
        local = local_fn(item)
        if local is None:
            continue
        answered += 1
        if local == det:
            agreed += 1
        elif len(disagreements) < max_disagreements:
            disagreements.append((label_fn(item), str(local), str(det)))
    return AgreementReport(
        task=task, model=model, total=total, answered=answered,
        agreed=agreed, disagreements=tuple(disagreements),
    )


def role_agreement(
    cards: Iterable[dict],
    *,
    client: Optional[LocalModelClient] = None,
    local_fn: Optional[Callable[[dict], Optional[str]]] = None,
) -> AgreementReport:
    """Agreement on role tagging over card dicts.

    Each card is ``{"name": ..., "oracle_text": ..., "type_line": ...}``.
    Passing ``local_fn`` replaces the model call entirely (tests, dry
    runs); otherwise a client is built from the environment — calling
    this function IS the opt-in, so the env flag is not consulted.
    """
    resolved = client or LocalModelClient()
    call = local_fn or (lambda c: _run_or_none(
        resolved, "role_tag",
        oracle_text=c.get("oracle_text", ""),
        type_line=c.get("type_line", ""),
    ))
    return measure_agreement(
        "role_tag",
        list(cards),
        local_fn=call,
        deterministic_fn=lambda c: _staples.classify_role_extended(
            c.get("oracle_text", ""), c.get("type_line", ""),
        ),
        label_fn=lambda c: str(c.get("name") or "(unnamed card)"),
        model=resolved.config.model,
    )


def archetype_agreement(
    decks: Iterable[dict],
    *,
    client: Optional[LocalModelClient] = None,
    local_fn: Optional[Callable[[dict], Optional[str]]] = None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> AgreementReport:
    """Agreement on archetype tagging over deck dicts.

    Each deck is ``{"name": ..., "deck_text": ..., "path": Optional[Path]}``.
    """
    resolved = client or LocalModelClient()
    call = local_fn or (lambda d: local_archetype(
        d.get("deck_text", ""),
        deck_name=str(d.get("name") or ""),
        lookup=lookup,
        client=resolved,
        enabled=True,
    ))
    return measure_agreement(
        "archetype_tag",
        list(decks),
        local_fn=call,
        deterministic_fn=lambda d: deterministic_archetype(
            d.get("deck_text", ""), deck_path=d.get("path"), lookup=lookup,
        ),
        label_fn=lambda d: str(d.get("name") or "(unnamed deck)"),
        model=resolved.config.model,
    )


# ---------------------------------------------------------------------------
# CLI — `python -m commander_builder.local_model ...`
# ---------------------------------------------------------------------------

def _load_cards(path: Path) -> list[dict]:
    """Card records from a JSON file: a list of dicts, or a name -> dict map
    (the shape ``tests/fixtures/real_oracles.ORACLES`` uses)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [
            {"name": name, **(rec if isinstance(rec, dict) else {})}
            for name, rec in data.items()
        ]
    return [rec for rec in data if isinstance(rec, dict)]


def _load_decks(directory: Path, pattern: str = "*.dck") -> list[dict]:
    decks = []
    for path in sorted(directory.glob(pattern)):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        decks.append({"name": path.name, "deck_text": text, "path": path})
    return decks


def main(argv: Optional[Sequence[str]] = None) -> int:
    # --model / --url are accepted on EITHER side of the subcommand
    # (`--url X preflight` and `preflight --url X` both work). The
    # SUPPRESS default is what makes that safe: without it, the
    # subparser's own default would overwrite a value given before the
    # subcommand with None.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--model", default=argparse.SUPPRESS, help="Override the model name.",
    )
    common.add_argument(
        "--url", default=argparse.SUPPRESS, help="Override the daemon base URL.",
    )

    parser = argparse.ArgumentParser(
        prog="python -m commander_builder.local_model",
        parents=[common],
        description=(
            "Local-model tier: preflight the daemon, or measure how often it "
            "agrees with the deterministic classifiers. Reports agreement, "
            "never accuracy."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "preflight", parents=[common],
        help="Check the daemon and the configured model.",
    )
    sub.add_parser(
        "tasks", parents=[common], help="List registered local-model tasks.",
    )

    agree = sub.add_parser(
        "agreement", parents=[common],
        help="Measure local-vs-deterministic agreement.",
    )
    agree.add_argument("--task", choices=["role", "archetype"], required=True)
    agree.add_argument(
        "--cards", type=Path,
        help="JSON file of card records (role task).",
    )
    agree.add_argument(
        "--decks", type=Path,
        help="Directory of .dck files (archetype task).",
    )
    agree.add_argument("--glob", default="*.dck", help="Deck filename pattern.")
    agree.add_argument("--limit", type=int, default=0, help="Cap items measured.")
    agree.add_argument("--json", action="store_true", help="Emit JSON.")

    args = parser.parse_args(argv)

    config = LocalModelConfig.from_env()
    model_override = getattr(args, "model", None)
    url_override = getattr(args, "url", None)
    if model_override or url_override:
        config = LocalModelConfig(
            model=model_override or config.model,
            base_url=url_override or config.base_url,
        )
    client = LocalModelClient(config)

    if args.command == "tasks":
        for name in task_names():
            print(f"{name}: {get_task(name).description}")
        return 0

    try:
        client.preflight()
    except LocalModelUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.command == "preflight":
        print(f"OK: {config.model} is pulled and reachable at {config.base_url}")
        return 0

    if args.task == "role":
        if not args.cards:
            parser.error("--task role requires --cards <file.json>")
        items = _load_cards(args.cards)
        if args.limit:
            items = items[: args.limit]
        report = role_agreement(items, client=client)
    else:
        if not args.decks:
            parser.error("--task archetype requires --decks <directory>")
        items = _load_decks(args.decks, args.glob)
        if args.limit:
            items = items[: args.limit]
        report = archetype_agreement(items, client=client)

    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

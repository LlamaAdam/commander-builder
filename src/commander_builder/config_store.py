"""Per-user application config store (FP-011 — BYO LLM token).

Distinct from ``_secrets.py``: that module loads the credentials ENV
file (``~/.commander-builder/credentials``) into ``os.environ`` for the
CLIs. *This* module holds the **web app's** per-user settings — including
a bring-your-own Anthropic API token — as JSON at:

    Windows:  %LOCALAPPDATA%\\commander-builder\\config.json
    other:    ~/.commander-builder/config.json

Override the whole path via the ``COMMANDER_BUILDER_CONFIG`` env var
(used by tests / CI / non-standard setups).

Token-safety contract (the reason this module exists):

  * The raw token is **never echoed back**. ``redact_config`` returns a
    boolean ``<key>_set`` plus a last-4 ``<key>_hint`` for display — the
    editable field is omitted entirely, so a GET → render → PUT round
    trip can't accidentally re-submit (or leak) the real key.
  * ``validate_update`` checks a supplied token against the same
    credential-shape pattern the pre-commit secret scanner uses
    (``scripts/scan_secrets.py``), rejecting malformed values so a typo
    doesn't silently disable Claude.
  * ``save_config`` writes the file owner-only (chmod 0o600 on Unix;
    best-effort no-op on Windows, where the app binds to 127.0.0.1 and
    %LOCALAPPDATA% is already per-user).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

_OVERRIDE_ENV_VAR = "COMMANDER_BUILDER_CONFIG"
_DIR_NAME = "commander-builder"
_FILE_NAME = "config.json"

# Keys the web config accepts. Anything else in a PUT body is rejected
# so the file can't accumulate arbitrary client-supplied junk.
#   secret  — never round-tripped to the client; stored redacted on GET.
#   int     — coerced + range-checked.
#   str     — stored as-is; None / "" clears the key.
SECRET_KEYS = frozenset({"anthropic_api_key"})
_INT_KEYS = frozenset({"default_bracket"})
_STR_KEYS = frozenset({"model", "moxfield_user", "deck_dir"})
ALLOWED_KEYS = SECRET_KEYS | _INT_KEYS | _STR_KEYS

# Default deck-dir used when no deck_dir is configured.
# Uses %USERPROFILE%\Documents\CommanderBuilder\decks on Windows;
# ~/Documents/CommanderBuilder/decks elsewhere.
_DECK_DIR_ENV_VAR = "COMMANDER_BUILDER_DECK_DIR"

# Mirrors the anthropic-key pattern in ``scripts/scan_secrets.py``
# (kept in sync by hand — scripts/ isn't an importable package). Used to
# validate a token supplied via PUT before we persist it.
_ANTHROPIC_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{16,}$")


def config_path() -> Path:
    """Resolve the active config.json path.

    Honors ``COMMANDER_BUILDER_CONFIG`` first. Otherwise uses
    ``%LOCALAPPDATA%\\commander-builder\\config.json`` on Windows and
    ``~/.commander-builder/config.json`` elsewhere. The file may not
    exist — callers handle that.
    """
    override = os.environ.get(_OVERRIDE_ENV_VAR)
    if override:
        return Path(override)
    localappdata = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and localappdata:
        return Path(localappdata) / _DIR_NAME / _FILE_NAME
    return Path.home() / f".{_DIR_NAME}" / _FILE_NAME


def load_config(path: Optional[Path] = None) -> dict[str, Any]:
    """Read the config JSON. Returns ``{}`` when the file is missing,
    empty, or unparseable (a corrupt config must never crash the app —
    it degrades to defaults). Only whitelisted keys are surfaced."""
    target = path or config_path()
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in ALLOWED_KEYS}


def save_config(data: dict[str, Any], path: Optional[Path] = None) -> Path:
    """Write the full config dict to disk, owner-only. Creates the parent
    directory if needed. Only whitelisted keys are persisted."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in data.items() if k in ALLOWED_KEYS}
    target.write_text(json.dumps(clean, indent=2, sort_keys=True), encoding="utf-8")
    # Owner read/write only. Harmless no-op on Windows.
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


def redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Project a stored config into the client-safe shape for GET.

    Secret keys are NEVER returned verbatim. For each secret key with a
    non-empty value we emit ``<key>_set: true`` and ``<key>_hint:
    "…<last4>"``; the raw field itself is omitted. Non-secret keys pass
    through unchanged.
    """
    out: dict[str, Any] = {}
    for key in sorted(ALLOWED_KEYS):
        if key in SECRET_KEYS:
            val = cfg.get(key)
            has = bool(val)
            out[f"{key}_set"] = has
            if has:
                s = str(val)
                out[f"{key}_hint"] = ("…" + s[-4:]) if len(s) > 4 else "…"
        elif key in cfg:
            out[key] = cfg[key]
    return out


def validate_update(updates: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate + normalize a PUT body into a sparse update dict.

    Returns ``(normalized, errors)``. ``normalized`` maps each accepted
    key to either a new value or the ``None`` sentinel meaning *clear
    this key*. ``errors`` is a list of human-readable messages; when
    non-empty the caller should reject the whole request (400) and not
    persist anything.

    Rules:
      * Unknown keys → error (no silent drop, so the client learns).
      * Secret keys: ``None`` / ``""`` clears; any other value must match
        the credential-shape pattern, else error.
      * ``default_bracket``: int in 1..5 (accepts a numeric string).
      * String keys: ``None`` / ``""`` clears; otherwise stored as str.
    """
    normalized: dict[str, Any] = {}
    errors: list[str] = []

    for key, value in updates.items():
        if key not in ALLOWED_KEYS:
            errors.append(f"unknown config key: {key!r}")
            continue

        if key in SECRET_KEYS:
            if value is None or value == "":
                normalized[key] = None  # clear
            elif isinstance(value, str) and _ANTHROPIC_KEY_RE.match(value.strip()):
                normalized[key] = value.strip()
            else:
                errors.append(
                    f"{key}: does not look like a valid Anthropic API key "
                    f"(expected 'sk-ant-…')"
                )
            continue

        if key in _INT_KEYS:
            if value is None or value == "":
                normalized[key] = None
                continue
            try:
                n = int(value)
            except (TypeError, ValueError):
                errors.append(f"{key}: expected an integer 1-5")
                continue
            if not (1 <= n <= 5):
                errors.append(f"{key}: must be 1-5, got {n}")
                continue
            normalized[key] = n
            continue

        # String keys.
        if value is None or value == "":
            normalized[key] = None
        elif isinstance(value, str):
            normalized[key] = value
        else:
            errors.append(f"{key}: expected a string")

    return normalized, errors


def apply_update(
    updates: dict[str, Any], path: Optional[Path] = None,
) -> dict[str, Any]:
    """Merge a validated update into the stored config and persist it.

    Keys mapped to ``None`` in ``updates`` are removed; others are set.
    Returns the new stored config (raw, NOT redacted — callers redact
    before returning to a client)."""
    target = path or config_path()
    cfg = load_config(target)
    for key, value in updates.items():
        if value is None:
            cfg.pop(key, None)
        else:
            cfg[key] = value
    save_config(cfg, target)
    return cfg


def get_deck_dir(path: Optional[Path] = None) -> Path:
    """Resolve the active deck directory.

    Resolution order (first wins):
    1. ``COMMANDER_BUILDER_DECK_DIR`` environment variable.
    2. ``deck_dir`` key in the persisted config (set via ``PUT /api/config``).
    3. Platform default:
       Windows: ``%USERPROFILE%\\Documents\\CommanderBuilder\\decks``
       other:   ``~/Documents/CommanderBuilder/decks``

    The directory is NOT created by this function — callers decide when to
    create it (e.g. on first actual write, not on every lookup).
    """
    env_override = os.environ.get(_DECK_DIR_ENV_VAR)
    if env_override:
        return Path(env_override)

    cfg = load_config(path)
    configured = cfg.get("deck_dir")
    if configured:
        return Path(configured)

    # Platform default.
    user_profile = os.environ.get("USERPROFILE") if os.name == "nt" else None
    home = Path(user_profile) if user_profile else Path.home()
    return home / "Documents" / "CommanderBuilder" / "decks"

"""User card-collection registry (ManaFoundry parity — owned-card
filtering).

ManaFoundry lets a user register the cards they actually own and then
filters/annotates deck suggestions by ownership. This module is the
storage + matching core for that feature; the advisor/proposer filter
stages and the web/CLI surfaces consume it.

Storage location — ``~/.commander-builder/collection.txt`` by default,
overridable via the ``COMMANDER_BUILDER_COLLECTION`` env var:

* The dot-dir-in-home convention deliberately matches ``_secrets.py``
  (``~/.commander-builder/credentials``): the collection is per-user
  machine-local data that must NEVER live inside the repo (it would be
  meaningless to other clones and noisy in git). It is NOT a secret,
  so none of the chmod/redaction machinery from config_store applies.
* The env override exists for tests/CI (point it at ``tmp_path``) and
  non-standard setups, mirroring ``COMMANDER_BUILDER_CREDENTIALS`` /
  ``COMMANDER_BUILDER_CONFIG``.
* The path is resolved at CALL time by ``collection_path()`` — never
  baked into a def-time default or module-level constant consumed by
  callers. This is the DEFAULT_DB_PATH lesson (see tests/conftest.py's
  ``_isolate_knowledge_log_default_path`` docstring): an import-time
  copy silently defeats every test monkeypatch and lets tests read the
  developer's real user dir.

File format — one card name per line, ``#`` comments and blank lines
ignored, an optional ``<qty> `` prefix tolerated and discarded:

    # commander-builder collection
    Sol Ring
    3 Lightning Bolt
    Malakir Rebirth

Quantities are deliberately NOT stored: Commander is a singleton
format, so ownership is a yes/no question per name — "do I own at
least one copy". Tolerating the qty prefix means a Moxfield bulk list
or a ``csv_to_lines`` conversion can be saved verbatim.

Name matching — ``name_key()`` folds to the lowercase FRONT FACE
(``split("//")[0].strip().lower()``). This is the exact convention the
web layer's ``deck_text_ops._dck_name_key`` and the core
``_card_list_refresh`` / ``edhrec_client`` DFC handling already use:
collection exports usually carry the full ``Malakir Rebirth //
Malakir Mire`` form while advisor recs / .dck lines usually carry only
the front face, and folding both sides bridges the drift. The helper
is REDEFINED here rather than imported from ``deck_text_ops`` because
core modules must never import from ``commander_builder.web`` (web
imports core, never the reverse — same layering note as
``import_formats``).

Basic lands are ALWAYS considered owned (see ``owns()``): every player
effectively has unlimited basics, and excluding "add 2 Islands" from a
manabase suggestion because the user never typed "Island" into their
collection would be pure noise.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

# Env override + default location. Same shape as _secrets.py's
# credentials resolution: override wins outright, otherwise the
# dot-dir in the user's home.
_OVERRIDE_ENV_VAR = "COMMANDER_BUILDER_COLLECTION"
_DEFAULT_DIR = ".commander-builder"
_DEFAULT_FILE = "collection.txt"

# Basic lands (plus Wastes) — mirrors web/deck_text_ops._BASIC_LANDS.
# Kept as a local frozenset for the same layering reason name_key is
# redefined here: core cannot import from web. Snow-Covered variants
# share the basic supertype and are handled by prefix-strip in
# ``_is_basic_land_key`` rather than enumerating six more names.
_BASIC_LAND_KEYS = frozenset(
    n.lower() for n in
    ("Forest", "Island", "Plains", "Swamp", "Mountain", "Wastes")
)
_SNOW_PREFIX = "snow-covered "


def name_key(name: str) -> str:
    """Matching key for a card name: case-folded front face.

    Same one-line convention as ``deck_text_ops._dck_name_key`` (see
    module docstring for why it's redefined rather than imported).
    Front-face names are unique across Magic cards, so the fold cannot
    collide two distinct cards.
    """
    return name.split("//", 1)[0].strip().lower()


def _is_basic_land_key(key: str) -> bool:
    """True when a name-key is a basic land (Snow-Covered included)."""
    if key.startswith(_SNOW_PREFIX):
        key = key[len(_SNOW_PREFIX):]
    return key in _BASIC_LAND_KEYS


def collection_path() -> Path:
    """Resolve the active collection file path AT CALL TIME.

    Honors ``COMMANDER_BUILDER_COLLECTION`` first (tests / CI /
    non-standard setups), else ``~/.commander-builder/collection.txt``.
    The file may not exist — callers (``load_collection``) treat a
    missing file as "no collection registered", which keeps every
    ownership filter inert.
    """
    override = os.environ.get(_OVERRIDE_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / _DEFAULT_DIR / _DEFAULT_FILE


def parse_collection_lines(text: str) -> list[str]:
    """Parse plain collection text into an ordered, deduped name list.

    Accepts one name per line; ``#`` comments and blank lines are
    skipped; an optional ``<qty> `` prefix is stripped (ownership is
    boolean — see module docstring). Dedup is by ``name_key`` but the
    FIRST-SEEN original casing is preserved so ``save_collection``
    round-trips human-readable names, not lowercase keys.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        # Strip a UTF-8 BOM (Excel/Notepad artifacts) so it can't glue
        # itself to the first name — same guard as import_formats.
        s = raw.strip().lstrip("﻿").strip()
        if not s or s.startswith("#"):
            continue
        # Optional quantity prefix ("3 Lightning Bolt"). Only strip a
        # LEADING all-digit token followed by whitespace — a card name
        # can never start with a bare number-then-space in Magic, so
        # this cannot eat a real name.
        parts = s.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            s = parts[1].strip()
        if not s:
            continue
        key = name_key(s)
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def parse_collection_text(text: str) -> list[str]:
    """Parse a pasted/uploaded collection blob — CSV or plain lines.

    CSV detection + conversion REUSE ``import_formats`` (the paste-
    import parsers landed one commit before this feature): a second
    hand-rolled CSV parser would inevitably drift on quoting/BOM/
    delimiter handling. ``csv_to_lines`` returns the plain
    ``<qty> <Name>`` shape, which ``parse_collection_lines`` already
    tolerates — so both input formats converge on one parser.

    Raises ``import_formats.ImportFormatError`` on a positively-
    detected CSV with a broken row (the web route maps it to a 400,
    mirroring the deck-paste route's contract). Plain text never
    errors — unparseable lines are just names we'll never match.
    """
    from .import_formats import _looks_like_csv, csv_to_lines
    if _looks_like_csv(text):
        text = csv_to_lines(text)
    return parse_collection_lines(text)


def load_collection(path: Optional[Path] = None) -> Optional[frozenset[str]]:
    """Load the registered collection as a frozenset of name-keys.

    Returns ``None`` when no collection file exists (or it can't be
    read) — the sentinel every filter stage reads as "feature not in
    use, stay inert". This is distinct from an EMPTY frozenset (a
    present-but-empty file), which means "user registered a collection
    containing nothing" and legitimately marks every non-basic add as
    unowned. A read error degrades to None rather than raising because
    a corrupt collection file must never break an audit — same
    degrade-to-defaults posture as ``config_store.load_config``.
    """
    target = path or collection_path()
    if not target.exists():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    return frozenset(name_key(n) for n in parse_collection_lines(text))


def save_collection(names: Iterable[str], path: Optional[Path] = None) -> Path:
    """Persist a collection (original-casing names, one per line).

    Creates the parent directory if needed. Callers pass the output of
    ``parse_collection_text`` so the file stays deduped and readable.
    No chmod: the collection is not a secret (contrast _secrets.py).
    """
    target = path or collection_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# commander-builder collection — one card name per line.\n"
        "# Managed via the web Settings panel or by editing directly.\n"
        + "\n".join(names)
    )
    target.write_text(body + "\n", encoding="utf-8")
    return target


def clear_collection(path: Optional[Path] = None) -> None:
    """Delete the collection file (unregister the collection).

    Deleting — rather than writing an empty file — restores the
    ``load_collection() is None`` inert state; an empty FILE would
    instead mean "I own nothing" and flag every suggestion (see
    ``load_collection``'s None-vs-empty contract). Missing file is a
    no-op.
    """
    target = path or collection_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass


def owns(collection_keys: frozenset[str], card_name: str) -> bool:
    """True when ``card_name`` counts as owned against a loaded
    collection: either its name-key is registered, or it is a basic
    land (always owned — see module docstring)."""
    key = name_key(card_name)
    return key in collection_keys or _is_basic_land_key(key)

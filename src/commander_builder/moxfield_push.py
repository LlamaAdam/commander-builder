"""Moxfield deck push helpers.

Phase 2 needs to push iterated decks back to Moxfield so the cycle closes:
  pull → convert → simulate → analyze → push → pull next iteration

Moxfield doesn't publish a write API. The audit prompt works around this by
running JavaScript inside a logged-in browser session — pasting deck text into
the deck-edit page's textarea and clicking Save. Replicating that from Python
needs either:
  (a) an authenticated session cookie / bearer token (not user-facing today)
  (b) a clipboard-based "manual paste" helper (works without auth)
  (c) a browser-automation driver (Playwright / Selenium) — heavy, brittle

We ship (b) now and leave (a) as a typed stub for when token auth becomes
practical. (c) is out of scope until headless flows actually save reliably.

Public API:

    from commander_builder.moxfield_push import dck_to_textarea, prepare_push

    text = dck_to_textarea(Path(".../[USER] My Deck v2 [B3].dck"))
    # → "1 Sol Ring|CMM|1\\n1 Atraxa, Praetors' Voice|CMM|1\\n..." for paste

    prepare_push(deck_path, copy_to_clipboard=True)
    # → writes the textarea blob to the OS clipboard if pyperclip is available,
    #   else prints to stdout for manual copy. Returns the blob string.

The `_api_push` stub raises NotImplementedError; flesh it out when auth lands.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Optional


# Forge .dck section markers we recognize.
_SECTION_RE = re.compile(r"^\[([A-Za-z]+)\]$")

# Forge card line: "<qty> <Name>|<SET>|<CN>" (set+cn optional).
# Group 1: qty, group 2: name, group 3: set (optional), group 4: collector
# number (optional).
_DCK_LINE_RE = re.compile(
    r"^(\d+)\s+(.+?)(?:\|([A-Za-z0-9]+)\|([A-Za-z0-9*]+))?\s*$",
)


def to_moxfield_line(line: str) -> str:
    """Convert one Forge .dck card line to Moxfield's bulk-paste format.

    Moxfield rejects the ``|SET|CN`` pipe suffix Forge uses; it accepts
    either bare ``"1 Arcane Signet"`` or printing-locked
    ``"1 Arcane Signet (MIC) 157"``. We emit the printing-locked form
    when the .dck has set+cn so Moxfield faithfully reproduces the
    chosen printing; bare names pass through unchanged.

    Examples:
      ``"1 Arcane Signet|MIC|157"`` → ``"1 Arcane Signet (MIC) 157"``
      ``"1 Forest"``                → ``"1 Forest"``
      ``"1 Sephiroth // One-Winged Angel|FIN|115"``
        → ``"1 Sephiroth // One-Winged Angel (FIN) 115"``
    """
    m = _DCK_LINE_RE.match(line.strip())
    if not m:
        return line.rstrip()
    qty, name, set_code, cn = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
    if set_code and cn:
        return f"{qty} {name} ({set_code.upper()}) {cn}"
    return f"{qty} {name}"


def parse_dck_lines(deck_path: Path) -> dict[str, list[str]]:
    """Split a .dck into named sections of card lines.

    Returns `{"commander": [...], "main": [...], ...}` with each list being the
    raw card lines (`<qty> <Name>|<SET>|<CN>`). Unknown sections come through
    lowercased keys so callers can still read them."""
    if not deck_path.exists():
        raise FileNotFoundError(f"deck not found: {deck_path}")
    out: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw in deck_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SECTION_RE.match(line)
        if m:
            current = m.group(1).lower()
            # Don't pre-create the metadata bucket — Moxfield doesn't accept
            # the metadata block in its bulk-edit textarea, so it shouldn't
            # appear in the parsed output at all.
            if current != "metadata":
                out.setdefault(current, [])
            continue
        if current is None or current == "metadata":
            continue
        out.setdefault(current, []).append(line)
    return out


def dck_to_textarea(deck_path: Path) -> str:
    """Render a .dck file as the line-list format Moxfield's bulk-edit textarea
    accepts: ``<qty> <Name>`` or ``<qty> <Name> (SET) <CN>`` if printing locked.

    Forge's pipe-suffixed lines (``1 Arcane Signet|MIC|157``) are rejected by
    Moxfield's parser, so we run every card line through ``to_moxfield_line()``
    before joining. That converter rewrites pipe form into Moxfield's
    parenthesized form and passes bare names through unchanged.

    Includes commanders, mainboard, and any other sections found. Moxfield's
    parser is permissive about the order; commanders auto-route by card type.
    Output is newline-joined ready to paste."""
    sections = parse_dck_lines(deck_path)
    lines: list[str] = []
    # Order matters for human readability of the resulting textarea, not for
    # Moxfield's parser. Commanders first → mainboard → sideboard → considering.
    for key in ("commander", "main", "sideboard", "considering"):
        if key in sections:
            lines.extend(to_moxfield_line(s) for s in sections[key])
    # Anything Moxfield-recognized but not in the canonical order goes last.
    for key, val in sections.items():
        if key in {"commander", "main", "sideboard", "considering", "metadata"}:
            continue
        lines.extend(to_moxfield_line(s) for s in val)
    return "\n".join(lines)


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard write. Returns True on success, False if no
    backend is available. We avoid making pyperclip a hard dep since the
    rest of the project doesn't need it."""
    try:
        import pyperclip  # type: ignore
    except ImportError:
        return False
    try:
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def prepare_push(
    deck_path: Path,
    copy_to_clipboard: bool = True,
    print_to_stdout: bool = False,
) -> str:
    """Generate the Moxfield-textarea blob for a .dck. Optionally copies to
    clipboard. Always returns the blob so callers can route it themselves."""
    text = dck_to_textarea(deck_path)
    if copy_to_clipboard:
        ok = _copy_to_clipboard(text)
        if ok:
            print(f"Copied {len(text.splitlines())} lines to clipboard.")
            print("Paste into Moxfield's bulk-edit textarea and click Save.")
        else:
            print("Clipboard backend not available (install `pyperclip` if you want auto-copy).")
            print_to_stdout = True
    if print_to_stdout:
        print("--- BEGIN MOXFIELD TEXTAREA ---")
        print(text)
        print("--- END MOXFIELD TEXTAREA ---")
    return text


def _api_push(deck_id: str, payload: dict, api_token: Optional[str] = None) -> None:
    """Programmatic push via Moxfield's authenticated API.

    NOT IMPLEMENTED — Moxfield's deck-update endpoint requires session auth
    that we don't currently capture. When you have a working bearer token or
    cookie session, this is the seam to flesh out: it should issue a PATCH
    or PUT against `https://api2.moxfield.com/v3/decks/all/<deck_id>` with the
    structured payload Moxfield expects (boards.mainboard.cards keyed by card
    id, with quantities). Until then, use `prepare_push` for the manual flow."""
    raise NotImplementedError(
        "Moxfield API push requires authenticated session — use prepare_push "
        "to generate a textarea blob and paste it into the bulk-edit page."
    )


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="moxfield_push")
    p.add_argument("deck", help="Path to a .dck file (absolute or relative).")
    p.add_argument("--no-clipboard", action="store_true",
                   help="Skip clipboard write; print blob to stdout.")
    args = p.parse_args(argv)
    deck_path = Path(args.deck)
    if not deck_path.is_absolute():
        from .forge_runner import VENDOR_FORGE
        deck_path = VENDOR_FORGE / "userdata" / "decks" / "commander" / args.deck
    prepare_push(
        deck_path,
        copy_to_clipboard=not args.no_clipboard,
        print_to_stdout=args.no_clipboard,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

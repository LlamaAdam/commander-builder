"""Regenerate a real Claude-curated v2 for every [USER] base deck.

Uses commander-auto-curate's curator, which routes through the
subscription `claude` CLI when no API key is set — so this works on a
Claude Max box (like box1) with no ANTHROPIC_API_KEY. Overwrites the prior
v2 (detuned or otherwise) with an actual curated build, so the soak's
original-vs-v2 A/B becomes a real test of "does the curator improve the
deck?" rather than "can we detect a deliberately-worsened deck?".

Mode default 'overhaul' (up to 15 swaps) so every deck gets a meaningfully
different v2 to test; pass 'polish' for the curator's conservative call.

Usage:
  python scripts/recurate_user_decks.py [overhaul|polish|free]
"""
from __future__ import annotations

import sys
from pathlib import Path

from commander_builder.forge_runner import VENDOR_FORGE
from commander_builder.proposer import auto_curate_main
from commander_builder.web._helpers import _bracket_from_filename

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "overhaul"
    bases = [p for p in sorted(DECK_DIR.glob("[[]USER[]]*.dck"))
             if " v2 " not in p.name and "-detune" not in p.name
             and "SPDET" not in p.name and "POP" not in p.name]
    print(f"re-curating {len(bases)} decks in {mode} mode via claude CLI", flush=True)
    ok = fail = 0
    for p in bases:
        br = _bracket_from_filename(p.name) or 3
        print(f"\n=== {p.name} (B{br}) ===", flush=True)
        try:
            rc = auto_curate_main([str(p), "--bracket", str(br),
                                   "--mode", mode, "--no-log"])
        except Exception as exc:  # noqa: BLE001
            print(f"  EXC {type(exc).__name__}: {exc}", flush=True)
            fail += 1
            continue
        if rc == 0:
            ok += 1
        else:
            fail += 1
        print(f"  rc={rc}", flush=True)
    print(f"\ndone: {ok} curated, {fail} failed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

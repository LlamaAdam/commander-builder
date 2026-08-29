"""Test-only Flask launcher for the Playwright web smokes.

NOT imported by production code and NOT part of the pytest suite. It is
spawned by ``playwright.config.js`` (the ``webServer`` block) and does
three things before handing control to Flask:

1. **Isolate all on-disk state.** A temp state dir holds the deck
   directory (``COMMANDER_BUILDER_DECK_DIR``), the knowledge database
   (``COMMANDER_BUILDER_KNOWLEDGE_DB``) and the user config home
   (``COMMANDER_BUILDER_CONFIG``, which is also where
   ``routes_meta._js_error_log_path`` puts ``_js_errors.log``). Nothing
   the smokes do can touch a developer's real decks or knowledge log.

2. **Cut the network.** Scryfall / EDHREC lookups are replaced with a
   tiny local fake, and every non-loopback ``socket.connect`` raises. A
   smoke that accidentally reaches for the internet fails loudly and
   immediately instead of hanging for 20s on a urlopen timeout. NO Forge
   is involved: the two sim endpoints are never exercised for real — the
   specs intercept ``/api/propose_swap_async`` + ``/api/sim_job/*`` in
   the browser and hand back a prepared report (see
   ``tests/e2e/fixtures.js``).

3. **Seed deterministic content.** Five decks plus knowledge-log rows,
   including one deck whose rows deliberately straddle measurement eras
   3 and 4 so the verdict-breakdown era sub-line has something to
   render.

Production code is untouched: everything here is monkeypatching from the
outside, exactly as ``tests/test_web_app.py``'s fixtures do.

Usage::

    python tests/e2e/server.py --port 5199 --state-dir /tmp/cb-web-smokes
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Deck fixtures
# ---------------------------------------------------------------------------

#: Deck stems seeded into the temp deck dir. The ``[USER]`` prefix is
#: what ``_list_decks`` uses to decide a deck is user-owned (and so
#: shown in the sidebar by default); the ``[B3]`` suffix is what
#: ``_bracket_from_filename`` reads, and is the trigger for the
#: ``bracket_tag_unverified`` hint on a mainboard-changing PUT (and the
#: ``BracketUnverified=`` [metadata] marker that now persists it).
DECKS = {
    # Verdict / save-flow subject.
    "[USER] Smoke Alpha [B3]": "Test Cmdr",
    # Deck-editor subject WITH a bracket tag -> exercises the
    # bracket_tag_unverified save-status warning.
    "[USER] Smoke Bravo [B3]": "Test Cmdr",
    # Second bracket-tagged subject. The warning is now a DURABLE
    # ``BracketUnverified=`` marker in the deck's [metadata] rather than
    # a per-response flag, so a spec that raises it leaves state behind:
    # the reopen-the-editor spec needs a deck no other spec has marked,
    # or it would pass without proving anything.
    "[USER] Smoke Delta [B3]": "Test Cmdr",
    # Deck-editor subject WITHOUT a bracket tag -> plain "Saved." path,
    # and the Name= restamp subject.
    "[USER] Smoke Charlie": "Test Cmdr",
    # Plain-list/CSV imports cannot identify a commander. This fixture
    # exercises the dashboard repair flow that moves an existing mainboard
    # card into a newly-created [Commander] section.
    "[USER] Commanderless Import [B3]": None,
    # Verdict-breakdown subjects.
    "[USER] Era Mix [B3]": "Test Cmdr",
    "[USER] Era Pure [B3]": "Test Cmdr",
}


def _deck_body(stem: str, commander: str | None, forests: int = 60) -> str:
    """A minimally valid .dck: metadata + commander + a 99-card main."""
    main_target = 99 if commander else 100
    cultivates = main_target - forests - (1 if commander is None else 0)
    commander_block = (
        f"[Commander]\n1 {commander}\n\n" if commander else ""
    )
    candidate = "1 Dragon Candidate|TST|1\n" if commander is None else ""
    return (
        "[metadata]\n"
        f"Name={stem}\n\n"
        f"{commander_block}"
        "[Main]\n"
        f"{candidate}"
        f"{forests} Forest\n"
        f"{cultivates} Cultivate\n"
    )


# ---------------------------------------------------------------------------
# Offline stubs
# ---------------------------------------------------------------------------

def _fake_lookup_card(name, *_args, **_kwargs):
    """Local stand-in for ``scryfall_client.lookup_card``.

    Same shape as the fake in ``tests/test_web_app.py``'s ``client``
    fixture so the dashboard renders realistic tiles (CMC, colors,
    prices) with zero network.
    """
    if "Forest" in name:
        return {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "oracle_text": "({T}: Add {G}.)",
            "cmc": 0.0,
            "mana_cost": "",
            "color_identity": ["G"],
            "colors": [],
            "prices": {"usd": "0.05"},
            "legalities": {"commander": "legal"},
        }
    if "Cultivate" in name:
        return {
            "name": "Cultivate",
            "type_line": "Sorcery",
            "oracle_text": (
                "Search your library for up to two basic land cards..."
            ),
            "cmc": 3.0,
            "mana_cost": "{2}{G}",
            "color_identity": ["G"],
            "colors": ["G"],
            "prices": {"usd": "1.50"},
            "legalities": {"commander": "legal"},
        }
    return {
        "name": name,
        "type_line": "Legendary Creature — Elder Dragon",
        "oracle_text": "",
        "cmc": 5.0,
        "mana_cost": "{3}{G}{G}",
        "color_identity": ["G"],
        "colors": ["G"],
        "prices": {"usd": "10.00"},
        "legalities": {"commander": "legal"},
    }


class NetworkBlocked(RuntimeError):
    """Raised when a smoke run reaches for a non-loopback address."""


def _block_outbound_network() -> None:
    """Make every non-loopback TCP connect raise.

    Belt-and-braces on top of the lookup stubs: if a future dashboard
    section grows a new HTTP call, the smokes surface it as a hard,
    instantly-visible failure instead of a 20-second urlopen stall that
    reads like flakiness.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _allowed(address) -> bool:
        if not isinstance(address, tuple) or not address:
            return True  # AF_UNIX and friends — not our concern.
        host = str(address[0])
        return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "")

    def guarded_connect(self, address):
        if not _allowed(address):
            raise NetworkBlocked(
                f"outbound network blocked in the e2e fixture server: "
                f"{address!r}"
            )
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _allowed(address):
            raise NetworkBlocked(
                f"outbound network blocked in the e2e fixture server: "
                f"{address!r}"
            )
        return real_connect_ex(self, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex


def _install_offline_stubs() -> None:
    """Point every card/meta lookup at the local fake.

    ``deck_dashboard`` did ``from .scryfall_client import lookup_card``
    at module scope, so it holds its own reference — both names have to
    be patched, same as the pytest fixtures do.
    """
    from commander_builder import deck_dashboard, edhrec_client, scryfall_client

    scryfall_client.lookup_card = _fake_lookup_card
    scryfall_client.lookup_card_prints = lambda name, **_kw: None
    deck_dashboard.lookup_card = _fake_lookup_card
    # Salt list is a live EDHREC fetch inside build_dashboard's
    # fail-quiet block; stub it so the block never even tries.
    edhrec_client.fetch_salt_list = lambda *_a, **_kw: {}


# ---------------------------------------------------------------------------
# Knowledge-log seeding
# ---------------------------------------------------------------------------

def _seed_knowledge_log(db_path: Path) -> None:
    """Write the iteration rows the verdict-breakdown smokes read.

    ``Era Mix`` gets rows in measurement era 3 AND era 4; ``Era Pure``
    gets era-4 rows only. ``measurement_era`` is passed explicitly —
    ``Iteration.to_row`` documents that a caller-set era wins over the
    created_at-derived default — so the seeding needs no raw SQL and no
    clock games.
    """
    from commander_builder.knowledge_log import Iteration, record_iteration

    def _row(deck_stem, verdict, era, version="v4"):
        return Iteration(
            deck_id=deck_stem,
            deck_name=deck_stem.replace("[USER] ", ""),
            bracket=3,
            audit_version=version,
            audit_manifest={"added": [], "removed": []},
            sim_report={"old_wins": 10, "new_wins": 14},
            verdict=verdict,
            measurement_era=era,
        )

    mix = "[USER] Era Mix [B3]"
    # Era 3 (|margin| >= 4 rule) — two kept, one reverted.
    for verdict in ("kept", "kept", "reverted"):
        record_iteration(_row(mix, verdict, 3), db_path=db_path)
    # Era 4 (significance-tested) — one kept, one neutral.
    for verdict in ("kept", "neutral"):
        record_iteration(_row(mix, verdict, 4), db_path=db_path)

    # Five rows minimum on BOTH decks: renderDashboard only fetches the
    # breakdown once a deck has >= 5 iterations, so a four-row deck
    # would make the "no era sub-line" assertion pass for the wrong
    # reason (no panel at all).
    pure = "[USER] Era Pure [B3]"
    for verdict in ("kept", "kept", "reverted", "neutral", "pending"):
        record_iteration(_row(pure, verdict, 4), db_path=db_path)


# ---------------------------------------------------------------------------
# Sim-report fixtures (written for the browser-side route mocks)
# ---------------------------------------------------------------------------

def _write_sim_fixtures(out_path: Path) -> None:
    """Emit the mocked ``/api/sim_job`` reports the specs replay.

    The ``suggested_verdict`` block inside each fixture is produced by
    the REAL server helper (``web._helpers.suggested_verdict``) rather
    than hand-written, so the p-values the UI is asserted against are
    the ones production computes. If that rule ever changes, the
    fixture changes with it and the spec's expectations fail loudly
    instead of quietly testing a stale copy of the old rule.
    """
    from commander_builder.web._helpers import suggested_verdict

    def _report(old_wins, new_wins, deck):
        decisive = old_wins + new_wins
        suggestion = suggested_verdict(old_wins, new_wins)
        return {
            "old_deck": f"{deck}.dck",
            "new_deck": f"{deck}_proposed.dck",
            "diff": {"added": ["Cultivate"], "removed": ["Forest"]},
            "games_per_pod": 10,
            "mode": "pod",
            "bracket": 3,
            # ComparisonReport.winner is the ANY-lead field — the very
            # thing the browser must no longer derive its default from.
            # Kept truthful to the split so a regression that re-reads
            # it shows up as a wrong radio.
            "winner": "new" if new_wins > old_wins else "old",
            "old_wins": old_wins,
            "new_wins": new_wins,
            "old_games": decisive + 4,
            "new_games": decisive + 4,
            "draws": 2,
            "margin": abs(new_wins - old_wins),
            "total_games": decisive + 10,
            "timestamp": "2026-08-20T12:00:00+00:00",
            "suggested_verdict": suggestion,
            "pods_completed": 4,
            "pods_planned": 4,
            "stopped_early": False,
            "failed_pods": 0,
            "timed_out_pods": 0,
            "excluded_games": 0,
            "pod_failures": [],
            "pod_summaries": [],
        }

    alpha = "[USER] Smoke Alpha [B3]"
    out_path.write_text(
        json.dumps(
            {
                # 21-20 over 41 decisive games: an ANY-lead read says
                # "old deck won"; the binomial test says near-tie.
                "split_21_20": _report(21, 20, alpha),
                # 15-30: a real, significant lead for the new list.
                "split_15_30": _report(15, 30, alpha),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=5199)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--state-dir", required=True,
        help="Temp dir for decks / knowledge db / config home. WIPED on boot.",
    )
    args = ap.parse_args()

    state = Path(args.state_dir).resolve()
    if state.exists():
        shutil.rmtree(state)
    deck_dir = state / "decks"
    config_home = state / "config"
    deck_dir.mkdir(parents=True)
    config_home.mkdir(parents=True)
    db_path = state / "knowledge_log.sqlite"

    for stem, commander in DECKS.items():
        (deck_dir / f"{stem}.dck").write_text(
            _deck_body(stem, commander), encoding="utf-8",
        )

    # Same env vars the pytest suite uses. Set before create_app so the
    # app + every call-time path resolver reads them.
    os.environ["COMMANDER_BUILDER_DECK_DIR"] = str(deck_dir)
    os.environ["COMMANDER_BUILDER_KNOWLEDGE_DB"] = str(db_path)
    os.environ["COMMANDER_BUILDER_CONFIG"] = str(config_home / "config.json")

    _install_offline_stubs()
    _seed_knowledge_log(db_path)
    _write_sim_fixtures(state / "sim-fixtures.json")
    _block_outbound_network()

    from commander_builder.web.app import create_app

    app = create_app(deck_dir=deck_dir, knowledge_db=db_path)

    @app.post("/api/e2e/reset_commanderless")
    def reset_commanderless():
        """Restore the mutable commander fixture before every retry/test."""
        stem = "[USER] Commanderless Import [B3]"
        (deck_dir / f"{stem}.dck").write_text(
            _deck_body(stem, None), encoding="utf-8",
        )
        return {"ok": True}

    print(
        f"[e2e] serving http://{args.host}:{args.port} "
        f"(state={state})",
        flush=True,
    )
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

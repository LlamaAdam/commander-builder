"""Test-only Flask launcher for the full-walkthrough lane (tests/e2e/full-walkthrough.spec.js).

NOT imported by production code and NOT part of the pytest suite. It is
spawned by ``playwright.full.config.js`` (the ``webServer`` block) when
no ``E2E_BASE_URL`` points the spec at an app that is already running.

WHY A SECOND LAUNCHER (2026-10-02)
----------------------------------
``tests/e2e/server.py`` exists to make the smokes hermetic: it stubs
every Scryfall / EDHREC lookup, blocks outbound sockets and seeds six
synthetic decks whose names the smoke specs hard-code. The walkthrough
needs the opposite posture — the owner asked to see REAL results for two
real decks, so the server here keeps the network open, lets the live
import lane, the live audit and a real Forge compare run, and seeds
nothing the spec can seed through the app's own routes (the spec
paste-imports deck A and the fallback deck B and writes its iteration
rows through ``/api/save_iteration``, so the CI run and the owner's
local run against a live app walk the same code). Mixing both postures
into one launcher behind flags would couple the smokes to this lane's
state; a sibling file keeps them independent.

What it still does before handing control to Flask:

1. **Isolate the state the spec cannot isolate itself.** The knowledge
   DB, the user config home (API key / bracket / collection — the
   Settings rows PUT real values) and the replay store live in a temp
   state dir. The deck dir is a temp dir too UNLESS ``--deck-dir`` is
   given: the CI lane points it at ``vendor/forge/userdata/decks/commander``
   because Forge resolves decks by name inside its own ``userdata`` and
   ``run_match._fallback_opponents`` globs that same folder for filler
   seats — a pod compare cannot run from a temp deck dir.

2. **Seed filler seats.** A pod-mode A/B compare needs at least two
   UNTAGGED same-bracket decks for the filler seats (decision C1: a
   ``[USER]``/``[REF]``/``[PREMADE]``/``[CONTROL]`` deck may never sit in
   one). Eight copies of the fallback deck-B fixture are written under
   plain ``Filler Goblins N [B3]`` names so the compare has a pool
   without harvesting anything from Moxfield. They are ``type: pool`` to
   ``/api/decks`` and so stay out of the sidebar by default.

3. **Capture replays.** ``COMMANDER_BUILDER_KEEP_GAME_LOGS=1`` (and a
   replay dir inside the state dir) so the one real compare leaves a run
   for the Replays page rows to find.

4. **Optional offline posture.** ``--offline`` (or ``E2E_OFFLINE=1`` in
   the environment, which the Playwright config forwards) reuses
   ``server.py``'s stubs + socket guard so the lane can be developed in a
   sandbox with no egress: the spec then marks network/Forge rows as
   skipped instead of attempting them, and every offline-capable page
   still renders against the stubbed card data.

Usage::

    python tests/e2e/server_full.py --port 5299 --state-dir /tmp/cb-e2e-full
    python tests/e2e/server_full.py --port 5299 --state-dir /tmp/cb-e2e-full \
        --deck-dir vendor/forge/userdata/decks/commander
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# The sibling launcher owns the offline stubs; import them rather than
# copy them so the two lanes cannot drift on what "offline" means.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import _block_outbound_network, _install_offline_stubs  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"
FALLBACK_B = FIXTURES / "e2e_walkthrough_deck_b.dck"

#: Untagged filler decks for the pod compare's filler seats. Bracket 3 to
#: match both walkthrough decks' ``[B3]`` tags (filler picking is
#: same-bracket only). Eight, so a 4-core runner's four pods each get a
#: distinct pair instead of the stride walk repeating one.
FILLER_STEMS = [f"Filler Goblins {i} [B3]" for i in range(1, 9)]


def _seed_fillers(deck_dir: Path) -> None:
    """Write the four filler decks (idempotent: existing files are kept)."""
    from commander_builder.dck_meta import rewrite_name

    text = FALLBACK_B.read_text(encoding="utf-8")
    for stem in FILLER_STEMS:
        target = deck_dir / f"{stem}.dck"
        if target.exists():
            continue
        # Name= must equal the stem or Forge's win attribution cannot map
        # the deck back to its file (the same invariant every importer
        # keeps).
        target.write_text(rewrite_name(text, stem), encoding="utf-8")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=5299)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--state-dir", required=True,
        help="Temp dir for knowledge db / config home / replays. WIPED on boot.",
    )
    ap.add_argument(
        "--deck-dir", default=None,
        help="Deck directory (default: <state-dir>/decks). The CI lane passes "
             "vendor/forge/userdata/decks/commander so Forge can find the decks.",
    )
    ap.add_argument(
        "--offline", action="store_true",
        help="Stub card lookups and block outbound sockets (sandbox development).",
    )
    args = ap.parse_args()

    offline = args.offline or _truthy(os.environ.get("E2E_OFFLINE"))

    state = Path(args.state_dir).resolve()
    if state.exists():
        shutil.rmtree(state)
    config_home = state / "config"
    replay_dir = state / "replays"
    for d in (config_home, replay_dir):
        d.mkdir(parents=True)
    db_path = state / "knowledge_log.sqlite"

    deck_dir = (Path(args.deck_dir) if args.deck_dir else state / "decks").resolve()
    deck_dir.mkdir(parents=True, exist_ok=True)
    _seed_fillers(deck_dir)

    # Same env vars the pytest suite and server.py use. Set before
    # create_app so the app + every call-time path resolver reads them.
    os.environ["COMMANDER_BUILDER_DECK_DIR"] = str(deck_dir)
    os.environ["COMMANDER_BUILDER_KNOWLEDGE_DB"] = str(db_path)
    os.environ["COMMANDER_BUILDER_CONFIG"] = str(config_home / "config.json")
    os.environ["COMMANDER_BUILDER_LOCK_DIR"] = str(config_home)
    os.environ.setdefault("COMMANDER_BUILDER_KEEP_GAME_LOGS", "1")
    os.environ.setdefault("COMMANDER_BUILDER_REPLAY_DIR", str(replay_dir))

    if offline:
        _install_offline_stubs()
        _block_outbound_network()

    from commander_builder.web.app import create_app

    app = create_app(deck_dir=deck_dir, knowledge_db=db_path)
    print(
        f"[e2e-full] serving http://{args.host}:{args.port} "
        f"(state={state} deck_dir={deck_dir} offline={offline})",
        flush=True,
    )
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

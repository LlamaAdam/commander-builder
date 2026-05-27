"""FP-010 — desktop wrapper for the web app.

Runs the existing Flask app (``web.app.create_app``) in-process on a
background thread and shows it in a native OS window via **pywebview**, so
the whole tool ships as a double-click EXE instead of "open a terminal, run
the server, open a browser". Packaged with PyInstaller — see
``packaging/commander-builder.spec`` + ``scripts/build_desktop.py``.

Design notes:
- **No browser, no manual server.** One process: Flask thread + a webview
  window pointed at ``http://127.0.0.1:<free-port>/``.
- **Heavy data is NOT bundled.** Forge's JAR (~120 MB), the JRE, and the
  ``mtg_cards/`` image/oracle cache (~180 MB) are far too big for a tidy
  EXE. The app locates them on disk like the dev setup does and degrades
  gracefully when absent (Forge-dependent audit/sim calls error per-request,
  exactly as on a dev box without Forge) — the plan's "first-run downloader"
  is a later slice (see docs/fp010-plan.md).
- **pywebview is an optional dep** (``[desktop]`` extra). Importing this
  module is harmless without it; only ``launch()`` raises a clear message.
- ``launch()`` takes injectable ``webview`` / ``serve`` hooks so the wiring
  is unit-testable without spawning a real native window.
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Callable, Optional

APP_TITLE = "Commander Builder"
DEFAULT_HOST = "127.0.0.1"


def find_free_port(host: str = DEFAULT_HOST) -> int:
    """Bind an ephemeral port and return it. The OS guarantees uniqueness
    at bind time; the tiny race before the Flask server re-binds is
    acceptable for a single-user localhost app."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def wait_until_up(host: str, port: int, timeout: float = 15.0) -> bool:
    """Poll until the server accepts a TCP connection (or timeout). Returns
    True once reachable so the window doesn't open on a blank/refused page."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _default_serve(deck_dir: Optional[str], host: str, port: int) -> threading.Thread:
    """Build the Flask app and run it on a daemon thread. Daemon so the
    process exits cleanly when the webview window closes. No reloader — it
    double-spawns and breaks inside a PyInstaller bundle."""
    from .web.app import create_app
    app = create_app(deck_dir=Path(deck_dir) if deck_dir else None)

    def _run() -> None:
        app.run(host=host, port=port, debug=False,
                use_reloader=False, threaded=True)

    t = threading.Thread(target=_run, name="commander-builder-web", daemon=True)
    t.start()
    return t


def launch(
    deck_dir: Optional[str] = None,
    host: str = DEFAULT_HOST,
    port: Optional[int] = None,
    *,
    webview=None,
    serve: Optional[Callable[[Optional[str], str, int], threading.Thread]] = None,
) -> str:
    """Start the app server and open a native window at its URL.

    Blocks until the window is closed (``webview.start()`` runs the GUI
    loop). Returns the served URL — handy for tests that inject a non-
    blocking fake ``webview``. ``serve`` is injectable too so tests can
    avoid spinning a real Flask server.
    """
    if webview is None:
        try:
            import webview as _wv  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised via message
            raise RuntimeError(
                "pywebview is required for the desktop app. "
                "Install with: pip install commander-builder[desktop]"
            ) from exc
        webview = _wv

    serve = serve or _default_serve
    if port is None:
        port = find_free_port(host)
    serve(deck_dir, host, port)
    wait_until_up(host, port)
    url = f"http://{host}:{port}/"
    webview.create_window(APP_TITLE, url, width=1280, height=860)
    webview.start()
    return url


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="commander-builder-desktop",
        description="Run Commander Builder as a native desktop window.",
    )
    ap.add_argument("--deck-dir", default=None,
                    help="Directory of .dck files (default: the Forge "
                         "userdata decks dir, same as the web app).")
    args = ap.parse_args(argv)
    launch(deck_dir=args.deck_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""FP-010 -- desktop wrapper for the web app.

Runs the existing Flask app (``web.app.create_app``) in-process on a
background thread and shows it in a native OS window via **pywebview**, so
the whole tool ships as a double-click EXE instead of "open a terminal, run
the server, open a browser". Packaged with PyInstaller -- see
``packaging/commander-builder.spec`` + ``scripts/build_desktop.py``.

Design notes:
- **No browser, no manual server.** One process: Flask thread + a webview
  window pointed at ``http://127.0.0.1:<free-port>/``.
- **Heavy data is NOT bundled.** Forge's JAR (~120 MB), the JRE, and the
  ``mtg_cards/`` image/oracle cache (~180 MB) are far too big for a tidy
  EXE. The app locates them on disk like the dev setup does and degrades
  gracefully when absent (Forge-dependent audit/sim calls error per-request,
  exactly as on a dev box without Forge).
- **pywebview is an optional dep** (``[desktop]`` extra). Importing this
  module is harmless without it; only ``launch()`` raises a clear message.
- ``launch()`` takes injectable ``webview`` / ``serve`` / ``_acquire_lock``
  hooks so the wiring is unit-testable without spawning a real native window.

Window chrome (FP-010 slice 3):
- **App icon** -- ``_icon_path()`` resolves the bundled PNG (next to the
  package's ``data/`` dir) and is passed to ``webview.create_window``. Falls
  back to ``None`` (no icon) when the file is absent; never crashes.
- **Single-instance guard** -- ``_acquire_instance_lock()`` writes a
  lock-file (``%LOCALAPPDATA%/commander-builder/instance.lock`` on Windows,
  ``~/.commander-builder/instance.lock`` elsewhere) and attempts a
  non-blocking exclusive lock via ``msvcrt.locking`` (Windows) or
  ``fcntl.flock`` (POSIX). Returns a context-manager-like object; raises
  ``SingleInstanceError`` when a second instance is detected. Injectable
  via ``_acquire_lock`` kwarg for tests.
- **Graceful shutdown** -- ``_default_serve()`` returns a ``_ServerHandle``
  that exposes a ``shutdown()`` method; ``launch()`` registers a pywebview
  ``closing`` event handler that calls it so the Flask thread drains
  cleanly instead of being forcibly killed when the OS window is closed.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable, Optional

APP_TITLE = "Commander Builder"
DEFAULT_HOST = "127.0.0.1"
_ICON_FILENAME = "commander_builder_icon.png"


# --------------------------------------------------------------------------- #
# Single-instance guard
# --------------------------------------------------------------------------- #

class SingleInstanceError(RuntimeError):
    """Raised when a second instance of the desktop app is detected."""


class _InstanceLock:
    """Holds an open lock-file; released on close() or context exit."""

    def __init__(self, path: Path, fh):  # fh: open file handle
        self._path = path
        self._fh = fh

    def close(self):
        try:
            if os.name == "nt":
                import msvcrt
                # Seek to byte 0 before unlocking (must match the locked offset).
                try:
                    self._fh.seek(0)
                except Exception:  # noqa: BLE001
                    pass
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # type: ignore[import]
                fcntl.flock(self._fh, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _lock_file_path() -> Path:
    """Return the instance-lock file path (mirrors config_store layout)."""
    env_dir = os.environ.get("COMMANDER_BUILDER_LOCK_DIR")
    if env_dir:
        return Path(env_dir) / "instance.lock"
    localappdata = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and localappdata:
        return Path(localappdata) / "commander-builder" / "instance.lock"
    return Path.home() / ".commander-builder" / "instance.lock"


def _acquire_instance_lock(lock_path: Optional[Path] = None) -> _InstanceLock:
    """Attempt a non-blocking exclusive lock on the instance lock-file.

    Raises ``SingleInstanceError`` when another instance already holds it.
    The returned ``_InstanceLock`` must be closed (or used as a context
    manager) to release the lock on exit.

    Implementation note: Windows ``msvcrt.locking`` requires a C-runtime file
    descriptor opened in binary mode. POSIX uses ``fcntl.flock``. Both paths
    are the same file; the locking semantics are:
      Windows: LK_NBLCK on byte 0 raises OSError when another fd holds it.
      POSIX: LOCK_EX|LOCK_NB raises BlockingIOError when already locked.
    """
    path = lock_path or _lock_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            import msvcrt
            # Open in binary mode for msvcrt.locking compatibility.
            fh = open(path, "w+b")  # noqa: WPS515
            fh.write(str(os.getpid()).encode())
            fh.flush()
            fh.seek(0)
            # LK_NBLCK: non-blocking exclusive lock on 1 byte from current pos.
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # type: ignore[import]
            fh = open(path, "w", encoding="ascii")  # noqa: WPS515
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(str(os.getpid()))
            fh.flush()
    except (OSError, PermissionError) as exc:
        try:
            fh.close()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass
        raise SingleInstanceError(
            "Commander Builder is already running. "
            "Only one instance can be open at a time."
        ) from exc
    return _InstanceLock(path, fh)


# --------------------------------------------------------------------------- #
# Icon resolution
# --------------------------------------------------------------------------- #

def _icon_path() -> Optional[Path]:
    """Resolve the app icon PNG path.

    Looks for the icon next to the ``commander_builder`` package -- the same
    base directory where ``data/`` lives. Inside a PyInstaller bundle this
    resolves correctly via ``sys._MEIPASS``; in a dev tree it resolves via
    ``__file__``. Returns ``None`` when the file is absent (never crashes).
    """
    import sys
    # PyInstaller sets _MEIPASS; fall back to the source package dir.
    base: Path
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass) / "commander_builder"
    else:
        base = Path(__file__).parent
    candidate = base / "data" / _ICON_FILENAME
    return candidate if candidate.exists() else None


# --------------------------------------------------------------------------- #
# Flask server handle + graceful shutdown
# --------------------------------------------------------------------------- #

class _ServerHandle:
    """Wraps the Flask WSGI server thread and exposes a ``shutdown()`` method
    for graceful stop on window-close."""

    def __init__(self, thread: threading.Thread, shutdown_fn: Callable[[], None]):
        self.thread = thread
        self._shutdown_fn = shutdown_fn

    def shutdown(self) -> None:
        """Signal the Flask server to stop and wait briefly for it to drain."""
        try:
            self._shutdown_fn()
        except Exception:  # noqa: BLE001
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=3.0)


# --------------------------------------------------------------------------- #
# Core helpers
# --------------------------------------------------------------------------- #

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


def _default_serve(deck_dir: Optional[str], host: str, port: int) -> _ServerHandle:
    """Build the Flask app and run it on a daemon thread. Returns a
    ``_ServerHandle`` so ``launch()`` can request a graceful shutdown when
    the window closes.

    Daemon so the process exits cleanly when the webview window closes even
    if shutdown() was not called. No reloader -- it double-spawns and breaks
    inside a PyInstaller bundle.
    """
    from werkzeug.serving import make_server  # type: ignore[import]
    from .web.app import create_app
    app = create_app(deck_dir=Path(deck_dir) if deck_dir else None)

    # make_server returns a standard-library wsgiref-compatible server;
    # shutdown() sets an internal flag checked in serve_forever().
    srv = make_server(host, port, app, threaded=True)

    def _run() -> None:
        srv.serve_forever()

    def _stop() -> None:
        srv.shutdown()

    t = threading.Thread(target=_run, name="commander-builder-web", daemon=True)
    t.start()
    return _ServerHandle(t, _stop)


def _resolve_deck_dir(deck_dir: Optional[str]) -> Optional[str]:
    """Resolve the effective deck directory for launch().

    ``deck_dir`` (CLI arg or explicit kwarg) wins; otherwise fall back to
    ``config_store.get_deck_dir()`` so the configured / default value is
    used when launching via the packaged EXE with no CLI flag.
    """
    if deck_dir is not None:
        return deck_dir
    try:
        from .config_store import get_deck_dir
        return str(get_deck_dir())
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def launch(
    deck_dir: Optional[str] = None,
    host: str = DEFAULT_HOST,
    port: Optional[int] = None,
    *,
    webview=None,
    serve: Optional[Callable[[Optional[str], str, int], _ServerHandle]] = None,
    _acquire_lock: Optional[Callable[[], _InstanceLock]] = None,
) -> str:
    """Start the app server and open a native window at its URL.

    Blocks until the window is closed (``webview.start()`` runs the GUI
    loop). Returns the served URL -- handy for tests that inject a non-
    blocking fake ``webview``. ``serve`` and ``_acquire_lock`` are injectable
    so tests can avoid spinning a real Flask server or touching the filesystem.

    ``deck_dir`` is resolved via ``_resolve_deck_dir``: an explicit value
    wins; otherwise ``config_store.get_deck_dir()`` supplies the persisted
    or platform-default location.

    Window chrome:
    - The app icon is resolved via ``_icon_path()`` and passed to
      ``webview.create_window`` (None = no icon).
    - A single-instance lock is acquired before the window opens; a second
      launch raises ``SingleInstanceError`` (injectable via ``_acquire_lock``
      for tests).
    - A ``closing`` event on the webview window signals the Flask server to
      shut down gracefully before the process exits.
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

    # --- single-instance guard ---
    acquire = _acquire_lock or _acquire_instance_lock
    instance_lock = acquire()

    serve_fn = serve or _default_serve
    if port is None:
        port = find_free_port(host)

    effective_deck_dir = _resolve_deck_dir(deck_dir)
    server_handle = serve_fn(effective_deck_dir, host, port)

    # First-run dependency check -- warn (never block).
    try:
        from .bootstrap import check_dependencies
        status = check_dependencies()
        if status.missing:
            print(f"[desktop] missing dependencies: {', '.join(status.missing)} "
                  f"-- run `commander-builder-bootstrap` for details", flush=True)
    except Exception:  # noqa: BLE001
        pass

    wait_until_up(host, port)
    url = f"http://{host}:{port}/"

    icon = _icon_path()
    win = webview.create_window(
        APP_TITLE, url, width=1280, height=860,
        icon=str(icon) if icon else None,
    )

    # Graceful Flask shutdown on window close.
    def _on_closing():
        if hasattr(server_handle, "shutdown"):
            server_handle.shutdown()
        instance_lock.close()

    if win is not None and hasattr(win, "events") and hasattr(win.events, "closing"):
        win.events.closing += _on_closing

    webview.start()
    return url


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="commander-builder-desktop",
        description="Run Commander Builder as a native desktop window.",
    )
    ap.add_argument("--deck-dir", default=None,
                    help="Directory of .dck files. Overrides the persisted "
                         "deck_dir config setting. If neither is set, uses "
                         "%%USERPROFILE%%\\Documents\\CommanderBuilder\\decks "
                         "(Windows) or ~/Documents/CommanderBuilder/decks.")
    args = ap.parse_args(argv)
    try:
        launch(deck_dir=args.deck_dir)
    except SingleInstanceError as exc:
        print(f"[desktop] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Tests for the FP-010 desktop launcher (commander_builder.desktop).

Pure-logic + wiring tests. We never open a real native window: pywebview is
injected as a fake, and the Flask server start is injected too, so these run
offline with no GUI and no port races beyond a localhost bind.
"""
from __future__ import annotations

import socket

import pytest

from commander_builder import desktop


def test_find_free_port_returns_bindable_port():
    port = desktop.find_free_port()
    assert isinstance(port, int) and 1024 < port < 65536
    # The port is free right now — we can bind it ourselves.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))  # must not raise


def test_wait_until_up_true_when_listening():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert desktop.wait_until_up("127.0.0.1", port, timeout=2.0) is True


def test_wait_until_up_false_on_timeout():
    # An almost-certainly-closed port; short timeout keeps the test fast.
    closed = desktop.find_free_port()  # free => nothing listening
    assert desktop.wait_until_up("127.0.0.1", closed, timeout=0.3) is False


def test_launch_wires_webview_to_served_url(monkeypatch):
    """launch() resolves a URL, starts the server via the injected `serve`,
    and opens a window at that URL via the injected `webview`."""
    # Don't actually poll a socket — the fake serve starts nothing.
    monkeypatch.setattr(desktop, "wait_until_up", lambda *a, **k: True)

    served = {}

    def fake_serve(deck_dir, host, port):
        served["args"] = (deck_dir, host, port)
        return None  # no real thread

    calls = {"create": None, "started": 0}

    class FakeWebview:
        @staticmethod
        def create_window(title, url, **kw):
            calls["create"] = {"title": title, "url": url, "kw": kw}

        @staticmethod
        def start():
            calls["started"] += 1

    url = desktop.launch(
        deck_dir="C:/decks", host="127.0.0.1", port=5599,
        webview=FakeWebview, serve=fake_serve,
    )

    assert url == "http://127.0.0.1:5599/"
    assert served["args"] == ("C:/decks", "127.0.0.1", 5599)
    assert calls["create"]["title"] == desktop.APP_TITLE
    assert calls["create"]["url"] == "http://127.0.0.1:5599/"
    assert calls["create"]["kw"].get("width") and calls["create"]["kw"].get("height")
    assert calls["started"] == 1


def test_launch_missing_pywebview_raises_helpful_error(monkeypatch):
    """Without pywebview installed, launch() raises a message pointing at
    the [desktop] extra rather than a bare ImportError."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("no module named webview")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match=r"pip install commander-builder\[desktop\]"):
        desktop.launch(webview=None, serve=lambda *a, **k: None, port=5600)


def test_default_serve_starts_real_flask_app(tmp_path):
    """_default_serve builds create_app and serves it on a daemon thread;
    the health endpoint answers. (Real Flask, no webview.)"""
    import urllib.request

    (tmp_path / "Sample [B3].dck").write_text(
        "[metadata]\nName=Sample\n[Commander]\n1 Test\n[Main]\n1 Forest\n",
        encoding="utf-8",
    )
    port = desktop.find_free_port()
    desktop._default_serve(str(tmp_path), "127.0.0.1", port)
    assert desktop.wait_until_up("127.0.0.1", port, timeout=10.0)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as r:
        assert r.status == 200


# ---------------------------------------------------------------------------
# Round-2 review 2026-08-20 (R2-P23)
# ---------------------------------------------------------------------------

def test_launch_refuses_to_open_a_window_on_a_dead_server(monkeypatch):
    """``wait_until_up``'s boolean is the whole point of the helper.

    It used to be discarded, so a slow/failed server start produced a
    native window showing ERR_CONNECTION_REFUSED — the exact case the
    probe exists to prevent. Now: no window, an explanatory error, and a
    clean teardown."""
    monkeypatch.setattr(desktop, "wait_until_up", lambda *a, **k: False)

    shutdowns = {"n": 0}
    releases = {"n": 0}

    class _Handle:
        def shutdown(self):
            shutdowns["n"] += 1

    class _Lock:
        def close(self):
            releases["n"] += 1

    class FakeWebview:
        @staticmethod
        def create_window(*a, **kw):  # pragma: no cover - must not run
            raise AssertionError("a window was opened onto a dead server")

        @staticmethod
        def start(**kw):  # pragma: no cover - must not run
            raise AssertionError("the GUI loop was started anyway")

    with pytest.raises(desktop.ServerStartError) as exc:
        desktop.launch(
            deck_dir="C:/decks", host="127.0.0.1", port=5601,
            webview=FakeWebview,
            serve=lambda *a, **k: _Handle(),
            _acquire_lock=lambda: _Lock(),
        )

    # The message says what failed and that nothing opened.
    assert "did not start" in str(exc.value)
    assert "Nothing was opened" in str(exc.value)
    # Teardown so a retry isn't blocked by our own server/lock.
    assert shutdowns["n"] == 1
    assert releases["n"] == 1


def test_launch_still_opens_when_the_server_comes_up(monkeypatch):
    """Guard on the guard: the happy path is unchanged."""
    monkeypatch.setattr(desktop, "wait_until_up", lambda *a, **k: True)
    opened = {"n": 0}

    class FakeWebview:
        @staticmethod
        def create_window(title, url, **kw):
            opened["n"] += 1

        @staticmethod
        def start(**kw):
            pass

    class _Lock:
        def close(self):
            pass

    url = desktop.launch(
        deck_dir="C:/decks", host="127.0.0.1", port=5602,
        webview=FakeWebview, serve=lambda *a, **k: None,
        _acquire_lock=lambda: _Lock(),
    )
    assert url == "http://127.0.0.1:5602/" and opened["n"] == 1


def test_second_instance_does_not_blank_the_first_ones_pid(tmp_path):
    """The lock file is opened non-truncating.

    A user who sees "already running" with no visible window needs the
    holder's pid; the old truncating open destroyed it from the very
    process that was about to report the failure."""
    import os

    lock_path = tmp_path / "instance.lock"
    held = desktop._acquire_instance_lock(lock_path)
    try:
        assert lock_path.read_text(encoding="ascii") == str(os.getpid())
        with pytest.raises(desktop.SingleInstanceError):
            desktop._acquire_instance_lock(lock_path)
        # The payload survived the failed attempt.
        assert lock_path.read_text(encoding="ascii") == str(os.getpid())
    finally:
        held.close()


def test_lock_pid_is_rewritten_by_the_new_holder(tmp_path):
    """Non-truncating open must not leave a STALE pid behind either: the
    process that actually takes the lock stamps its own."""
    import os

    lock_path = tmp_path / "instance.lock"
    lock_path.write_text("999999", encoding="ascii")
    lock = desktop._acquire_instance_lock(lock_path)
    try:
        assert lock_path.read_text(encoding="ascii") == str(os.getpid())
    finally:
        lock.close()


def test_main_reports_a_dead_server_and_exits_nonzero(monkeypatch, capsys):
    """A double-clicked EXE must get the message, not a traceback."""
    def _boom(**kw):
        raise desktop.ServerStartError("server did not start; nothing opened")

    monkeypatch.setattr(desktop, "launch", _boom)
    assert desktop.main([]) == 1
    assert "server did not start" in capsys.readouterr().out

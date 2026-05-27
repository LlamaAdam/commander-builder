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

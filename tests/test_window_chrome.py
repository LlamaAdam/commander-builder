"""Tests for FP-010 slice 3 -- window chrome (icon, single-instance, shutdown).

All tests are pure logic / wiring. No real native window is opened:
- pywebview is injected via fake objects.
- The Flask server is injected via a fake serve() callable.
- The instance lock is injected via a fake _acquire_lock callable.
- No real lock files are created in the test suite.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commander_builder import desktop


# --------------------------------------------------------------------------- #
# _InstanceLock / _acquire_instance_lock
# --------------------------------------------------------------------------- #

def test_acquire_instance_lock_returns_lock(tmp_path):
    """_acquire_instance_lock succeeds when no other process holds the lock."""
    lock_path = tmp_path / "instance.lock"
    lock = desktop._acquire_instance_lock(lock_path=lock_path)
    try:
        assert lock_path.exists()
    finally:
        lock.close()


def test_acquire_instance_lock_raises_on_second_acquire(tmp_path):
    """A second call on the same path raises SingleInstanceError."""
    lock_path = tmp_path / "instance.lock"
    lock1 = desktop._acquire_instance_lock(lock_path=lock_path)
    try:
        with pytest.raises(desktop.SingleInstanceError, match="already running"):
            desktop._acquire_instance_lock(lock_path=lock_path)
    finally:
        lock1.close()


def test_acquire_instance_lock_succeeds_after_close(tmp_path):
    """After close(), the lock can be re-acquired (simulates clean exit)."""
    lock_path = tmp_path / "instance.lock"
    lock1 = desktop._acquire_instance_lock(lock_path=lock_path)
    lock1.close()
    # Should not raise:
    lock2 = desktop._acquire_instance_lock(lock_path=lock_path)
    lock2.close()


def test_instance_lock_context_manager(tmp_path):
    """_InstanceLock works as a context manager."""
    lock_path = tmp_path / "instance.lock"
    with desktop._acquire_instance_lock(lock_path=lock_path):
        assert lock_path.exists()
    # After __exit__, re-acquire must succeed.
    lock = desktop._acquire_instance_lock(lock_path=lock_path)
    lock.close()


def test_lock_file_path_uses_localappdata_on_windows(monkeypatch):
    """On Windows, lock file lives under %LOCALAPPDATA%/commander-builder/."""
    monkeypatch.delenv("COMMANDER_BUILDER_LOCK_DIR", raising=False)
    monkeypatch.setattr(desktop.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    p = desktop._lock_file_path()
    assert "commander-builder" in str(p)
    assert p.name == "instance.lock"


def test_lock_file_path_env_override(monkeypatch, tmp_path):
    """COMMANDER_BUILDER_LOCK_DIR overrides the default lock-file location."""
    monkeypatch.setenv("COMMANDER_BUILDER_LOCK_DIR", str(tmp_path))
    p = desktop._lock_file_path()
    assert p.parent == tmp_path
    assert p.name == "instance.lock"


# --------------------------------------------------------------------------- #
# _icon_path
# --------------------------------------------------------------------------- #

def test_icon_path_returns_none_when_absent(tmp_path, monkeypatch):
    """_icon_path returns None when the icon file does not exist."""
    # Point __file__ to a directory that has no data/ subdir.
    monkeypatch.setattr(desktop, "__file__", str(tmp_path / "desktop.py"))
    result = desktop._icon_path()
    assert result is None


def test_icon_path_returns_path_when_present(tmp_path, monkeypatch):
    """_icon_path returns the Path when the icon file exists under data/."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    icon_file = data_dir / desktop._ICON_FILENAME
    icon_file.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(desktop, "__file__", str(tmp_path / "desktop.py"))
    result = desktop._icon_path()
    assert result == icon_file


def test_icon_path_meipass(monkeypatch, tmp_path):
    """Inside a PyInstaller bundle (_MEIPASS set), icon is resolved from there."""
    import sys
    meipass = tmp_path / "_MEIPASS"
    pkg_dir = meipass / "commander_builder"
    data_dir = pkg_dir / "data"
    data_dir.mkdir(parents=True)
    icon_file = data_dir / desktop._ICON_FILENAME
    icon_file.write_bytes(b"\x89PNG\r\n")
    # _MEIPASS doesn't exist in normal Python so we set it directly and
    # clean up afterward (monkeypatch.setattr requires the attr to exist).
    sys._MEIPASS = str(meipass)  # type: ignore[attr-defined]
    try:
        result = desktop._icon_path()
        assert result == icon_file
    finally:
        del sys._MEIPASS  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# _ServerHandle.shutdown
# --------------------------------------------------------------------------- #

def test_server_handle_shutdown_calls_fn():
    called = []
    t = threading.Thread(target=lambda: None, daemon=True)
    t.start()  # must be started before join() is valid
    handle = desktop._ServerHandle(t, lambda: called.append(True))
    handle.shutdown()
    assert called == [True]


def test_server_handle_shutdown_tolerates_error():
    def _bad_stop():
        raise RuntimeError("crash")
    t = threading.Thread(target=lambda: None, daemon=True)
    t.start()
    handle = desktop._ServerHandle(t, _bad_stop)
    handle.shutdown()  # must not propagate


# --------------------------------------------------------------------------- #
# launch() -- icon + instance-guard + closing-event wiring
# --------------------------------------------------------------------------- #

def _make_fake_lock():
    """Return a no-op fake _InstanceLock."""
    lock = MagicMock(spec=desktop._InstanceLock)
    lock.close = MagicMock()
    return lock


def _make_fake_server_handle():
    """Return a fake _ServerHandle with a tracked shutdown()."""
    shutdown_calls = []

    def _noop_run():
        pass  # thread exits immediately; is_alive() -> False, so join skips

    t = threading.Thread(target=_noop_run, daemon=True)
    t.start()  # start so join() doesn't raise RuntimeError
    handle = desktop._ServerHandle(t, lambda: shutdown_calls.append(True))
    handle._shutdown_calls = shutdown_calls
    return handle


def test_launch_passes_icon_to_start(monkeypatch, tmp_path):
    """launch() passes the icon path to webview.start() -- pywebview's icon
    is a start() kwarg, NOT a create_window() param (passing it to
    create_window raises TypeError on a real window)."""
    monkeypatch.setattr(desktop, "wait_until_up", lambda *a, **k: True)

    # Provide a real icon file.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    icon_file = data_dir / desktop._ICON_FILENAME
    icon_file.write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(desktop, "__file__", str(tmp_path / "desktop.py"))

    create_calls = {}
    start_calls = {}

    class FakeWindow:
        events = MagicMock()

    class FakeWebview:
        @staticmethod
        def create_window(title, url, **kw):
            create_calls.update(kw)
            return FakeWindow()
        @staticmethod
        def start(**kw):
            start_calls.update(kw)

    desktop.launch(
        deck_dir=None, host="127.0.0.1", port=9901,
        webview=FakeWebview,
        serve=lambda *a, **k: _make_fake_server_handle(),
        _acquire_lock=_make_fake_lock,
    )
    assert "icon" not in create_calls          # NOT a create_window param
    assert start_calls.get("icon") == str(icon_file)


def test_launch_no_icon_kwarg_when_absent(monkeypatch, tmp_path):
    """launch() calls start() with NO icon kwarg when the asset is absent
    (and never passes icon to create_window)."""
    monkeypatch.setattr(desktop, "wait_until_up", lambda *a, **k: True)
    monkeypatch.setattr(desktop, "__file__", str(tmp_path / "desktop.py"))

    create_calls = {}
    start_calls = {}

    class FakeWebview:
        @staticmethod
        def create_window(title, url, **kw):
            create_calls.update(kw)
            return None
        @staticmethod
        def start(**kw):
            start_calls.update(kw)

    desktop.launch(
        deck_dir=None, host="127.0.0.1", port=9902,
        webview=FakeWebview,
        serve=lambda *a, **k: _make_fake_server_handle(),
        _acquire_lock=_make_fake_lock,
    )
    assert "icon" not in create_calls
    assert "icon" not in start_calls


def test_launch_acquires_instance_lock(monkeypatch):
    """launch() calls _acquire_lock before opening the window."""
    monkeypatch.setattr(desktop, "wait_until_up", lambda *a, **k: True)
    monkeypatch.setattr(desktop, "_icon_path", lambda: None)

    acquired = []

    def fake_acquire():
        acquired.append(True)
        return _make_fake_lock()

    class FakeWebview:
        @staticmethod
        def create_window(*a, **k): return None
        @staticmethod
        def start(): pass

    desktop.launch(
        deck_dir=None, host="127.0.0.1", port=9903,
        webview=FakeWebview,
        serve=lambda *a, **k: _make_fake_server_handle(),
        _acquire_lock=fake_acquire,
    )
    assert acquired == [True]


def test_launch_registers_closing_event_for_shutdown(monkeypatch):
    """launch() wires the closing event so shutdown() is called on close."""
    monkeypatch.setattr(desktop, "wait_until_up", lambda *a, **k: True)
    monkeypatch.setattr(desktop, "_icon_path", lambda: None)

    server = _make_fake_server_handle()
    closing_handlers = []

    class FakeEvents:
        def __iadd__(self, fn):
            closing_handlers.append(fn)
            return self

    class FakeWindow:
        class events:
            closing = FakeEvents()

    class FakeWebview:
        @staticmethod
        def create_window(*a, **k): return FakeWindow()
        @staticmethod
        def start(): pass

    desktop.launch(
        deck_dir=None, host="127.0.0.1", port=9904,
        webview=FakeWebview,
        serve=lambda *a, **k: server,
        _acquire_lock=_make_fake_lock,
    )

    assert closing_handlers, "no closing handler registered"
    # Fire the closing event.
    closing_handlers[0]()
    assert server._shutdown_calls == [True]


def test_launch_raises_single_instance_error_propagates(monkeypatch):
    """If _acquire_lock raises SingleInstanceError, launch() propagates it."""
    monkeypatch.setattr(desktop, "wait_until_up", lambda *a, **k: True)

    def bad_acquire():
        raise desktop.SingleInstanceError("already running")

    class FakeWebview:
        @staticmethod
        def create_window(*a, **k): return None
        @staticmethod
        def start(): pass

    with pytest.raises(desktop.SingleInstanceError, match="already running"):
        desktop.launch(
            deck_dir=None, host="127.0.0.1", port=9905,
            webview=FakeWebview,
            serve=lambda *a, **k: _make_fake_server_handle(),
            _acquire_lock=bad_acquire,
        )


def test_main_returns_1_on_single_instance_error(monkeypatch):
    """main() prints and returns exit code 1 when already running."""
    monkeypatch.setattr(
        desktop, "launch",
        lambda **kw: (_ for _ in ()).throw(
            desktop.SingleInstanceError("already running")),
    )
    rc = desktop.main([])
    assert rc == 1

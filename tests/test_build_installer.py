"""Tests for scripts/build_installer.py (FP-010 slice 4).

All tests mock subprocess and filesystem so ISCC.exe does not need to be
present and no actual compilation is performed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Helpers to import the script under test without a real project install.
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _import_build_installer():
    """Import scripts/build_installer.py as a module (not on sys.path by default)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_installer", SCRIPTS_DIR / "build_installer.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bi():
    """Return a freshly imported build_installer module."""
    return _import_build_installer()


# ---------------------------------------------------------------------------
# find_iscc -- explicit override
# ---------------------------------------------------------------------------

class TestFindIsccOverride:
    def test_override_exists(self, bi, tmp_path):
        fake_iscc = tmp_path / "ISCC.exe"
        fake_iscc.write_bytes(b"fake")
        result = bi.find_iscc(override=str(fake_iscc))
        assert result == fake_iscc

    def test_override_missing_raises(self, bi, tmp_path):
        missing = tmp_path / "NoSuchISCC.exe"
        with pytest.raises(FileNotFoundError, match="not found at the specified path"):
            bi.find_iscc(override=str(missing))


# ---------------------------------------------------------------------------
# find_iscc -- PATH discovery
# ---------------------------------------------------------------------------

class TestFindIsccPath:
    def test_found_on_path(self, bi, tmp_path):
        fake_iscc = tmp_path / "ISCC.exe"
        fake_iscc.write_bytes(b"fake")
        with patch("shutil.which", return_value=str(fake_iscc)):
            result = bi.find_iscc()
        assert result == fake_iscc

    def test_not_on_path_falls_through(self, bi, tmp_path):
        """When not on PATH, _expand_candidates is tried; if none exist, raises."""
        with patch("shutil.which", return_value=None), \
             patch.object(bi, "_expand_candidates", return_value=[]):
            with pytest.raises(FileNotFoundError, match="Inno Setup"):
                bi.find_iscc()


# ---------------------------------------------------------------------------
# find_iscc -- candidate-path fallback
# ---------------------------------------------------------------------------

class TestFindIsccCandidates:
    def test_candidate_found(self, bi, tmp_path):
        fake_iscc = tmp_path / "ISCC.exe"
        fake_iscc.write_bytes(b"fake")
        with patch("shutil.which", return_value=None), \
             patch.object(bi, "_expand_candidates", return_value=[fake_iscc]):
            result = bi.find_iscc()
        assert result == fake_iscc

    def test_no_candidates_raises_helpful_message(self, bi):
        with patch("shutil.which", return_value=None), \
             patch.object(bi, "_expand_candidates", return_value=[]):
            with pytest.raises(FileNotFoundError) as exc_info:
                bi.find_iscc()
        msg = str(exc_info.value)
        assert "jrsoftware.org" in msg or "winget" in msg


# ---------------------------------------------------------------------------
# _run_iscc -- verifies subprocess.run is called correctly
# ---------------------------------------------------------------------------

class TestRunIscc:
    def test_calls_subprocess(self, bi, tmp_path):
        fake_iscc = tmp_path / "ISCC.exe"
        fake_iss = tmp_path / "test.iss"
        with patch("subprocess.run") as mock_run:
            bi._run_iscc(fake_iscc, fake_iss)
        mock_run.assert_called_once_with(
            [str(fake_iscc), str(fake_iss)], check=True
        )


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

class TestMain:
    def _patch_env(self, bi, tmp_path, *, dist_exists=True, iss_exists=True):
        """Set up minimal fake filesystem and return the patches as a context stack."""
        from contextlib import ExitStack
        stack = ExitStack()

        fake_iscc = tmp_path / "ISCC.exe"
        fake_iscc.write_bytes(b"fake")

        # Patch ROOT to tmp_path so ISS and dist paths resolve inside tmp_path
        fake_root = tmp_path
        stack.enter_context(patch.object(bi, "ROOT", fake_root))

        iss_dir = tmp_path / "packaging"
        iss_dir.mkdir()
        iss_file = iss_dir / "commander-builder.iss"
        if iss_exists:
            iss_file.write_text("; fake iss")
        stack.enter_context(patch.object(bi, "ISS", iss_file))

        dist_dir = tmp_path / "dist" / "CommanderBuilder"
        if dist_exists:
            dist_dir.mkdir(parents=True)

        stack.enter_context(patch.object(bi, "find_iscc", return_value=fake_iscc))
        stack.enter_context(patch.object(bi, "_run_iscc"))

        return stack, fake_iscc

    def test_happy_path_returns_zero(self, bi, tmp_path):
        with self._patch_env(bi, tmp_path)[0]:
            rc = bi.main([])
        assert rc == 0

    def test_missing_iss_returns_2(self, bi, tmp_path):
        with self._patch_env(bi, tmp_path, iss_exists=False)[0]:
            rc = bi.main([])
        assert rc == 2

    def test_missing_dist_returns_2(self, bi, tmp_path):
        with self._patch_env(bi, tmp_path, dist_exists=False)[0]:
            rc = bi.main([])
        assert rc == 2

    def test_iscc_not_found_returns_1(self, bi, tmp_path):
        stack, _ = self._patch_env(bi, tmp_path)
        with stack:
            # Override the find_iscc patch to raise
            with patch.object(bi, "find_iscc",
                               side_effect=FileNotFoundError("Inno Setup not found")):
                rc = bi.main([])
        assert rc == 1

    def test_run_iscc_called_with_iss(self, bi, tmp_path):
        stack, fake_iscc = self._patch_env(bi, tmp_path)
        with stack:
            mock_run = bi._run_iscc  # already patched to MagicMock by _patch_env
            bi.main([])
            # _run_iscc should have been called with (iscc_path, ISS)
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0]
            assert call_args[0] == fake_iscc
            assert str(call_args[1]).endswith("commander-builder.iss")

    def test_explicit_iscc_flag_forwarded(self, bi, tmp_path):
        """--iscc CLI flag is passed through to find_iscc."""
        stack, fake_iscc = self._patch_env(bi, tmp_path)
        captured = {}

        def capture_find(override=None):
            captured["override"] = override
            return fake_iscc

        with stack:
            with patch.object(bi, "find_iscc", side_effect=capture_find):
                bi.main(["--iscc", str(fake_iscc)])

        assert captured["override"] == str(fake_iscc)

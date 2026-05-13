"""Forge headless `sim` invocation wrapper.

Single responsibility: run one Forge sim and return (stdout, stderr, returncode,
duration). Does NOT parse — `log_parser.py` owns parsing. Does NOT pick decks —
the caller (curator, run_match) owns selection.

Hard requirements pinned by Phase 1A discovery:
  - cwd MUST be the install dir (where forge.profile.properties lives) or
    Forge crashes during init with ExceptionInInitializerError.
  - Decks must be passed as filenames (with .dck), and must already live in
    `<userDir>/decks/commander/`. Forge ignores `-D <directory>` in 2.0.12.
  - All decks go after a single `-d` flag. Multiple `-d` flags break it.

Usage:

    from commander_builder.forge_runner import ForgeRunner
    runner = ForgeRunner.locate()
    result = runner.run(
        deck_filenames=["A.dck", "B.dck", "C.dck", "D.dck"],
        num_games=3,
    )
    print(result.returncode, result.duration_sec)
    print(result.stdout)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_JRE = REPO_ROOT / "vendor" / "jre"
VENDOR_FORGE = REPO_ROOT / "vendor" / "forge"

# Phase 1B test sims clocked B3-B5 4-player games at 96-120s each. 180s/game
# gives ~50% headroom and surfaces hung sims faster than the old 240s budget.
DEFAULT_TIMEOUT_PER_GAME_SEC = 180
MIN_TIMEOUT_SEC = 300

# Days after which the bundled Forge jar is considered stale and worth
# replacing. New MTG sets ship roughly every 4-6 weeks; 90 days gives
# enough headroom that most installs don't bounce in and out of the
# warning, but not so much that errata-sensitive cards (Sephiroth, Vivi)
# silently misbehave.
FORGE_STALE_AGE_DAYS = 90

_FORGE_JAR_VERSION_RE = re.compile(
    r"forge-gui-desktop-(\d+(?:\.\d+)+)",
)


def _utcnow(tz=timezone.utc):
    """Indirection so tests can pin "now" deterministically."""
    return datetime.now(tz)


@dataclass
class ForgeVersionInfo:
    """Snapshot of the bundled Forge jar — version, build date, age.

    ``is_stale`` is conservative: True only when ``age_days`` is known
    AND exceeds ``FORGE_STALE_AGE_DAYS``. Missing build.txt or
    malformed timestamps leave ``is_stale=False`` so we don't alarm
    the user about unknowable state.
    """
    jar_path: Optional[Path] = None
    version: Optional[str] = None
    build_date: Optional[datetime] = None
    age_days: Optional[int] = None
    is_stale: bool = False


def detect_forge_version(forge_dir: Path = VENDOR_FORGE) -> ForgeVersionInfo:
    """Inspect the vendor/forge directory and return version metadata.

    Looks for ``forge-gui-desktop-*.jar`` and parses the version out of
    the filename (the only place the bundle reliably exposes it). Reads
    the optional ``build.txt`` for a real build timestamp; falls back
    to ``age_days=None`` when build.txt is missing or malformed.

    Always returns a ForgeVersionInfo — never raises. A missing jar
    surfaces as ``version=None, jar_path=None`` so callers can render a
    "Forge install not found" warning without try/except boilerplate.
    """
    info = ForgeVersionInfo()
    if not forge_dir.exists() or not forge_dir.is_dir():
        return info

    # Rank candidates by parsed version (semver-ish) — lexicographic
    # sort would put "2.0.10" before "2.0.12" because "0" < "2" at the
    # relevant position, so the prior sorted(...)[0] picked the OLDER
    # jar when a user kept both around after an upgrade.
    # Fat jars ("jar-with-dependencies") win over thin within the same
    # version because forge_runner.locate() runs the fat one.
    candidates: list[tuple[tuple[int, ...], bool, Path]] = []
    for jar_path in forge_dir.glob("forge-gui-desktop-*.jar"):
        m = _FORGE_JAR_VERSION_RE.search(jar_path.name)
        if not m:
            continue
        try:
            version_tuple = tuple(int(part) for part in m.group(1).split("."))
        except ValueError:
            continue
        is_fat = "jar-with-dependencies" in jar_path.name
        candidates.append((version_tuple, is_fat, jar_path))
    if not candidates:
        return info
    # Highest version first; within a version, fat jar first.
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _, _, jar = candidates[0]
    info.jar_path = jar
    m = _FORGE_JAR_VERSION_RE.search(jar.name)
    if m:
        info.version = m.group(1)

    build_txt = forge_dir / "build.txt"
    if build_txt.exists():
        try:
            text = build_txt.read_text(encoding="utf-8").strip()
            # Forge bundles a "YYYY-MM-DD HH:MM:SS" timestamp.
            info.build_date = datetime.strptime(
                text, "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=timezone.utc)
        except (OSError, ValueError):
            info.build_date = None

    if info.build_date is not None:
        delta = _utcnow() - info.build_date
        info.age_days = max(0, int(delta.total_seconds() // 86400))
        info.is_stale = info.age_days > FORGE_STALE_AGE_DAYS

    return info


@dataclass
class SimResult:
    cmd: list[str]
    returncode: Optional[int]
    duration_sec: float
    stdout: str
    stderr: str
    timed_out: bool
    error: Optional[str]
    # Forge writes a separate forge.log next to forge.profile.properties. It
    # often contains stack traces that never reach stdout (DB load failures,
    # rules-engine NPEs). Capture it so post-mortem analysis has the full
    # picture without re-running the sim.
    forge_log_tail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), default=str, **kwargs)


def _run_blocking(
    cmd: list[str],
    timeout: int,
    cwd: str,
) -> tuple[str, str, Optional[int], bool, Optional[str]]:
    """Battle-tested blocking subprocess.run path. Returns the captured
    streams + status flags. Used when the caller doesn't need streaming."""
    stdout = ""
    stderr = ""
    returncode: Optional[int] = None
    timed_out = False
    error: Optional[str] = None
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            encoding="utf-8",
            errors="replace",
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        error = f"Timed out after {timeout}s"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return stdout, stderr, returncode, timed_out, error


def _run_streaming(
    cmd: list[str],
    timeout: int,
    cwd: str,
    *,
    stream: bool = True,
    on_line: Optional[Callable[[str], None]] = None,
    abort_check: Optional[Callable[[str], bool]] = None,
) -> tuple[str, str, Optional[int], bool, Optional[str]]:
    """Streaming variant: spawn the process with `Popen`, read stdout
    line-by-line on a worker thread, optionally echoing each line to stdout
    or feeding it to `on_line`. The full captured output is returned at the
    end so downstream parsers (log_parser, game_analyzer) see exactly what
    they would have seen from the blocking path.

    ``abort_check`` (Sprint 1C) lets a caller terminate the subprocess
    based on the streaming output. Called once per stdout line; when it
    returns True the worker thread kills the Forge subprocess and exits.
    The captured stdout is whatever was emitted up to the abort point;
    callers downstream are expected to handle a partial sim (e.g. by
    synthesizing a Match Result from per-game winner lines).

    Stderr is captured to a separate buffer in the same thread so we don't
    deadlock on a full pipe. Timeout enforcement uses `proc.wait(timeout=)`
    after EOF — the streaming path doesn't add anything timeout-wise."""
    stdout_lines: list[str] = []
    stderr_buf: list[str] = []
    returncode: Optional[int] = None
    timed_out = False
    error: Optional[str] = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # line-buffered
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        return "", "", None, False, error

    aborted = {"flag": False}

    def _consume_stdout():
        for line in proc.stdout:  # type: ignore[union-attr]
            stdout_lines.append(line)
            if stream:
                # Re-encode for the terminal — Windows cp1252 chokes on emoji
                # in some Forge log lines. Match the buffer write convention
                # used elsewhere in the project.
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except UnicodeEncodeError:
                    sys.stdout.buffer.write(line.encode("utf-8", errors="replace"))
                    sys.stdout.flush()
            stripped = line.rstrip("\n")
            if on_line is not None:
                try:
                    on_line(stripped)
                except Exception:  # noqa: BLE001
                    # Don't let a buggy callback take down the sim.
                    pass
            if abort_check is not None and not aborted["flag"]:
                try:
                    if abort_check(stripped):
                        aborted["flag"] = True
                        # Kill the JVM child. The pipe will EOF and the
                        # outer wait() will return.
                        try:
                            proc.kill()
                        except Exception:  # noqa: BLE001
                            pass
                        # Don't break — drain the rest of the pipe so the
                        # buffered lines emitted before kill() took effect
                        # land in stdout_lines for downstream parsing.
                except Exception:  # noqa: BLE001
                    pass

    def _consume_stderr():
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_buf.append(line)

    t_out = threading.Thread(target=_consume_stdout, daemon=True)
    t_err = threading.Thread(target=_consume_stderr, daemon=True)
    t_out.start()
    t_err.start()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        error = f"Timed out after {timeout}s"
        proc.kill()
        try:
            returncode = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    # Drain the consumer threads so we don't lose trailing lines.
    t_out.join(timeout=5)
    t_err.join(timeout=5)

    return "".join(stdout_lines), "".join(stderr_buf), returncode, timed_out, error


@dataclass
class ForgeRunner:
    java_path: Path
    forge_jar: Path
    forge_dir: Path

    @classmethod
    def locate(cls) -> "ForgeRunner":
        """Resolve repo-local Java + Forge or raise. No system fallback here —
        the verifier (Phase 1A) handles that case; the runner assumes the
        repo-local install succeeded."""
        java = VENDOR_JRE / "bin" / "java.exe"
        if not java.exists():
            java_alt = VENDOR_JRE / "bin" / "java"
            if java_alt.exists():
                java = java_alt
            else:
                sys_java = shutil.which("java")
                if not sys_java:
                    raise FileNotFoundError(
                        "Java not found. Expected vendor/jre/bin/java[.exe] or `java` on PATH."
                    )
                java = Path(sys_java)
        jars = sorted(VENDOR_FORGE.glob("forge-gui-desktop-*.jar"))
        fat = [j for j in jars if "jar-with-dependencies" in j.name]
        jar = (fat or jars or [None])[0]
        if jar is None:
            raise FileNotFoundError(
                f"Forge jar not found in {VENDOR_FORGE}. Expected forge-gui-desktop-*.jar."
            )
        return cls(java_path=java, forge_jar=jar, forge_dir=VENDOR_FORGE)

    def run(
        self,
        deck_filenames: list[str],
        num_games: int,
        game_format: str = "commander",
        timeout_sec: Optional[int] = None,
        stream: bool = False,
        on_line: Optional["Callable[[str], None]"] = None,
        abort_check: Optional["Callable[[str], bool]"] = None,
    ) -> SimResult:
        """Run one Forge sim. Returns a SimResult with full stdout captured.

        `stream=True` prints sim progress to stdout as it arrives (one line at
        a time) instead of waiting for the full sim to finish — useful for
        long curations where the user wants to see something happening. Costs
        a thread to consume the pipe; otherwise behavior is identical to the
        blocking path.

        `on_line` is an optional callback invoked once per stdout line as it
        arrives. Use this to drive a progress bar or log to a file without
        echoing to the terminal.

        `abort_check` (Sprint 1C) is invoked per stdout line; when it
        returns True the worker thread kills the Forge subprocess. Used by
        compare_versions to terminate a pod early when its in-pod margin
        becomes uncatchable. Implies the streaming path.

        When neither `stream` nor `on_line` nor `abort_check` is set, falls
        back to the battle-tested `subprocess.run` path."""
        if not deck_filenames:
            raise ValueError("deck_filenames must contain at least 2 decks.")
        if game_format == "commander" and len(deck_filenames) != 4:
            # Forge's commander sim expects a 4-player pod. 2/3-player works in
            # constructed but not commander; fail loud rather than produce
            # confusing results.
            raise ValueError(
                f"commander format expects exactly 4 decks, got {len(deck_filenames)}."
            )
        if num_games < 1:
            raise ValueError("num_games must be >= 1.")

        cmd = [
            str(self.java_path),
            "-jar",
            str(self.forge_jar),
            "sim",
            "-f",
            game_format,
            "-n",
            str(num_games),
            "-d",
            *deck_filenames,
        ]

        timeout = timeout_sec or max(MIN_TIMEOUT_SEC, num_games * DEFAULT_TIMEOUT_PER_GAME_SEC)
        started = datetime.now()

        if stream or on_line is not None or abort_check is not None:
            stdout, stderr, returncode, timed_out, error = _run_streaming(
                cmd, timeout, str(self.forge_dir),
                stream=stream, on_line=on_line, abort_check=abort_check,
            )
        else:
            stdout, stderr, returncode, timed_out, error = _run_blocking(
                cmd, timeout, str(self.forge_dir),
            )

        duration = (datetime.now() - started).total_seconds()
        return SimResult(
            cmd=cmd,
            returncode=returncode,
            duration_sec=duration,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            error=error,
            forge_log_tail=self._read_forge_log_tail(),
        )

    @staticmethod
    def _read_forge_log_tail_impl(forge_dir: Path, max_bytes: int = 64 * 1024) -> str:
        """Read the tail of forge.log. Static helper so the streaming runner
        path doesn't need a ForgeRunner instance to call it."""
        log_path = forge_dir / "forge.log"
        try:
            if not log_path.exists():
                return ""
            size = log_path.stat().st_size
            with log_path.open("rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                data = f.read()
            return data.decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _read_forge_log_tail(self, max_bytes: int = 64 * 1024) -> str:
        """Return the last ~64KB of vendor/forge/forge.log if present.

        Forge appends across runs, so the full file is unbounded; the tail is
        what's relevant to the just-finished sim. Best-effort — a missing or
        unreadable log returns empty rather than crashing the run."""
        log_path = self.forge_dir / "forge.log"
        try:
            if not log_path.exists():
                return ""
            size = log_path.stat().st_size
            with log_path.open("rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                data = f.read()
            return data.decode("utf-8", errors="replace")
        except OSError:
            return ""


if __name__ == "__main__":
    # Smoke entry point: `python -m commander_builder.forge_runner deck1.dck ...`
    import sys
    if len(sys.argv) < 5:
        print("Usage: forge_runner.py <deck1.dck> <deck2.dck> <deck3.dck> <deck4.dck> [num_games=2]")
        sys.exit(2)
    decks = sys.argv[1:5]
    n = int(sys.argv[5]) if len(sys.argv) > 5 else 2
    r = ForgeRunner.locate().run(decks, num_games=n)
    print(f"returncode={r.returncode} duration={r.duration_sec:.1f}s timed_out={r.timed_out}")
    if r.error:
        print(f"error: {r.error}")
    print("--- stdout (last 30 lines) ---")
    print("\n".join(r.stdout.splitlines()[-30:]))

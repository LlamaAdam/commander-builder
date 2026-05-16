"""commander-doctor — environment health check.

Sanity-check that the commander_builder environment is wired up correctly.
Run before troubleshooting "why isn't this working?". Reports a green/yellow/
red status per check and exits non-zero if any RED check fails.

Checks performed:

  Python:        version + executable path
  Package:       importable + editable install detected
  Forge:         vendor/forge present + jar visible + Java reachable
  Decks dir:     Forge deck directory exists + at least one [B<n>].dck
  Knowledge log: SQLite file accessible + schema present
  Scryfall:      cache dir writable
  EDHREC:        cache dir writable
  Anthropic:     API key set (yellow if missing — only needed for claude_*)
  Ollama:        daemon reachable (yellow if not — only needed for ollama_*)
  Anthropic SDK: importable (yellow if not — only needed for claude_*)

Output goes to stdout; --json for structured form. Exit codes:
  0   all GREEN, project is healthy
  1   one or more RED checks failed (broken core)
  2   only YELLOW (degraded but functional)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .forge_runner import VENDOR_FORGE, VENDOR_JRE
from .knowledge_log import DEFAULT_DB_PATH, init_db, stats_summary

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRYFALL_CACHE = REPO_ROOT / ".cache" / "scryfall"
EDHREC_CACHE = REPO_ROOT / ".cache" / "edhrec"
DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"

# Status levels.
GREEN = "green"
YELLOW = "yellow"
RED = "red"


@dataclass
class CheckResult:
    name: str
    status: str               # green | yellow | red
    message: str
    detail: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def worst_status(self) -> str:
        statuses = [c.status for c in self.checks]
        if RED in statuses:
            return RED
        if YELLOW in statuses:
            return YELLOW
        return GREEN

    @property
    def exit_code(self) -> int:
        return {RED: 1, YELLOW: 2, GREEN: 0}[self.worst_status]

    def to_dict(self) -> dict:
        return {
            "worst_status": self.worst_status,
            "exit_code": self.exit_code,
            "checks": [c.to_dict() for c in self.checks],
        }


# --- Individual checks -----------------------------------------------------

def _check_python() -> CheckResult:
    info = sys.version_info
    if info < (3, 10):
        return CheckResult(
            "python", RED,
            f"Python {info.major}.{info.minor} is below the project minimum (3.10).",
            f"executable: {sys.executable}",
        )
    return CheckResult(
        "python", GREEN,
        f"Python {info.major}.{info.minor}.{info.micro}",
        f"executable: {sys.executable}",
    )


def _check_package() -> CheckResult:
    try:
        import commander_builder  # noqa: F401
    except ImportError as exc:
        return CheckResult(
            "package", RED,
            "commander_builder package not importable — run `pip install -e .`",
            str(exc),
        )
    pkg_path = Path(commander_builder.__file__).parent
    is_editable = "site-packages" not in str(pkg_path)
    return CheckResult(
        "package", GREEN,
        f"commander_builder importable ({'editable' if is_editable else 'installed'})",
        f"location: {pkg_path}",
    )


def _check_forge() -> CheckResult:
    if not VENDOR_FORGE.exists():
        return CheckResult(
            "forge", YELLOW,
            "vendor/forge missing — live sims will fail. See setup/forge/README.md.",
            f"expected: {VENDOR_FORGE}",
        )
    jars = list(VENDOR_FORGE.glob("forge-gui-desktop-*.jar"))
    if not jars:
        return CheckResult(
            "forge", RED,
            "vendor/forge exists but no forge-gui-desktop-*.jar found.",
            f"checked: {VENDOR_FORGE}",
        )
    return CheckResult(
        "forge", GREEN,
        f"Forge jar present: {jars[0].name}",
        f"size: {jars[0].stat().st_size // 1024 // 1024} MB",
    )


def _check_java() -> CheckResult:
    """Pick repo-local JRE first, fall back to system PATH."""
    java = VENDOR_JRE / "bin" / "java.exe"
    if not java.exists():
        java_alt = VENDOR_JRE / "bin" / "java"
        if java_alt.exists():
            java = java_alt
        else:
            sys_java = shutil.which("java")
            if not sys_java:
                return CheckResult(
                    "java", YELLOW,
                    "No Java found in vendor/jre or PATH — Forge sims will fail.",
                    f"checked: {VENDOR_JRE}",
                )
            return CheckResult(
                "java", GREEN, f"java on PATH: {sys_java}",
            )
    return CheckResult(
        "java", GREEN, f"vendor JRE present: {java}",
    )


def _check_decks_dir() -> CheckResult:
    if not DECK_DIR.exists():
        return CheckResult(
            "decks_dir", YELLOW,
            "Deck directory missing — run `commander-import` to populate it.",
            f"expected: {DECK_DIR}",
        )
    deck_count = sum(1 for _ in DECK_DIR.glob("*.dck"))
    if deck_count == 0:
        return CheckResult(
            "decks_dir", YELLOW,
            "Deck directory empty — run `commander-import --harvest <bracket>`.",
            f"path: {DECK_DIR}",
        )
    return CheckResult(
        "decks_dir", GREEN,
        f"{deck_count} decks on disk",
        f"path: {DECK_DIR}",
    )


def _check_knowledge_log() -> CheckResult:
    try:
        init_db(DEFAULT_DB_PATH)
        stats = stats_summary(db_path=DEFAULT_DB_PATH)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "knowledge_log", RED,
            f"knowledge_log open failed: {type(exc).__name__}",
            str(exc),
        )
    return CheckResult(
        "knowledge_log", GREEN,
        f"{stats['total']} iterations across {stats['unique_decks']} decks",
        f"path: {DEFAULT_DB_PATH}",
    )


def _check_cache_dir(name: str, path: Path) -> CheckResult:
    try:
        path.mkdir(parents=True, exist_ok=True)
        # Sanity-check writability.
        probe = path / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            name, RED,
            f"{name} cache dir not writable: {exc}",
            f"path: {path}",
        )
    return CheckResult(name, GREEN, f"writable", f"path: {path}")


def _check_anthropic_key() -> CheckResult:
    if "ANTHROPIC_API_KEY" not in os.environ:
        return CheckResult(
            "anthropic_key", YELLOW,
            "ANTHROPIC_API_KEY not set — claude_verdict / claude_propose will fall back.",
        )
    val = os.environ["ANTHROPIC_API_KEY"]
    return CheckResult(
        "anthropic_key", GREEN, f"ANTHROPIC_API_KEY set ({len(val)} chars)",
    )


def _check_anthropic_sdk() -> CheckResult:
    try:
        import anthropic  # noqa: F401
        return CheckResult("anthropic_sdk", GREEN, "anthropic SDK importable")
    except ImportError:
        return CheckResult(
            "anthropic_sdk", YELLOW,
            "`anthropic` not installed — claude backends unavailable. "
            "Install with `pip install -e \".[claude]\"`.",
        )


def _check_ollama(url: str = "http://localhost:11434/api/tags") -> CheckResult:
    """Reach the daemon via /api/tags (lists installed models). Cheap call."""
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError):
        return CheckResult(
            "ollama", YELLOW,
            "Ollama daemon not reachable — ollama_* backends unavailable.",
            f"checked: {url}",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "ollama", YELLOW,
            f"Ollama unexpected response: {type(exc).__name__}",
            str(exc),
        )
    models = data.get("models", []) if isinstance(data, dict) else []
    return CheckResult(
        "ollama", GREEN,
        f"daemon reachable, {len(models)} model(s) installed",
        f"models: {[m.get('name') for m in models[:3]]}",
    )


# --- Orchestration ---------------------------------------------------------

def run_doctor(skip_ollama: bool = False) -> DoctorReport:
    """Run all checks and return a DoctorReport. `skip_ollama=True` for tests
    so we don't depend on the daemon being either reachable or unreachable."""
    report = DoctorReport()
    report.checks.append(_check_python())
    report.checks.append(_check_package())
    report.checks.append(_check_forge())
    report.checks.append(_check_java())
    report.checks.append(_check_decks_dir())
    report.checks.append(_check_knowledge_log())
    report.checks.append(_check_cache_dir("scryfall_cache", SCRYFALL_CACHE))
    report.checks.append(_check_cache_dir("edhrec_cache", EDHREC_CACHE))
    report.checks.append(_check_anthropic_key())
    report.checks.append(_check_anthropic_sdk())
    if not skip_ollama:
        report.checks.append(_check_ollama())
    return report


def format_text(report: DoctorReport) -> str:
    icons = {GREEN: "OK ", YELLOW: "WARN", RED: "FAIL"}
    lines = []
    lines.append("=" * 60)
    lines.append(" Commander Builder — environment health check")
    lines.append("=" * 60)
    for c in report.checks:
        icon = icons.get(c.status, "?")
        lines.append(f"  [{icon}] {c.name:<18s} {c.message}")
        if c.detail:
            lines.append(f"           {c.detail}")
    lines.append("")
    lines.append(f"Worst status: {report.worst_status.upper()}  (exit code {report.exit_code})")
    if report.worst_status == GREEN:
        lines.append("Everything looks good.")
    elif report.worst_status == YELLOW:
        lines.append("Project is functional but some optional backends are unavailable.")
    else:
        lines.append("CRITICAL: at least one core check failed. Address [FAIL] items first.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="commander-doctor",
                                description="Environment health check.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON.")
    p.add_argument("--skip-ollama", action="store_true",
                   help="Don't attempt Ollama daemon connection.")
    args = p.parse_args(argv)

    # Load external credentials BEFORE running the env-var checks so
    # ``_check_anthropic_key`` sees keys that the user has configured
    # in ~/.commander-builder/credentials. Shell env still wins.
    from ._secrets import load_credentials
    load_credentials(quiet=True)

    report = run_doctor(skip_ollama=args.skip_ollama)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        try:
            print(format_text(report))
        except UnicodeEncodeError:
            sys.stdout.buffer.write((format_text(report) + "\n").encode("utf-8", errors="replace"))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

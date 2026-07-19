"""Phase 1A — Forge headless verifier.

Confirms that Forge is installed, Java runs it, and headless `sim` mode produces
output we can later parse. Touches no external services. Writes everything it
finds (and Forge's stdout/stderr/forge.log) to `verify_output/` for human review.

Run from the repo root:

    python -m commander_builder.verify_forge

Or:

    python src/commander_builder/verify_forge.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# How many games per test match. Small on purpose — Phase 1A is a smoke test,
# not a benchmark.
NUM_GAMES = 3

# Per-match timeout. Heuristic AI on a few games shouldn't take longer than this;
# if it does, something is wrong (deadlock, GUI launching, missing cards looping).
MATCH_TIMEOUT_SEC = 600

# Repo-root vendor paths. The verifier checks these BEFORE falling back to system
# locations, so users can run a self-contained install by dropping a portable JRE
# and Forge release into vendor/. See vendor/README.md.
REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_JRE = REPO_ROOT / "vendor" / "jre"
VENDOR_FORGE = REPO_ROOT / "vendor" / "forge"


@dataclass
class Findings:
    """What the verifier discovered. Saved as JSON for the user to inspect."""

    timestamp: str
    platform: str
    python_version: str
    java_path: Optional[str] = None
    java_version: Optional[str] = None
    forge_install_dir: Optional[str] = None
    forge_jar: Optional[str] = None
    forge_userdata_dir: Optional[str] = None
    decks_dir: Optional[str] = None
    constructed_decks: list[str] = field(default_factory=list)
    commander_decks: list[str] = field(default_factory=list)
    constructed_test: dict = field(default_factory=dict)
    commander_test: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ---------- discovery helpers ----------


def find_java() -> tuple[Optional[str], Optional[str]]:
    """Return (java_path, version_string) or (None, None) if not found.

    Order of resolution:
      1. vendor/jre/bin/java[.exe] — repo-local portable JRE
      2. shutil.which("java") — system PATH
    """
    candidates: list[Path] = [
        VENDOR_JRE / "bin" / "java.exe",
        VENDOR_JRE / "bin" / "java",
    ]
    java: Optional[str] = None
    for c in candidates:
        if c.is_file():
            java = str(c)
            break
    if java is None:
        java = shutil.which("java")
    if not java:
        return None, None
    try:
        # `java -version` prints to stderr.
        # env scrub: no JVM we spawn should inherit the Anthropic
        # credential _secrets.load_credentials() may have exported —
        # see forge_runner.scrubbed_child_env() for the rationale.
        from .forge_runner import scrubbed_child_env
        result = subprocess.run(
            [java, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=scrubbed_child_env(),
        )
        version = (result.stderr or result.stdout).strip().splitlines()[0]
        return java, version
    except Exception as exc:
        return java, f"<error: {exc}>"


def candidate_forge_dirs() -> list[Path]:
    """Likely locations for Forge.

    vendor/forge/ is checked FIRST. Users can drop the Forge release directly
    in vendor/forge/ OR drop the extracted release directory inside it (e.g.
    vendor/forge/forge-gui-desktop-1.6.62/) — both shapes are handled.
    """
    candidates: list[Path] = []

    # Vendored install — check vendor/forge/ itself, then any single subdirectory.
    if VENDOR_FORGE.is_dir():
        candidates.append(VENDOR_FORGE)
        for sub in sorted(VENDOR_FORGE.iterdir()):
            if sub.is_dir():
                candidates.append(sub)

    # System install fallbacks.
    env = os.environ
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = env.get(key)
        if base:
            candidates.append(Path(base) / "Forge")
    candidates.append(Path("C:/Forge"))
    user = env.get("USERPROFILE")
    if user:
        candidates.append(Path(user) / "Documents" / "Forge")

    # De-dupe while preserving order.
    seen: set[Path] = set()
    out: list[Path] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def find_forge_install() -> Optional[Path]:
    for cand in candidate_forge_dirs():
        if cand.is_dir():
            # A Forge install dir contains a jar matching forge-gui-*.jar
            # OR a `forge.exe` / `forge.cmd` launcher.
            jars = list(cand.glob("forge-gui-*.jar"))
            launchers = list(cand.glob("forge.*"))
            if jars or launchers:
                return cand
    return None


def find_forge_jar(install_dir: Path) -> Optional[Path]:
    """Pick the most likely jar — prefer `-jar-with-dependencies` if present."""
    jars = sorted(install_dir.glob("forge-gui-*.jar"))
    if not jars:
        return None
    fat = [j for j in jars if "jar-with-dependencies" in j.name]
    return fat[0] if fat else jars[-1]


def find_userdata_dir(install_dir: Path) -> Optional[Path]:
    """Find a Forge userdata dir containing a `decks/` subfolder.

    Order matches the precedence Forge itself uses when forge.profile.properties
    is present: project-local `<install>/userdata` wins, then the platform
    default, then the install dir itself.
    """
    candidates = [
        install_dir / "userdata",
        Path(os.environ.get("APPDATA", "")) / "Forge" if os.environ.get("APPDATA") else None,
        Path(os.environ.get("LOCALAPPDATA", "")) / "Forge" if os.environ.get("LOCALAPPDATA") else None,
        install_dir / "res",
        install_dir,
    ]
    for c in candidates:
        if c and c.is_dir() and (c / "decks").is_dir():
            return c
    return None


def list_decks(decks_dir: Path) -> tuple[list[Path], list[Path]]:
    """Return (constructed, commander) deck paths.

    Forge stores deck files (`.dck`) in subfolders by format. `decks/constructed`
    and `decks/commander` are the typical locations.
    """
    constructed_dir = decks_dir / "constructed"
    commander_dir = decks_dir / "commander"
    constructed = sorted(constructed_dir.glob("*.dck")) if constructed_dir.is_dir() else []
    commander = sorted(commander_dir.glob("*.dck")) if commander_dir.is_dir() else []
    return constructed, commander


def list_bundled_decks(install_dir: Path) -> tuple[list[Path], list[Path]]:
    """Fallback: read decks shipped inside the install's `res/quest/` tree.

    Forge releases bundle hundreds of precons under `res/quest/precons/` and
    `res/quest/commanderprecons/`. These are usable as `sim` opponents without
    the user ever launching Forge interactively.
    """
    quest = install_dir / "res" / "quest"
    constructed_dir = quest / "precons"
    commander_dir = quest / "commanderprecons"
    constructed = sorted(constructed_dir.glob("*.dck")) if constructed_dir.is_dir() else []
    commander = sorted(commander_dir.glob("*.dck")) if commander_dir.is_dir() else []
    return constructed, commander


# ---------- match runner ----------


def deck_id_from_path(deck_path: Path) -> str:
    """Forge's `sim` accepts a deck filename when it ends in `.<3 alnum>`.

    Quoting from `forge sim -h`:
      "deck is treated as file if it ends with a dot followed by three numbers
       or letters"

    So we pass `<name>.dck` and Forge will resolve it inside the directory
    given via `-D`.
    """
    return deck_path.name


def run_sim(
    java: str,
    jar: Path,
    deck_paths: list[Path],
    game_format: str,
    num_games: int,
    output_dir: Path,
    label: str,
) -> dict:
    """Run a Forge sim match and capture everything.

    Returns a dict with: cmd, returncode, stdout_path, stderr_path, forge_log_path,
    duration_sec, error (if any).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    deck_args = [deck_id_from_path(p) for p in deck_paths]

    # Forge's `sim` flags (from the in-binary help text):
    #   -d  one or more deck names/filenames
    #   -D  absolute directory containing those decks
    #   -f  format
    #   -n  number of games
    # All chosen decks must live in the same directory for `-D` to work; the
    # verifier picks decks from a single source so this holds.
    deck_dir = deck_paths[0].parent if deck_paths else Path()

    # Forge ignores `-D` in 2.0.12 — it always reads from
    # `<userDir>/decks/<format>/`. The verifier ensures decks live there
    # (via forge.profile.properties + a project-local userdata) before
    # invoking sim, so we just pass deck filenames.
    cmd = [
        java,
        "-jar",
        str(jar),
        "sim",
        "-f",
        game_format,
        "-n",
        str(num_games),
        "-d",
        *deck_args,
    ]

    stdout_path = output_dir / f"{label}_stdout.txt"
    stderr_path = output_dir / f"{label}_stderr.txt"
    info_path = output_dir / f"{label}_meta.json"

    started = datetime.now()
    error: Optional[str] = None
    returncode: Optional[int] = None
    timed_out = False

    try:
        # env scrub: the Forge JVM must never inherit the Anthropic
        # credential — see forge_runner.scrubbed_child_env().
        from .forge_runner import scrubbed_child_env
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=MATCH_TIMEOUT_SEC,
            cwd=str(jar.parent),
            env=scrubbed_child_env(),
        )
        stdout_path.write_text(proc.stdout, encoding="utf-8")
        stderr_path.write_text(proc.stderr, encoding="utf-8")
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        error = f"Timed out after {MATCH_TIMEOUT_SEC}s"
        timed_out = True
        stdout_path.write_text(exc.stdout or "", encoding="utf-8")
        stderr_path.write_text(exc.stderr or "", encoding="utf-8")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    duration = (datetime.now() - started).total_seconds()

    # Try to grab Forge's own log file if it exists. Path is platform/install
    # dependent; we copy from any plausible location.
    forge_log_copies: list[str] = []
    log_candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Forge" / "logs" / "forge.log"
        if os.environ.get("LOCALAPPDATA") else None,
        jar.parent / "forge.log",
        jar.parent / "logs" / "forge.log",
    ]
    for c in log_candidates:
        if c and c.is_file():
            dest = output_dir / f"{label}_forge.log"
            try:
                shutil.copyfile(c, dest)
                forge_log_copies.append(str(dest))
            except Exception:
                pass

    info = {
        "label": label,
        "cmd": cmd,
        "returncode": returncode,
        "duration_sec": round(duration, 2),
        "timed_out": timed_out,
        "error": error,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "forge_log_copies": forge_log_copies,
        "decks_used": [str(p) for p in deck_paths],
        "format": game_format,
        "num_games": num_games,
    }
    info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


# ---------- main ----------


def header(msg: str) -> None:
    print(f"\n{'=' * 70}\n{msg}\n{'=' * 70}")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    output_dir = repo_root / "verify_output"
    output_dir.mkdir(exist_ok=True)

    findings = Findings(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        platform=sys.platform,
        python_version=sys.version.split()[0],
    )

    header("Step 1: Java")
    java, java_version = find_java()
    findings.java_path = java
    findings.java_version = java_version
    if not java:
        findings.notes.append("FATAL: java not on PATH. Install JRE 8+ and re-run.")
        print("ERROR: java not found on PATH.")
        _save_findings(output_dir, findings)
        return 1
    print(f"Java: {java}\nVersion: {java_version}")

    header("Step 2: Forge install")
    install = find_forge_install()
    if not install:
        findings.notes.append(
            "FATAL: Forge install directory not found in any expected location. "
            f"Checked: {[str(c) for c in candidate_forge_dirs()]}"
        )
        print("ERROR: Forge install not found. Checked these paths:")
        for c in candidate_forge_dirs():
            print(f"  - {c} {'(missing)' if not c.exists() else '(no jar/launcher)'}")
        print("\nIf Forge is installed elsewhere, set the FORGE_INSTALL_DIR env var "
              "and re-run, or move/symlink the install to one of the checked paths.")
        _save_findings(output_dir, findings)
        return 1
    findings.forge_install_dir = str(install)
    print(f"Forge install: {install}")

    jar = find_forge_jar(install)
    if not jar:
        findings.notes.append(f"FATAL: no forge-gui-*.jar found in {install}")
        print(f"ERROR: no forge-gui-*.jar in {install}")
        _save_findings(output_dir, findings)
        return 1
    findings.forge_jar = str(jar)
    print(f"Jar: {jar.name}")

    header("Step 3: Userdata + decks")
    userdata = find_userdata_dir(install)
    constructed: list[Path] = []
    commander: list[Path] = []
    if userdata:
        findings.forge_userdata_dir = str(userdata)
        decks_dir = userdata / "decks"
        findings.decks_dir = str(decks_dir)
        print(f"Userdata: {userdata}")
        print(f"Decks dir: {decks_dir}")
        constructed, commander = list_decks(decks_dir)
    else:
        findings.notes.append(
            "Userdata `decks/` not found — falling back to bundled `res/quest/` decks."
        )
        print("Userdata `decks/` not found — falling back to bundled `res/quest/` decks.")

    if not constructed and not commander:
        bundled_constructed, bundled_commander = list_bundled_decks(install)
        if bundled_constructed or bundled_commander:
            print(
                f"Bundled decks under res/quest: "
                f"{len(bundled_constructed)} precons, {len(bundled_commander)} commander precons"
            )
            findings.notes.append(
                f"Using bundled decks: {len(bundled_constructed)} precons, "
                f"{len(bundled_commander)} commanderprecons"
            )
            constructed, commander = bundled_constructed, bundled_commander
        else:
            findings.notes.append(
                "No decks found in userdata or bundled res/quest/. "
                "Phase 1A cannot proceed without decks."
            )
            print("WARN: no decks anywhere. Skipping sim runs.")
            _save_findings(output_dir, findings)
            return 1
    findings.constructed_decks = [p.name for p in constructed]
    findings.commander_decks = [p.name for p in commander]
    print(f"Constructed decks found: {len(constructed)}")
    for p in constructed[:10]:
        print(f"  - {p.name}")
    if len(constructed) > 10:
        print(f"  ... and {len(constructed) - 10} more")
    print(f"Commander decks found: {len(commander)}")
    for p in commander[:10]:
        print(f"  - {p.name}")
    if len(commander) > 10:
        print(f"  ... and {len(commander) - 10} more")

    # ---------- Test 1: 2-player constructed ----------
    header("Step 4: 2-player constructed sim (3 games)")
    if len(constructed) < 2:
        msg = (
            f"SKIP: need >=2 constructed decks, found {len(constructed)}. "
            "Open Forge once and create/import a couple of constructed decks."
        )
        print(msg)
        findings.constructed_test = {"skipped": True, "reason": msg}
    else:
        picks = constructed[:2]
        print(f"Decks: {[p.name for p in picks]}")
        info = run_sim(
            java=java,
            jar=jar,
            deck_paths=picks,
            game_format="constructed",
            num_games=NUM_GAMES,
            output_dir=output_dir,
            label="constructed",
        )
        findings.constructed_test = info
        _print_run_summary(info)

    # ---------- Test 2: 4-player commander ----------
    header("Step 5: 4-player commander sim (3 games)")
    if len(commander) < 4:
        msg = (
            f"SKIP: need >=4 commander decks for a 4-player pod, found {len(commander)}. "
            "Open Forge once and create/import 4+ commander decks, or import some via "
            "the audit pipeline once Phase 1B is built."
        )
        print(msg)
        findings.commander_test = {"skipped": True, "reason": msg}
    else:
        picks = commander[:4]
        print(f"Decks: {[p.name for p in picks]}")
        info = run_sim(
            java=java,
            jar=jar,
            deck_paths=picks,
            game_format="commander",
            num_games=NUM_GAMES,
            output_dir=output_dir,
            label="commander",
        )
        findings.commander_test = info
        _print_run_summary(info)

    _save_findings(output_dir, findings)
    header("Done")
    print(f"All output saved to: {output_dir}")
    print("Next: paste the contents of verify_output/findings.json and any *_stdout.txt "
          "files back so we can design the log parser based on actual Forge output.")
    return 0


def _print_run_summary(info: dict) -> None:
    if info.get("error"):
        print(f"  status: ERROR ({info['error']})")
    elif info.get("returncode") == 0:
        print(f"  status: OK (exit 0, {info['duration_sec']}s)")
    else:
        print(f"  status: non-zero exit {info.get('returncode')} ({info['duration_sec']}s)")
    print(f"  stdout: {info['stdout_path']}")
    print(f"  stderr: {info['stderr_path']}")
    if info.get("forge_log_copies"):
        for p in info["forge_log_copies"]:
            print(f"  forge.log copied to: {p}")
    # Quick peek at stdout: first/last few lines, to flag obvious failure modes.
    try:
        text = Path(info["stdout_path"]).read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if lines:
            print("  --- stdout head ---")
            for ln in lines[:5]:
                print(f"    {ln}")
            if len(lines) > 10:
                print("    ...")
                print("  --- stdout tail ---")
                for ln in lines[-5:]:
                    print(f"    {ln}")
            # Heuristic warnings from common Forge issues.
            joined = "\n".join(lines).lower()
            if "javafx" in joined:
                print("  WARN: JavaFX referenced in stdout — headless mode may not be clean.")
            if re.search(r"unknown card|unable to find card", joined):
                print("  WARN: unknown card warnings — recent set coverage gap likely.")
    except Exception:
        pass


def _save_findings(output_dir: Path, findings: Findings) -> None:
    out = output_dir / "findings.json"
    out.write_text(json.dumps(asdict(findings), indent=2), encoding="utf-8")
    print(f"\nFindings written to: {out}")


if __name__ == "__main__":
    raise SystemExit(main())

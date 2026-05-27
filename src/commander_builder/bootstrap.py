"""FP-010 slice 1 — first-run dependency detection + Forge downloader.

A packaged EXE ships only the Python app; the heavy runtime data lives on
disk and may be absent on first launch:

  - **Forge** (`vendor/forge/forge-gui-desktop-*-jar-with-dependencies.jar`)
    — the sim engine. Auto-downloadable from the Card-Forge GitHub releases.
  - **JRE** (`vendor/jre/bin/java[.exe]`) — needed to run the Forge JAR.
    Platform-specific; reported but not auto-installed in this slice.
  - **mtg_cards/** — Scryfall oracle/image cache. Primes itself on demand
    via `scryfall_client`; reported but not bulk-downloaded here.

This module is import-safe with no heavy deps. ``check_dependencies()`` is
pure detection (fully testable); ``download_forge()`` streams the latest
release JAR (HTTP injectable for tests). Wired into the desktop launcher so
a first-run user gets a clear "what's missing + how to get it" message
instead of silent per-request Forge errors.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

FORGE_RELEASES_API = "https://api.github.com/repos/Card-Forge/forge/releases/latest"
TEMURIN_RELEASES_API = (
    "https://api.github.com/repos/adoptium/temurin17-binaries/releases/latest"
)
_USER_AGENT = "commander-builder/0.2 (+https://github.com/LlamaAdam/commander-builder)"


@dataclass
class DependencyStatus:
    """Presence/absence of each external runtime dependency."""
    forge_jar: Optional[Path] = None      # the located fat jar, or None
    jre: Optional[Path] = None            # java[.exe], or None
    cards_dir: Optional[Path] = None      # mtg_cards dir if it exists, else None
    notes: list[str] = field(default_factory=list)

    @property
    def forge_present(self) -> bool:
        return self.forge_jar is not None

    @property
    def jre_present(self) -> bool:
        return self.jre is not None

    @property
    def cards_present(self) -> bool:
        return self.cards_dir is not None

    @property
    def all_present(self) -> bool:
        return self.forge_present and self.jre_present and self.cards_present

    @property
    def missing(self) -> list[str]:
        out = []
        if not self.forge_present:
            out.append("forge")
        if not self.jre_present:
            out.append("jre")
        if not self.cards_present:
            out.append("mtg_cards")
        return out

    def to_dict(self) -> dict:
        return {
            "forge_jar": str(self.forge_jar) if self.forge_jar else None,
            "jre": str(self.jre) if self.jre else None,
            "cards_dir": str(self.cards_dir) if self.cards_dir else None,
            "missing": self.missing,
            "all_present": self.all_present,
            "notes": list(self.notes),
        }


def _find_forge_jar(forge_dir: Path) -> Optional[Path]:
    """Highest-version fat jar in ``forge_dir`` (version-aware, mirrors the
    fix in forge_runner — not a lexicographic first-match)."""
    import re
    jars = [
        j for j in forge_dir.glob("forge-gui-desktop-*.jar")
        if "jar-with-dependencies" in j.name
    ]
    if not jars:
        return None

    def _ver(p: Path) -> tuple:
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", p.name)
        return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)

    return max(jars, key=_ver)


def _find_jre(jre_dir: Path) -> Optional[Path]:
    for name in ("java.exe", "java"):
        cand = jre_dir / "bin" / name
        if cand.exists():
            return cand
    # System java is acceptable too (forge_runner falls back to PATH).
    import shutil
    sys_java = shutil.which("java")
    return Path(sys_java) if sys_java else None


def check_dependencies(
    forge_dir: Optional[Path] = None,
    jre_dir: Optional[Path] = None,
    cards_dir: Optional[Path] = None,
) -> DependencyStatus:
    """Detect Forge / JRE / mtg_cards. Defaults mirror the dev layout
    (``forge_runner.VENDOR_*`` + ``scryfall_client``'s cards dir)."""
    from .forge_runner import VENDOR_FORGE, VENDOR_JRE
    forge_dir = forge_dir or VENDOR_FORGE
    jre_dir = jre_dir or VENDOR_JRE
    if cards_dir is None:
        try:
            from .scryfall_client import _resolve_cards_dir
            cards_dir = _resolve_cards_dir()
        except Exception:  # noqa: BLE001
            cards_dir = None

    status = DependencyStatus(
        forge_jar=_find_forge_jar(forge_dir) if forge_dir.exists() else None,
        jre=_find_jre(jre_dir),
        cards_dir=cards_dir if (cards_dir and Path(cards_dir).exists()) else None,
    )
    if not status.forge_present:
        status.notes.append(
            "Forge jar missing — run `commander-builder-bootstrap "
            "--download-forge` or grab it from "
            "github.com/Card-Forge/forge/releases.")
    if not status.jre_present:
        status.notes.append(
            "Java not found — install a JRE 17+ (or drop one under "
            "vendor/jre/). Forge needs Java to run.")
    if not status.cards_present:
        status.notes.append(
            "mtg_cards cache absent — it primes itself from Scryfall on "
            "demand; audits/images will be slower until it warms.")
    return status


def _pick_forge_jar_asset(release: dict) -> Optional[dict]:
    """From a GitHub `releases/latest` payload, pick the desktop fat-jar
    asset. Returns the asset dict (has ``name`` + ``browser_download_url``)
    or None when the release has no matching asset."""
    assets = release.get("assets") or []
    candidates = [
        a for a in assets
        if isinstance(a, dict)
        and "forge-gui-desktop-" in (a.get("name") or "")
        and "jar-with-dependencies" in (a.get("name") or "")
        and (a.get("name") or "").endswith(".jar")
    ]
    if not candidates:
        return None
    # Prefer the newest by version in the filename (defensive — a release
    # normally ships one).
    import re

    def _ver(a: dict) -> tuple:
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", a.get("name", ""))
        return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)

    return max(candidates, key=_ver)


def _pick_jre_asset(release: dict, system: str, machine: str) -> Optional[dict]:
    """From an Adoptium/Temurin GitHub release payload, pick the JRE archive
    asset matching the caller's platform.

    Mirrors ``_pick_forge_jar_asset`` but keys off the OS + arch tokens that
    Temurin embeds in its asset names (e.g.
    ``OpenJDK17U-jre_x64_windows_hotspot_17.0.11_9.zip``):

      ``system``  is ``platform.system()``:
        "Windows" -> "windows", "Linux" -> "linux", "Darwin" -> "mac".
      ``machine`` is ``platform.machine()``:
        "AMD64"/"x86_64" -> "x64", "arm64"/"aarch64" -> "aarch64".

    Returns the matching asset dict (has ``name`` + ``browser_download_url``)
    or None when no asset matches.
    """
    os_token = {
        "windows": "windows",
        "linux": "linux",
        "darwin": "mac",
    }.get((system or "").lower())
    arch_token = {
        "amd64": "x64",
        "x86_64": "x64",
        "x64": "x64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get((machine or "").lower())
    if os_token is None or arch_token is None:
        return None

    assets = release.get("assets") or []
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "").lower()
        if not name:
            continue
        if "jre" not in name:
            continue
        if os_token in name and arch_token in name:
            return a
    return None


def download_forge(
    forge_dir: Optional[Path] = None,
    *,
    _get_release: Optional[Callable[[], dict]] = None,
    _download: Optional[Callable[[str, Path], None]] = None,
) -> Path:
    """Download the latest Forge desktop fat-jar into ``forge_dir``.

    Returns the written jar path. ``_get_release`` / ``_download`` are
    injectable for tests; defaults hit the GitHub API + stream the asset.
    Raises RuntimeError when no suitable asset is found.
    """
    from .forge_runner import VENDOR_FORGE
    forge_dir = forge_dir or VENDOR_FORGE
    get_release = _get_release or _fetch_latest_release
    download = _download or _stream_to_file

    release = get_release()
    asset = _pick_forge_jar_asset(release)
    if asset is None:
        raise RuntimeError(
            "no forge-gui-desktop fat jar found in the latest GitHub release")
    forge_dir.mkdir(parents=True, exist_ok=True)
    dest = forge_dir / asset["name"]
    download(asset["browser_download_url"], dest)
    return dest


def download_jre(
    jre_dir: Optional[Path] = None,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    _get_release: Optional[Callable[[], dict]] = None,
    _download: Optional[Callable[[str, Path], None]] = None,
) -> Path:
    """Download a Temurin JRE 17 archive for the current platform into
    ``jre_dir`` and return the archive path.

    ``system`` / ``machine`` default to ``platform.system()`` /
    ``platform.machine()``. ``_get_release`` / ``_download`` are injectable
    for tests. Raises ``RuntimeError`` when no suitable asset is found.

    Note: this downloads only the *archive* (zip / tar.gz). Extraction is
    the caller's responsibility — a first-run UI can do it with
    ``zipfile`` or ``tarfile`` after the download completes.
    """
    import platform as _platform
    from .forge_runner import VENDOR_JRE
    jre_dir = jre_dir or VENDOR_JRE
    system = system or _platform.system()
    machine = machine or _platform.machine()

    get_release = _get_release or _fetch_temurin_release
    download = _download or _stream_to_file

    release = get_release()
    asset = _pick_jre_asset(release, system, machine)
    if asset is None:
        raise RuntimeError(
            f"no Temurin JRE asset found for system={system!r}, "
            f"machine={machine!r} in the latest release"
        )
    jre_dir.mkdir(parents=True, exist_ok=True)
    dest = jre_dir / asset["name"]
    download(asset["browser_download_url"], dest)
    return dest


def extract_jre(archive: Path, jre_dir: Optional[Path] = None) -> Path:
    """Extract a downloaded Temurin JRE archive (.zip / .tar.gz) into
    ``jre_dir`` so that ``jre_dir/bin/java[.exe]`` exists.

    Temurin archives nest everything under a single top-level directory
    (e.g. ``jdk-17.0.11+9-jre/``); its contents are flattened into
    ``jre_dir``. Returns ``jre_dir``. Raises ``RuntimeError`` on an
    unsupported archive type or if extraction yields no ``bin/java``.
    """
    import shutil
    import tarfile
    import tempfile
    import zipfile
    from .forge_runner import VENDOR_JRE

    jre_dir = jre_dir or VENDOR_JRE
    name = archive.name.lower()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmp_path)
        elif name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive, "r:gz") as tf:
                # filter='data' (py3.12+) blocks path-traversal members.
                try:
                    tf.extractall(tmp_path, filter="data")
                except TypeError:  # pragma: no cover - older pythons
                    tf.extractall(tmp_path)
        else:
            raise RuntimeError(f"unsupported JRE archive type: {archive.name}")

        # Flatten the single nested top-level dir, if present.
        entries = list(tmp_path.iterdir())
        src = entries[0] if (len(entries) == 1 and entries[0].is_dir()) else tmp_path

        jre_dir.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            target = jre_dir / child.name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            shutil.move(str(child), str(target))

    bindir = jre_dir / "bin"
    if not ((bindir / "java.exe").exists() or (bindir / "java").exists()):
        raise RuntimeError(
            f"JRE extraction did not produce bin/java under {jre_dir}"
        )
    return jre_dir


def ensure_jre(
    jre_dir: Optional[Path] = None,
    *,
    system: Optional[str] = None,
    machine: Optional[str] = None,
    _get_release: Optional[Callable[[], dict]] = None,
    _download: Optional[Callable[[str, Path], None]] = None,
) -> Path:
    """First-run convenience: download AND extract a Temurin JRE so
    ``jre_dir/bin/java[.exe]`` exists, unless it already does. Returns
    ``jre_dir``. Test seams mirror ``download_jre``."""
    from .forge_runner import VENDOR_JRE
    jre_dir = jre_dir or VENDOR_JRE
    bindir = jre_dir / "bin"
    if (bindir / "java.exe").exists() or (bindir / "java").exists():
        return jre_dir
    archive = download_jre(
        jre_dir, system=system, machine=machine,
        _get_release=_get_release, _download=_download,
    )
    return extract_jre(archive, jre_dir)


def prime_card_cache(
    names: Optional[list[str]] = None,
    *,
    _lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """Prime the Scryfall card-snapshot cache for a list of card names.

    ``names`` defaults to a small representative set (dual lands + 5 staples)
    chosen for quick first-run warm-up. Each card is fetched once via
    ``scryfall_client.lookup_card`` (cache-first, writes the snapshot on
    miss). Returns ``{"primed": [<name>, ...], "errors": [<name>, ...]}``.

    ``_lookup`` is injectable for tests; defaults to
    ``scryfall_client.lookup_card``.
    """
    _DEFAULT_PRIME_NAMES = [
        "Sol Ring", "Command Tower", "Arcane Signet",
        "Path to Exile", "Swords to Plowshares",
        "Cultivate", "Kodama's Reach",
        "Cyclonic Rift", "Demonic Tutor",
        "Rhystic Study",
    ]
    targets = names if names is not None else _DEFAULT_PRIME_NAMES

    if _lookup is None:
        from .scryfall_client import lookup_card as _lookup_fn
        _lookup = _lookup_fn

    primed: list[str] = []
    errors: list[str] = []
    for name in targets:
        try:
            result = _lookup(name)
            if result is not None:
                primed.append(name)
            else:
                errors.append(name)
        except Exception:  # noqa: BLE001
            errors.append(name)
    return {"primed": primed, "errors": errors}


def _fetch_temurin_release() -> dict:
    req = urllib.request.Request(
        TEMURIN_RELEASES_API,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _fetch_latest_release() -> dict:
    req = urllib.request.Request(
        FORGE_RELEASES_API,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _stream_to_file(url: str, dest: Path) -> None:
    """Stream ``url`` to ``dest`` (atomic via .part rename). The Forge jar
    is ~120 MB so we copy in chunks rather than read() it all into memory."""
    import shutil
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh, length=1024 * 256)
    tmp.replace(dest)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="commander-builder-bootstrap",
        description="Check / fetch the desktop app's external dependencies "
                    "(Forge, JRE, mtg_cards).",
    )
    ap.add_argument("--check", action="store_true",
                    help="report dependency status and exit (default if no "
                         "other action given).")
    ap.add_argument("--download-forge", action="store_true",
                    help="download the latest Forge desktop jar.")
    ap.add_argument("--download-jre", action="store_true",
                    help="download a Temurin JRE 17 archive for the current "
                         "platform into vendor/jre/.")
    ap.add_argument("--prime-cards", action="store_true",
                    help="warm the Scryfall card-snapshot cache with a small "
                         "representative set of staple cards.")
    ap.add_argument("--prime-cards-list", nargs="+", metavar="NAME",
                    default=None,
                    help="prime the cache for specific card names (overrides "
                         "the built-in list; requires --prime-cards).")
    args = ap.parse_args(argv)

    rc = 0

    if args.download_forge:
        print("Downloading the latest Forge desktop jar...")
        try:
            jar = download_forge()
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {type(exc).__name__}: {exc}")
            rc = 1
        else:
            print(f"  saved: {jar}")

    if args.download_jre:
        print("Downloading Temurin JRE 17 archive...")
        try:
            arch = download_jre()
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {type(exc).__name__}: {exc}")
            rc = 1
        else:
            print(f"  saved: {arch}")
            print("  extracting...")
            try:
                jdir = extract_jre(arch)
            except Exception as exc:  # noqa: BLE001
                print(f"  extract failed: {type(exc).__name__}: {exc}")
                rc = 1
            else:
                print(f"  installed JRE -> {jdir}")

    if args.prime_cards:
        names = args.prime_cards_list  # None -> built-in default list
        print("Priming Scryfall card-snapshot cache...")
        result = prime_card_cache(names=names)
        print(f"  primed: {len(result['primed'])} cards")
        if result["errors"]:
            print(f"  errors: {result['errors']}")

    status = check_dependencies()
    print("Dependency status:")
    print(f"  Forge jar : {status.forge_jar or 'MISSING'}")
    print(f"  Java/JRE  : {status.jre or 'MISSING'}")
    print(f"  mtg_cards : {status.cards_dir or 'absent (primes on demand)'}")
    for note in status.notes:
        print(f"  - {note}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

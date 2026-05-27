# PyInstaller spec — Commander Builder desktop app (FP-010).
#
# Build:  python scripts/build_desktop.py
#    or:  pyinstaller --noconfirm packaging/commander-builder.spec
#
# One-FOLDER build (COLLECT) rather than one-file: pywebview's native
# backend (EdgeChromium via pythonnet on Windows) unpacks more reliably
# from a folder than a self-extracting one-file stub. Output lands in
# dist/CommanderBuilder/CommanderBuilder.exe.
#
# Heavy runtime data (Forge JAR, JRE, mtg_cards/) is intentionally NOT
# bundled — see docs/fp010-plan.md. Only the Python app + Flask
# templates/static ship in the EXE.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(os.getcwd())
PKG = ROOT / "src" / "commander_builder"

# Flask resolves templates/ + static/ relative to the package dir. Bundle
# them under the same package-relative path so create_app() finds them
# inside PyInstaller's _MEIPASS extraction dir.
datas = [
    (str(PKG / "web" / "templates"), "commander_builder/web/templates"),
    (str(PKG / "web" / "static"), "commander_builder/web/static"),
]

# Blueprints are imported dynamically by create_app; pywebview's platform
# backend is imported lazily — declare both so the freezer keeps them.
hiddenimports = (
    collect_submodules("commander_builder.web")
    + collect_submodules("webview")
    + ["flask", "jinja2"]
)

block_cipher = None

a = Analysis(
    [str(ROOT / "packaging" / "desktop_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CommanderBuilder",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # GUI app — suppress the console window
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="CommanderBuilder",
)

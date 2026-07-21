"""PyInstaller entry point for the Commander Builder desktop EXE (FP-010).

Kept as a tiny shim (not the package's ``__main__``) so PyInstaller has a
concrete top-level script to analyze. All logic lives in
``commander_builder.desktop``.
"""
from commander_builder.desktop import main

raise SystemExit(main())

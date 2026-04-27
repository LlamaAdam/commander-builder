"""Pytest config: ensure `src/` is on sys.path so `commander_builder.*` imports
without an editable install."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

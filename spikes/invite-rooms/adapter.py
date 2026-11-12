"""Compatibility import: product adapter is the single implementation."""
from pathlib import Path
import sys

_PRODUCT = Path(__file__).resolve().parents[2] / "server"
if str(_PRODUCT) not in sys.path:
    sys.path.insert(0, str(_PRODUCT))
from invite_rooms.control.adapter import LoopbackAdapter  # noqa: E402

__all__ = ["LoopbackAdapter"]

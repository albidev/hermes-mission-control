"""Compatibility import: product owns fixed link state."""
from pathlib import Path
import sys

_PRODUCT = Path(__file__).resolve().parents[2] / "server"
if str(_PRODUCT) not in sys.path:
    sys.path.insert(0, str(_PRODUCT))
from invite_rooms.control.state import FixedLinkStore, LinkStateError  # noqa: E402

__all__ = ["FixedLinkStore", "LinkStateError"]

"""Lab adapter for the product consent contract."""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[2] / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from invite_rooms.consent import canonical  # noqa: E402
from invite_rooms.consent import verify as _verify  # noqa: E402
from lobby_control import ControlError  # noqa: E402


def verify(public_key, receipt, signature, expected):
    try:
        _verify(public_key, receipt, signature, expected)
    except Exception as exc:
        if isinstance(exc, ControlError):
            raise
        raise ControlError("invalid_consent") from None

"""Compatibility surface for the spike; implementation lives in the product package."""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[2] / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from invite_rooms.control.framing import (  # noqa: E402
    MAX_FRAME_BYTES, FrameError, TLSIdentityError, TLSStreamClosed,
    SequencedTLS, certificate_fingerprint, hmac_compare,
)
from invite_rooms.control.tls import client_context, server_context, wrap_client, wrap_server  # noqa: E402

__all__ = [
    "MAX_FRAME_BYTES", "TLSIdentityError", "TLSStreamClosed", "SequencedTLS",
    "certificate_fingerprint", "hmac_compare", "client_context", "server_context",
    "wrap_client", "wrap_server", "FrameError",
]

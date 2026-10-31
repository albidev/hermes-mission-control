"""Compatibility import: the product owns the room firewall policy."""
from pathlib import Path
import sys

_PRODUCT = Path(__file__).resolve().parents[2] / "server"
if str(_PRODUCT) not in sys.path:
    sys.path.insert(0, str(_PRODUCT))
from invite_rooms.control.firewall import (  # noqa: E402
    FirewallDenied, ForwardedRequest, Request, RoomFirewall, UpstreamAmbiguous,
    UpstreamResponse, _RUN_ID, _validated_scope,
)

__all__ = ["FirewallDenied", "ForwardedRequest", "Request", "RoomFirewall",
           "UpstreamAmbiguous", "UpstreamResponse", "_RUN_ID", "_validated_scope"]

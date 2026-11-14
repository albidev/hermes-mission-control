"""Lab compatibility shim — re-exports the canonical implementation.

The accepted bounded protocol now lives in server/invite_rooms/native_rpc.py.
This module preserves existing spike import names so all lab tests exercise
the SAME implementation.  No independent second copy.
"""
from invite_rooms.native_rpc import (  # noqa: F401
    BoundedNativeRpc,
    NativeRpcAmbiguous,
    NativeRpcDenied,
    NativeRpcRejected,
    MAX_REQUEST,
    MAX_RESPONSE,
    _FIELDS,
    _unique,
)
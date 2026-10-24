"""Spike CLI compatibility; certificate generation lives in the product."""
import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[2] / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
from invite_rooms.control.pki import (
    init_owner, issue_server, fingerprint, main, _openssl,
)

if __name__ == "__main__":
    raise SystemExit(main())

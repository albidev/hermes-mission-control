"""Spike CLI compatibility; certificate generation lives in the test lab module.

The PKI helper is test-only tooling (it mints throwaway lab certificates), so it
sits under server/tests/lab_pki.py rather than in the product package.
"""
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _path in (_REPO, _REPO / "server"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from server.tests.lab_pki import (
    init_owner, issue_server, fingerprint, main, _openssl,
)

if __name__ == "__main__":
    raise SystemExit(main())

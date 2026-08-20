#!/usr/bin/env python3
"""Promote approved candidates whose quarantine has elapsed into the vault.

Reads ~/.hermes/vault-brain/candidates/, finds candidates with status=approved
whose quarantine_until <= now, and writes them to the vault wiki/concepts.
Marks them status=promoted. Safe to run as a cron (idempotent).

Usage:
  python3 promote_candidates.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidates as candidates_mod  # noqa: E402


def main() -> int:
    promoted = candidates_mod.promote_ready()
    if promoted:
        print(f"Promoted {len(promoted)} candidate(s) to wiki/concepts/:")
        for c in promoted:
            print(f"  - {c.get('title', c.get('id'))}")
    # else: silent — nothing to promote, nothing to report (watchdog pattern)
    return 0


if __name__ == "__main__":
    sys.exit(main())

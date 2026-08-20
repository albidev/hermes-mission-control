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
        # Push each affected vault_dir if it is a git repo with a remote.
        # Only Crossnection (private albidev/crossnection-vault) is a git repo.
        for vault_dir in candidates_mod.vault_dirs_with_promotions():
            _git_commit_and_push(vault_dir, promoted)
    # else: silent — nothing to promote, nothing to report (watchdog pattern)
    return 0


def _git_commit_and_push(vault_dir: Path, promoted: list) -> None:
    """Commit and push vault_dir to its git remote if present and dirty."""
    import subprocess
    git_dir = vault_dir / ".git"
    if not git_dir.exists():
        print(f"  [skip] {vault_dir}: not a git repo")
        return
    try:
        subprocess.run(["git", "-C", str(vault_dir), "add", "-A"],
                       check=True, capture_output=True, timeout=60)
        subprocess.run(["git", "-C", str(vault_dir), "commit", "-m",
                        f"nightly-brain: promote {len(promoted)} candidate(s)"],
                       check=True, capture_output=True, timeout=60)
    except subprocess.CalledProcessError:
        # nothing to commit (no changes) — fine, idempotent
        return
    try:
        subprocess.run(["git", "-C", str(vault_dir), "push", "origin", "main"],
                       check=True, capture_output=True, timeout=120)
        print(f"  [push] {vault_dir} -> origin/main")
    except subprocess.CalledProcessError as exc:
        print(f"  [push FAILED] {vault_dir}: {exc.stderr.decode().strip()}")


if __name__ == "__main__":
    sys.exit(main())

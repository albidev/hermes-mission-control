#!/usr/bin/env bash
# Verify that every path referenced by the operational documentation and
# package scripts exists in this repository tree.
#
# This repository IS the Mission Control application (no apps/mission-control
# monorepo layout). A user cloning it must be able to follow the README,
# CONTRIBUTING, and runbooks from the repository root. This check fails CI
# when a documented operational path drifts out of the tree or when the old
# monorepo layout leaks back into the operational docs.
#
# Usage:
#   bash scripts/check-documented-paths.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Paths referenced by README quick start / testing, CONTRIBUTING, runbooks,
# and package.json scripts. Relative to the repository root.
required_paths=(
  ".env.example"
  "package.json"
  "vite.config.ts"
  "server/requirements.txt"
  "scripts/run-local-telemetry.sh"
  "scripts/smoke-upgrade.sh"
  "scripts/smoke-test-telemetry.sh"
  "scripts/reapply-core-mission-control-fixes.sh"
  "patches/hermes-core-mission-control-api_server.patch"
  "docs/runbooks/upgrade-compatibility.md"
  "docs/contracts/compatibility-matrix.md"
  "docs/contracts/mission-control-capabilities-v1.json"
  "docs/contracts/mission-control-trace-v1.json"
)

# Operational docs that must never reference the legacy monorepo layout.
operational_docs=(
  "README.md"
  "CONTRIBUTING.md"
  "docs/runbooks/upgrade-compatibility.md"
)

fail=0

echo "[check-documented-paths] repo root: $REPO_ROOT"
echo "[check-documented-paths] verifying $((${#required_paths[@]})) documented paths..."

for rel in "${required_paths[@]}"; do
  if [[ -e "$REPO_ROOT/$rel" ]]; then
    echo "  [OK] $rel"
  else
    echo "  [MISSING] $rel"
    fail=1
  fi
done

echo "[check-documented-paths] verifying no legacy 'apps/mission-control' references in operational docs..."
for doc in "${operational_docs[@]}"; do
  if grep -q "apps/mission-control" "$REPO_ROOT/$doc"; then
    echo "  [LEGACY] $doc still references apps/mission-control"
    fail=1
  else
    echo "  [OK] $doc"
  fi
done

if [[ "$fail" -ne 0 ]]; then
  echo "[check-documented-paths][FAIL] documented operational paths are out of sync with the repository tree." >&2
  exit 1
fi

echo "[check-documented-paths] all documented operational paths exist."

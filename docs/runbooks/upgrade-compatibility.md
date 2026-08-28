# Mission Control upgrade compatibility runbook

## Goal
Keep Mission Control usable across Hermes updates without touching core internals every release.

## Repos and boundaries
- `hermes-agent` (main upstream repo): must stay clean before/after update.
- `hermes-mission-control` (this repo): contains the UI, the telemetry sidecar, contracts, and smoke scripts.
- Rule: do not couple Mission Control to unstable internal payloads without a fallback path.

## Pre-upgrade checklist
1. Ensure `hermes-agent` working tree is clean
   - `git status --short`
2. Ensure Mission Control branch is clean or committed
   - `git status --short`
3. Run frontend build
   - `pnpm build`
4. Run smoke script
   - `bash scripts/smoke-upgrade.sh`

## Update flow (safe)
1. Update `hermes-agent` to target version/commit.
2. Restart services (gateway + mission-control) if needed.
3. Run smoke script again.
4. Open `/agents` and verify:
   - Live toggle works
   - Timeline renders
   - DAG renders
5. Open `/agents/:agentId` and verify single-agent filter and KPIs.

## Expected compatibility behavior
- If `/mission-control/capabilities` is missing (404), frontend uses built-in v1 defaults.
- If SSE fails, frontend falls back to polling automatically.
- If trace payload is wrapped (`trace`, `data`, `payload`), frontend unwraps and normalizes it.
- If `compact=1` is unsupported, frontend can run without compact mode.

## Fast failure diagnosis
- Blank trace cards: payload contract mismatch.
- Live mode no updates: SSE unavailable, check polling fallback and gateway logs.
- 401 lock screen: token missing/invalid.

## Rollback levers
1. Keep backend version, rely on polling fallback (no immediate rollback required).
2. Disable live expectations operationally (use Post mode).
3. If backend breaks contract badly, pin to known-good Hermes commit and rerun smoke.

## Stash/conflict recovery in hermes-agent
Use this when update/autostash leaves conflicted files:
1. `git reset --hard HEAD`
2. `git clean -fd .plans docs/plans tests/gateway website/docs/guides`
3. Save any applied dirty state safely
   - `git stash push -u -m "rescue-<label>"`
4. Keep only one canonical stash
   - `git stash list`
   - `git stash drop <duplicate>`

## Required artifacts in this repo
- `docs/contracts/mission-control-capabilities-v1.json`
- `docs/contracts/mission-control-trace-v1.json`
- `docs/contracts/compatibility-matrix.md`
- `patches/hermes-core-mission-control-api_server.patch`
- `scripts/reapply-core-mission-control-fixes.sh`
- `scripts/smoke-upgrade.sh`

## Canonical backend recovery path
If a Hermes core update drops Mission Control routes from `gateway/platforms/api_server.py`:
1. Run `bash scripts/reapply-core-mission-control-fixes.sh` (optionally pass the
   path to the `hermes-agent` checkout as the first argument; it defaults to
   `$HOME/.hermes/hermes-agent`).
2. The script first reapplies `patches/hermes-core-mission-control-api_server.patch` with `git apply`
3. Then it reapplies compatibility shims in `model_tools.py` and `tools/skills_tool.py`
4. Finally it runs syntax checks, restarts gateway, and smoke-checks Mission Control endpoints

Rule: update the canonical patch file whenever Mission Control backend compatibility changes, instead of relying on git stash recovery.

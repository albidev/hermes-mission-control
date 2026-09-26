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
5. Open `/sessions`, select a session, and use `Trace` to open `/agents?session=<sessionId>`.
   - Agents is the session trace cockpit; historical source/model filtering stays in Sessions.

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
- `patches/hermes-core-mission-control-api_server.patch` (historical, see note below)
- `scripts/reapply-core-mission-control-fixes.sh`
- `scripts/smoke-upgrade.sh`

> **Note on the `.patch` file:** `patches/hermes-core-mission-control-api_server.patch`
> targets the pre-sidecar architecture, when Mission Control routes lived directly on
> `gateway/platforms/api_server.py` (`_build_mission_control_snapshot`,
> `_handle_mission_control`, etc.). That code path no longer exists in current
> `hermes-agent` and the reapply script does **not** apply this patch — it is kept
> only as a historical reference. `check-documented-paths.sh` still requires the
> file to exist (documented path check), so don't delete it without also updating
> that script and this note.

## Canonical backend recovery path
Mission Control's actual backend dependency on Hermes core today is `hermes_cli/web_server.py`
(dashboard auth/token acceptance, `allowed_roots` for local file access, and the Knowledge
core-docs candidate list). If a Hermes core update breaks any of those:
1. Run `bash scripts/reapply-core-mission-control-fixes.sh` (optionally pass the
   path to the `hermes-agent` checkout as the first argument; it defaults to
   `$HOME/.hermes/hermes-agent`).
2. The script patches `hermes_cli/web_server.py` in place: multi-token bearer auth
   (`MISSION_CONTROL_TOKEN` / `API_SERVER_KEY` alongside the ephemeral session token),
   the `allowed_roots` local-file allowlist, and the Knowledge core-docs candidate paths
   (`SOUL.md`, `USER.md`, `AGENTS.md`, `memories/MEMORY.md`). Each block is idempotent —
   already-aligned files are left untouched.
3. It then verifies the Vite proxy points at the dashboard backend (`127.0.0.1:9119`,
   `/api/local` route) — this is a hard check, not a patch.
4. Finally it runs syntax checks, restarts `ai.hermes.dashboard-api`,
   `ai.hermes.mission-control-telemetry`, and `ai.hermes.mission-control`, and
   smoke-checks Mission Control endpoints on both 9119 and 5174.

Rule: update the reapply script's patch blocks whenever Mission Control's dependency
on `hermes_cli/web_server.py` changes, instead of relying on git stash recovery.

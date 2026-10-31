# Invite-only Rooms in Mission Control — MVP scope and integration strategy

**Status:** implementation in progress; not release-ready or deployed. The candidate tree now contains host/joining/pairing route handlers and browser panels, and several bounded implementation slices have scoped acceptance. Overall A2b2a acceptance remains open: durable-method coverage, complete store/I/O integration, two-installation E2E, and final independent review are still required.
**Runtime boundary:** the owner-only route dispatcher is mounted in the telemetry sidecar but remains disabled unless valid owner-private configuration is supplied. The control-listener lifecycle and complete two-owner flow are not yet accepted. This plan is a technical scope document, not a user guide.
**Baseline:** `dc028f5` on `feat/invite-rooms` is the historical design baseline. Incremental PR updates have been owner-authorized; this document does not authorize a merge, live restart, production rollout, or deployment.
**Goal:** allow two owners of an already paired Hermes installation to explicitly approve one fixed-roster, text-only Room, then use the existing canonical Rooms UI.

## Recommended architecture

Keep native `groups.*` as the Room authority. Mission Control owns only invitation/consent state and owner-facing orchestration; its sidecar must not write native Room/session tables. Reuse the accepted invite store, reconciliation and pinned control contracts rather than implementing a second state machine in React.

```text
Owner browser -> authenticated MC sidecar -> local owner operation service
                                              |-- owner-authenticated groups.* RPC
                                              |-- private peer-material registry
                                              `-- pinned control channel through WSS + inner mTLS
Other owner -> their own authenticated local owner operation service

After activation: existing GroupGatewayClient / use-group-room / GroupRoomView
                  -> canonical groups.state / groups.log / groups.send
Relay: authenticated opaque frames only, no admin/native API exposure
```

The current VPS B has a headless gateway, not a deployed Mission Control/owner-control service. Browser-to-browser acceptance cannot be inferred from F1. First support a small headless owner command adapter using the same owner operation service; deploying a second MC or a persistent owner-control service is a separate explicit rollout step. Do not expose B's `127.0.0.1:18791` to make its owner UI easier.

## In / out

### In the first product slice

- One host owner and one joining installation/profile, paired beforehand with approved installation identity, profile, certificate fingerprints and consent verification key.
- Fixed roster, explicit Host approval and separate durable Joining-owner consent.
- Create/copy an invitation, import it through the joining owner's private surface, preview sanitized scope/policy, show status and expiry/revocation, activate only when both consents are present.
- Hand off the confirmed native `room_id` to the existing Rooms view. Sending remains an explicit existing Room action; accepting an invitation never sends a prompt.
- Owner-only orphan inventory, quarantine and explicit reconciliation; no background cleanup of ambiguous native effects.
- Resume after refresh/restart with the same IDs. A transport ambiguity stays visible and blocked; never mint fresh IDs or infer absence from an unavailable native DB.

### Out

- Public peer discovery, auto-trust, automatic relay provisioning or arbitrary peer/base URLs supplied by a browser.
- Tool-capable remote execution, attachments, dynamic membership, host migration or multi-host authority.
- Public native/admin endpoints, generic SSH tunnels, VPS-to-Mac connections, secrets in localStorage or UI state snapshots.
- Production imports from `spikes/`, core changes, live MC restart, automatic merge/push/deploy.
- Unrelated upstream PRs and previously denied deletion targets.

## Current candidate snapshot

- The candidate tree contains the product-owned invite, pairing, joining, guest, and target-HTTP modules, plus owner-only HTTP dispatch and browser panels. The sidecar mounts the route dispatcher in `server/local_telemetry_server.py`.
- The feature remains off by default and returns 404 when its owner-private runtime configuration is absent or invalid. Pairing and joining have their own configuration/store requirements.
- Control-listener/connector primitives exist, but the sidecar-owned lifecycle and complete two-installation journey are not yet accepted. The default material resolver still fails closed; activation is not a verified end-to-end path.
- Remaining feature gates: the residual durable-method proof bundle, complete store/I/O integration, same-Mac two-instance E2E with recovery, overall independent review/acceptance, and owner-authorized rollout. Cross-host/VPS transport is a later gate, not an MVP claim.

## Design-time source inventory

The table below records the original proposal baseline at `dc028f5`; it explains why the architecture was chosen and is not a current implementation-status table. For the present API surface, see [api.md](../api.md#invite-only-rooms).

| Existing source | Reuse / missing boundary |
| --- | --- |
| `server/room_invite_store.py` | Durable invite/roster state; currently only imported by the isolated service/tests. Add public-safe projections without leaking invite secrets. |
| `server/room_lobby_service.py` | Native create/register coordinator; requires owner-authenticated RPC and a private material resolver. It is not a public request handler. |
| `spikes/invite-rooms/lobby_{control,owner,consent,material,activation,reconcile,native_rpc}.py` | Accepted lab contracts; promote only the needed behavior into a product-owned package, keeping a single implementation and rerunning lab parity tests. No live `sys.path` import of spike files. |
| `spikes/invite-rooms/lobby_owner.py::OwnerSurface` | Loopback lab surface deliberately rejects every Origin header. Do not proxy browser headers to it or remove that protection; use an internal operation-service boundary. |
| `spikes/invite-rooms/lobby_control.py::parse_invitation` | Lab-only `https://rooms.lab/...#secret` placeholder. Product needs a pinned, owner-configured invitation origin; no fetch to a URL supplied in an invitation. |
| `server/local_telemetry_server.py` | Existing `_is_authorized` and `/api/local/room/*` dispatch; add thin owner-only routes only after service tests pass. |
| `src/lib/group-gateway.ts`, `src/lib/use-group-room.ts` | Existing authenticated canonical Room client and transcript synchronization. Reuse unchanged unless the activation handoff requires a narrow extension. |
| `src/components/chat/GroupRoomView.tsx` | Existing room UI; add a separate lobby entry panel rather than duplicating the transcript. |

## Proposed product modules and route contract

New product package: `server/invite_rooms/` (operation service, peer registry, RPC adapter and redacted view models). Keep the existing store/coordinator as the starting point; moving them is an implementation decision, not permission to duplicate them. All secret material stays in owner-private files with 0700 parent/0600 files; the SQLite invitation store holds hashes, not native grants/admin tokens.

The routes in this proposal have since gained candidate handlers and sidecar dispatch. They are still feature-gated by owner-private configuration and are not a release-readiness claim. The exact current path set is documented in [`docs/api.md`](../api.md#invite-only-rooms); use it when comparing the frozen design contract below with the implementation.

| Route | Owner operation / restrictions |
| --- | --- |
| `POST /lobbies` | Create one local lobby from validated fixed roster and selected paired peer IDs; no arbitrary endpoint, fingerprint, grant or admin credential input. |
| `GET /lobbies/{id}` | Redacted local lobby, approvals, scope and policy state. Never raw grants, private paths or signature material. |
| `POST /lobbies/{id}/invites` | Issue an invitation. Return the secret only in this explicit copy/export response; `Cache-Control: no-store`, no request/response-body logging. |
| `POST /lobbies/{id}/invites/{invite}/revoke` | Revoke an unused invitation; show the difference between invite revocation, native grant revocation and stopping an admitted run. |
| `POST /imports/preview` and `POST /imports/redeem` | Joining-owner local operations. Parse import without navigation/fetch, resolve a preapproved peer, then use pinned control. GET preview never consumes an invitation. |
| `POST /lobbies/{id}/invites/{invite}/decision` | Host approve/reject, bound to the exact peer/profile/scope and policy digest shown to the owner. |
| `POST /imports/{id}/consent` | Joining-owner signed durable consent after Host approval; a browser cannot assert that it is the other owner. |
| `POST /lobbies/{id}/activate` | Owner-explicit mint/create/register; return a native room ID only after exact native readback. Ambiguous remains blocked, not a success toast. |
| `GET /orphans` + `POST /orphans/{scope}/quarantine` + `POST /orphans/{scope}/reconcile` | Local owner-only operations. Reconcile requires fresh confirmation and readable native evidence. Never cross a request-provided DB path. |

The headless joining-owner adapter invokes the same methods without reusing an MC bearer token from another installation. It must present the same scope/policy and require an explicit local owner decision. Invitation hashes, membership consent and transport authentication are distinct credentials; possession of one must not imply the others.

## Original serial implementation packets

Each packet was planned as RED → GREEN → independent review. The sequence remains useful for acceptance; do not infer current progress from this list. PR updates are allowed only when explicitly authorized by the owner; merge, live restart, and production rollout require separate approval.

### P1 — product operation boundary (original no-live-routes scope)

1. Add `server/tests/test_room_invite_operations.py` with behavior tests for redacted views, identity mismatch, immutable roster and separate approvals. Run the test first and retain the failure.
2. Implement `server/invite_rooms/service.py` and explicit owner context with injected RPC/private registry; no HTTP or global production paths.
3. Add registry tests for unknown peer, changed fingerprint/profile/policy and path/URL injection; implement `server/invite_rooms/peer_registry.py`.
4. Add exact-RPC-result and post-admission ambiguity tests; implement `server/invite_rooms/native_rpc.py` using the accepted bounded protocol, not a generic HTTP proxy.
5. Add parity tests covering the accepted signed-consent/reconciliation cases before moving any lab logic. Define a migration/import seam that avoids two divergent implementations.

Verify from the worktree:

```sh
PATH=/Users/albi/.hermes/hermes-agent/venv/bin:$PATH pnpm test:server
/Users/albi/.hermes/hermes-agent/venv/bin/python -B -m unittest discover -s spikes/invite-rooms/tests
```

Original P1 acceptance criterion: no imports into live telemetry at that stage, and no tools/grants/native actions from merely inspecting a lobby. The later candidate mounts the feature-gated dispatcher; see the current snapshot above.

### P2 — authenticated local API and headless joining-owner adapter (original packet)

1. Write real HTTP tests in `server/tests/test_room_invite_routes.py`: no auth, foreign Origin, invalid body/duplicate keys, redaction, IDOR/cross-profile denial and feature disabled.
2. Implement `server/invite_rooms/routes.py`; add a thin explicit dispatch in `server/local_telemetry_server.py`. Do not turn the sidecar into an arbitrary owner-operation forwarder.
3. Test the joining-owner command adapter against disposable local owner state. Do not transfer native admin tokens or consent signing keys.
4. Test that only the explicit issue/export operation returns the invite secret, and that credentials never enter logs, URLs sent to HTTP servers or cached projections.

Verify using the same Python server suite; include listener/auth tests that make actual requests, not source-string assertions. All runtimes remain isolated; no live restart.

### P3 — browser lobby UI and canonical Room handoff (original packet)

1. Add `src/lib/room-invite-api.ts` with redacted typed contracts and AbortController-owned requests; tests in `tests/room-invite-api.test.ts`.
2. Add `src/lib/use-room-invite.ts` for the lobby UI only, not the native state machine. Test reload/resume, stale response cancellation, unavailable vs empty and explicit ambiguity states.
3. Add `src/components/chat/RoomInvitePanel.tsx` and an entry in the existing Rooms picker. Expose paired identity/profile/fingerprint, policy and the two separate decisions; no secret prefill or auto-activation.
4. Hand off the confirmed native `room_id` into the existing room selection/pointer flow; preserve exact canonical event IDs/order, including background/foreground and desktop/mobile.

Verify:

```sh
pnpm typecheck
NODE_ENV=test pnpm test:frontend
pnpm build
```

Run isolated browser QA at desktop and mobile sizes: invite copy/import, pending host decision, pending joining consent, ready, activation ambiguity, reload and native Room handoff. An unimplemented joining-owner surface is a blocker, not an empty dropdown.

### P4 — release evidence, still no automatic rollout

1. Run one serial end-to-end product flow with actual owner-authenticated surfaces and an already paired Mac/VPS lab. The headless owner's explicit consent is part of the report; do not call that browser-to-browser proof.
2. Verify no native mint/create/register/send before the corresponding approvals, no secret leakage, unchanged policy, one native receipt per duplicate, exact transcript parity and recovery after an ambiguous operation.
3. Re-run native s2g/s2e/F1 only if touched transport/control behavior requires it; keep suites/gates serial, sources hashed and cleanup authorized/preserving evidence.
4. Independently review the complete delta, record limitations and obtain separate approval for any rollout, push or second-host owner-service deployment.

## Documentation deliverables before calling the feature ready

The API and design documents are technical references, not operating instructions. Two separate guides remain required **after** the two-installation E2E and overall acceptance stabilize the behavior:

- [ ] **User guide** (for the host and joining owner): prerequisites, explicit pairing and invitation steps, what each approval means, how to verify activation and open the canonical Room, recovery/ambiguity handling, and clear supported/unsupported network scope.
- [ ] **Agent/bot runbook** (for an authorized operator agent): an upfront authorization gate; ordered, executable steps with exact commands/config locations, expected results and stop conditions; explicit separation of agent-safe reads from owner-only SAS comparison, approval, consent, unpair and reconciliation; secret-handling rules; and fail-closed instructions for missing config, stale pairing or ambiguous native effects.

Do not write either as a claim of availability before the real E2E, recovery cases, and independent feature acceptance pass. Cross-host/VPS setup is not part of the MVP guide unless a later gate accepts it.

## Risks and release blockers

- **Trust bootstrap:** the lab is prepaired. The first MVP must label that prerequisite, not silently implement public discovery or trust-on-first-use from an invite.
- **Owner auth:** the browser MC token authorizes only its local owner; it cannot sign another owner's consent or select an arbitrary remote endpoint.
- **State drift:** config/policy changes invalidate the approved digest. F1X-O3 changed B's digest; old F1 catalogs cannot be reused.
- **Partial native effects:** target grants and host rooms may exist after lost replies. Keep durable ambiguity, quarantine and explicit reconciliation; an unavailable DB never proves absence.
- **Headless B:** persistent owner-control deployment on B is not included in the existing gateway-only lab; require a separate reversible deployment plan.
- **Rollout boundary:** worktree builds do not update the live MC checkout. No merged/pushed/deployed claim until those actions are separately authorized and verified.

## Completed prerequisite: F1X-O3

B's own private config now explicitly selects `platform_toolsets.api_server: []` and `cli: []`. The only advertised Room capability is `bot_room`, with manual approvals and the existing 3-iteration cap. Regular API/CLI tool definitions resolve to zero. A live invitation and capabilities read returned that exact policy; the temporary grant was revoked and subsequent access returned 403. B restarted alone; relay/proxy PIDs and all four lab unit hashes remained unchanged, routes stayed empty and public TLS still verifies normally. This closes application-level tool exposure, not OS/network sandboxing or product integration.

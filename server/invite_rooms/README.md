# Invite Rooms — private implementation boundaries

The owner-only `/api/local/room-invites/*` HTTP dispatcher is mounted by `server/local_telemetry_server.py`; its handlers live in `routes.py` and `joining_routes.py`. It remains **off by default**: a valid owner-private `MISSION_CONTROL_ROOM_INVITES_CONFIG` is required, and missing or invalid configuration is indistinguishable from a 404. Pairing endpoints additionally require a valid `pairing` block.

The sidecar now owns complete-bundle control/data listener construction, startup
and shutdown, including the running scoped registry and material coordinator.
Incomplete configuration still fails closed. That implementation is distinct
from accepted two-native-process E2E or cross-network deployment; those gates
remain separate. This is a living technical reference, not a release receipt.

Start with the [user guide](../../docs/invite-rooms-user-guide.md),
[operator setup](../../docs/runbooks/invite-rooms-setup.md) or
[agent runbook](../../docs/runbooks/invite-rooms-agent.md).
The [HTTP reference](../../docs/api.md#invite-only-rooms) is the route/response
source of truth; old slice plans are design history, not current onboarding.

The product service supports exactly one local owner profile and one remote member
on a distinct installation. The owner identity is trusted construction configuration,
not a browser field and not whatever a connected gateway reports.

## Store version and owner-directed recovery

New stores are explicitly **schema version 3**. Version 2 migrates by adding
empty history policy/admission tables; existing Rooms acquire no policy or
consent by inference. Pre-generation version 3 admission rows receive durable
generation metadata on reopen. Back up the coherent databases, keys and journals
before migration; it grants no new access. Version 1 is refused immediately
with `InviteError("legacy_schema_recovery_required")`, before schema initialization,
owner binding, or any application write. This includes both the real accepted
pre-P1 layout and the short-lived unversioned P1 layout. The ambiguity is deliberate:
there is no safe way to infer historical ownership or approval of a new policy.
Future versions remain write-refused with `schema_too_new`, including when a live
store object observes a version change made by another process.

There is **no automatic adoption of version 1 ownership/consent**. On that error
the operator must stop using that path, preserve the old database and
its journal files as an owner-controlled archive/backup, and explicitly select a
**new** empty store path for the trusted owner. Recreate the intended lobby and issue
new invitations, then obtain fresh host approval and actual Joining-owner consent
for the full reviewed policy/catalog. Do not relabel a version, delete old data,
copy historical consent into the new store, or overwrite the archived database.
Any future continuity migration/adoption requires a separately reviewed owner-driven
workflow. Existing IDs, roster, redemptions and immutable decisions are left intact
in the rejected database; they are not represented as approved product state.

## Generic lab coordinator versus product policy

`RoomLobbyService` remains the single native planning/create/register state machine.
Its accepted four-field lab contract is retained for existing private control,
`OnceActivation._plan`, `PrivateRegistry.resolve`, and `gate_p2_p4_local` callers.
Four-field redemptions have SQL **NULL** catalog/policy pins, explicitly unapproved
for product use. There are no synthetic all-zero digests. Six-field decisions cannot
be downgraded by supplying a four-field binding: all stored normalized pins must match.
The signed Joining-owner control protocol is unchanged.

`InviteRoomsService` is the mandatory product wrapper. It always requires all six
pins from the approved registry to match durable host and Joining-owner decisions.
No option or fallback selects the lab path. Its resolver wrapper checks those pins
again, builds the exact scoped request, and resolves **only** approved scoped material.
The coordinator also independently validates the real native catalog for six-pin
plans. The product passes `expected_authority` from trusted owner installation;
capabilities must match it before native create/register. Exact room/member/authority
readback is still mandatory. Activation sends no prompt; scoped material/grants
may be acquired through the joining owner's consent-bound material channel.

## Portable canonical native contract

The deployment integration must make the native `gateway.hosted_room_peer` contract
importable as an installed dependency (or explicitly provision the Python import
environment). Product code never searches an operator home or mutates `sys.path`.
Missing contract returns sanitized `native_contract_unavailable`; malformed catalogs
return `invalid_catalog`. Tests explicitly supply a read-only native source import
path via the test runner, not a fallback in the product or test modules.

## Scoped private material trust

Trusted owner provisioning uses immutable `MaterialScope` and `ScopedPeerMaterial`.
Scope includes owner installation/profile, lobby ID, planned native room ID, member
ID, invite ID, selected peer ID, endpoint, fingerprint, target installation/profile,
reviewed catalog/policy digests, and pairing generation. Static registration of a peer
or global grant is **not** approval for a room. Host material is admitted only from a
signed owner envelope and stored in the owner-private encrypted material-receipt store;
the first receipt for a complete scope is immutable.

The provisioning integration must establish provenance before approval; this layer
checks metadata association, **not native grant signature/HMAC validity**. Only actual
native admission can establish the latter. Never approve a browser-supplied bearer
or turn a claimed scope dict into proof of origin.

On explicit activation, the registry loads the exact encrypted receipt on every
request (including after a process restart), rechecks the pinned binding,
catalog and pairing generation, and obtains the adapter URL only from the injected
local route provider. With no route provider result, activation fails closed before
any native connect. Inspection/readiness never resolves private material, mints,
seals, creates, registers or sends.

The JSON registry is installation-local trusted configuration; it contains identity,
TLS CA, catalog and consent pins only. Private grants never live in it.

## Activation idempotency and the material-channel seam

`RoomInviteStore.mark_created` runs BEFORE any per-peer `groups.peer.register`
call, and `mark_peer_registered` runs only after that peer's native
registration is readback-verified (`RoomLobbyService.activate`). A lobby in
state `"created"` is therefore not proof that every remote roster member is
registered. `InviteRoomsService.activate` treats a `"created"` lobby as an
explicit idempotency check, not a success shortcut: it reads the store's
read-only `peer_registration_status` projection (zero native calls) and
returns the activated result only when every remote roster member is
durably `registered`; otherwise it raises `ServiceError("registration_incomplete")`
(HTTP 409). The public route does not blindly retry or infer success from a
partial registration; preserve its IDs for operator inspection. There is no
generic reconciliation HTTP endpoint in the published route table.
`InviteRoomsService.activation_status` exposes the
same projection as a redacted `registered: bool` per peer entry once the
lobby has reached `"created"`.

Directly constructed services without material/route injection remain
fail-closed. The live sidecar's `SidecarDataPlane` / `MaterialCoordinator` and
scoped registry now supply the product wiring; an unset legacy test injection
is not proof that the production process has no material channel. The JSON
format does not install arbitrary resolver callables or static global grants.
Native dependencies must be importable in the installed sidecar interpreter.
Canonical `native_rpc: {profile_home: ...}` attaches only to the exact existing
owner using discovery and a fresh session ticket; it never ensures/restarts a
gateway or falls back to a Bearer/dashboard endpoint.

**Mutation ambiguity:** if `/activate`
times out mid-flight, the coroutine is cancelled and no durable "ambiguous"
marker is written. The lobby is left exactly where the coordinator's own
native calls left it (possibly `"created"` with a partially registered
roster, or still `"sealed"`/`"open"` if the timeout hit before
`groups.create`). The next attempt either resumes cleanly through the
idempotency check above, or meets a native conflict (`409`-mapped
`native_room_conflict`/`native_peer_conflict`) if the prior attempt is still
in flight on the native side. The operator must inspect that existing scope;
do not create another lobby or treat an unavailable native reply as rollback.

## Control signals and post-mutation ambiguity (MVP.2a-fix4)

`invite_rooms.routes._run_activation` only ever catches an in-flight
activation's outcome to let the native transport's own teardown run before
deciding what escapes; it never converts the outcome itself. A control
signal (`KeyboardInterrupt`/`SystemExit`/`GeneratorExit`/`CancelledError`),
a `TimeoutError`, or an already-classified `ServiceError`/`InviteError`/
`PeerRegistryError` is always re-raised exactly as it left `scoped.activate`,
regardless of what the transport's teardown (`__aexit__`) claims to have
suppressed. Only an ordinary, unrecognized `Exception` may still be
reclassified.

`InviteRoomsService.activate`'s own catch-all for an unrecognized exception
from `RoomLobbyService.activate` — and the routes-layer fallback for an
unrecognized exception leaking past that service boundary — share one
derivation (`InviteRoomsService.classify_unconfirmed_mutation`), so routes
and the service always agree: the durable lobby state is read fresh, never
the exception text. A lobby already `"created"` with an incomplete remote
peer registration is `ServiceError("registration_ambiguous")` (the native
create is confirmed; some peer registration may or may not have landed). A
lobby that never reached `"created"` but already has a pinned native spec
(`pin_native_spec`, written right before `groups.create` is attempted) is
`ServiceError("creation_ambiguous")` (the create may have been admitted
remotely without a durable confirmation). Only when neither durable signal
is present — nothing was ever admitted — does the catch-all fall back to
`ServiceError("activation_failed")`. The classified-code set is unchanged;
this only narrows which of the existing codes an unrecognized exception
maps to.

## Browser host UI contract (MVP.2a-fix5)

`GET /lobbies/{id}/activation` peers now also carry `invite_id` (the latest
non-revoked invitation for that member, or `null`) and `redeemed_binding`
(`null` before redemption, else the exact 5-key durable binding the owner is
deciding against: `fingerprint`, `target_install_id`, `target_profile`,
`catalog_digest`, `policy_digest` — never `endpoint_id`, secrets, or grants).
`POST /lobbies/{id}/invites/{invite}/decision` now requires a body
`expected_binding` carrying the identical 5 keys shown in the activation
view; a mismatch on any field is `409 binding_changed` with zero durable
state change, and a missing/malformed `expected_binding` is
`400 invalid_body`. A decision attempted before the invite is redeemed keeps
the pre-existing `404` (`invite_unavailable`) behavior unchanged.
`GET /paired-peers` lists the owner's pre-paired peers for building a lobby
roster without a free-text endpoint: `{"peers": [{"peer_id",
"installation_id", "profile", "capability_digest"}]}`, redacted (no
endpoint, fingerprint, or grant), empty when none are configured. All three
endpoints share the existing owner-bearer auth, feature gate, and Origin/CSRF
rules; `InviteRoomsService.decide_host`'s `expected_binding` parameter is
**mandatory at the service boundary** (MVP.2a-fix6, R3-1) — a missing or
malformed value is rejected as `ServiceError("invalid_peer_binding")` with
zero store mutation, identically for the HTTP route and every direct/
headless caller. There is no unguarded internal path left: every caller must
supply the exact displayed 5-key projection.

The activation view (and readiness used by `preflight_activate`/`activate`)
pair each member's **latest** non-revoked `invite_id` with the redemption of
that *same* invite_id only (MVP.2a-fix6, R3-2) — never a stale redemption
left behind by an earlier invite of the same member (e.g. deny A, then
reissue B: the view reports `invite_id` B, `redeemed_binding: null`,
`host_decision`/`joining_consent: "pending"` until B itself is redeemed and
decided). Invite/redemption history is never exposed; only the latest pair.

## Route table

| Method | Path                                                   | Handler                |
| ------ | ------------------------------------------------------ | ----------------------- |
| POST   | `/lobbies`                                              | `_h_create_lobby`       |
| GET    | `/lobbies/{lobby_id}`                                   | `_h_inspect_lobby`      |
| POST   | `/lobbies/{lobby_id}/invites`                           | `_h_issue_invite`       |
| POST   | `/lobbies/{lobby_id}/invites/{invite_id}/revoke`        | `_h_revoke_invite`      |
| POST   | `/lobbies/{lobby_id}/invites/{invite_id}/decision`      | `_h_decide_host`        |
| GET    | `/lobbies/{lobby_id}/activation`                        | `_h_activation_status`  |
| POST   | `/lobbies/{lobby_id}/activate`                          | `_h_activate`           |
| GET    | `/paired-peers`                                         | `_h_paired_peers`       |

All paths are mounted under `/api/local/room-invites`. Every route requires
the owner bearer token (checked before the feature gate, so an unauthorized
caller always gets `401` regardless of gate state) and, when an `Origin`
header is present, an exact `MISSION_CONTROL_ALLOWED_ORIGIN` match.

## Error code table

| HTTP | `error`             | `code` (subset)                                                                 |
| ---- | ------------------- | -------------------------------------------------------------------------------- |
| 400  | `invalid_request`   | `invalid_lobby`, `invalid_invite`, `invalid_decision`, `invalid_peer_binding`, `invalid_peer_target`, `invalid_roster`, `mvp_roster_limit`, `distinct_peer_installation_required`, `catalog_digest_mismatch`, `invalid_binding`, `invalid_material_scope`, `invalid_owner_scope`, `invalid_peer_id` |
| 400  | `bad_request`       | `invalid_content_type`, `invalid_body`, `invalid_content_length`                  |
| 401  | *(unauthorized)*    | —                                                                                 |
| 403  | `forbidden_origin`  | —                                                                                 |
| 403  | `forbidden`         | `authority_mismatch`                                                             |
| 404  | `not_found`         | `lobby_unavailable`, `invite_unavailable`, `owner_profile_mismatch`, `owner_context_mismatch`, `unknown_peer`, `peer_not_paired`, `peer_binding_mismatch` |
| 409  | `conflict`          | `member_already_redeemed`, `fingerprint_mismatch`, `host_approval_required`, `decision_final`, `consent_required`, `native_spec_conflict`, `catalog_drift`, `material_scope_conflict`, `material_scope_mismatch`, `material_scope_unapproved`, `activation_not_ready`, `native_room_conflict`, `native_peer_conflict`, `native_rejected`, `registration_incomplete`, `binding_changed` |
| 413  | `payload_too_large` | —                                                                                 |
| 503  | `unavailable`       | `activation_unavailable`, `creation_ambiguous`, `registration_ambiguous`, `native_peer_unconfirmed`, `native_contract_unavailable`, `activation_failed`, `material_channel_unavailable`, `native_unavailable`, `activation_timeout` |
| 500  | `internal`          | — (sanitized catch-all; never a traceback or an interpolated value)              |

## Joining owner and immutable history

The JOINING owner's operations are exposed through `joining_routes.py` under
`/imports/*`, with the same authenticated owner boundary as the host routes.
Implementation modules include `invitation.py` (network-free invitation
parsing against a pinned, constructor-supplied origin, plus the joining owner's own
`ApprovedHostRegistry`), `consent.py` (the pure Ed25519 canonical/verify primitives
and an owner-private `ConsentKey`), `joining_store.py` (durable, schema-versioned,
fail-closed joining-side SQLite state — imports, confirmed scopes, consent receipts,
mint journal), `joining.py` (`JoiningOwnerService`: preview/redeem/confirm_scope/
consent), and `joining_material.py` (`TargetMaterialMint`: the target-side handlers
exercised during host activation, with a durable encrypted mint journal).

The product transport uses journal-authenticated peer contexts and pinned mTLS.
Address-map entries and registry projections are not consent or peer trust.
Current bind/destination validation is loopback-only; the issuer's invitation
origin and browser CORS are not network provisioning. See the operator guide
for the deny-all bootstrap and explicit scope-mapping/restart boundary.

At Room creation, `history_policy` is fixed to `full` or `postjoin` for the
Room's lifetime. The browser defaults to unchecked (`postjoin`); the API's
omitted-policy default is `full`, so headless callers must send the reviewed
choice. Revocation/reissue/readmission never reopen that policy decision.

`history_enabled` is a separate private runtime boolean, default false. It
gates history HTTP operations and construction/registration of history handlers.
OFF preserves lazy identity loading for cold-start/nondefault-profile assembly.
It does not change stored policy or issue receipts. Both modes require explicit
owner-issued read approval and guest acceptance of the exact current scope.

`history_host.py`, `history_consent.py` and `room_log.py` bind reads to current
peer identity/generation, owner/home, native authority/epoch, member/recipient
and durable admission. The lifecycle generation is not a log cursor. Final
revalidation refuses revocation/replacement during native I/O; the guest verifies
the owner signature before producing its distinct acceptance. Eligible text
events preserve native IDs/order/cursors, without private native metadata.

The complete [API reference](../../docs/api.md#history-policy-and-read-consent)
describes owner history approval, guest acceptance and bounded log reads. Tests
include `test_room_history_policy`, `test_history_consent_store`,
`test_history_product_http`, `test_history_integrated_regressions`,
`test_history_owner_action`, `test_room_log_slice` and sidecar gate/lifecycle tests.
Use the [actual CI lane](../../.github/workflows/ci.yml) for Core/dependency pins
and separate unittest/pytest selections. Copied Core methods, independent native
processes, cross-network topology and physical-phone/Safari checks are different
proof classes; a selected test or green CI does not close all of them.

# Invite rooms MVP.1 — private integration boundaries

No live HTTP routes, native listener, deployment, or automatic activation is wired here.
The product service supports exactly one local owner profile and one remote member
on a distinct installation. The owner identity is trusted construction configuration,
not a browser field and not whatever a connected gateway reports.

## Store version and owner-directed recovery

New stores are explicitly **schema version 2**. Version 1 is refused immediately
with `InviteError("legacy_schema_recovery_required")`, before schema initialization,
owner binding, or any application write. This includes both the real accepted
pre-P1 layout and the short-lived unversioned P1 layout. The ambiguity is deliberate:
there is no safe way to infer historical ownership or approval of a new policy.
Future versions remain write-refused with `schema_too_new`, including when a live
store object observes a version change made by another process.

There is **no automatic migration** and this batch does not touch production data.
On this error the operator must stop using that path, preserve the old database and
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
readback is still mandatory. No send or mint is performed on activation.

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
and reviewed catalog/policy digests. Registration of a peer/global grant alone is
**not** approval for a room. `PeerRegistry.approve_scoped_material` explicitly associates
already-provisioned private material with this complete scope, defensively snapshots
it, and requires exact match with the approved peer metadata, adapter URL, catalog,
and grant. Conflicting associations are rejected. This privileged method is not a
product service operation or a way to record Joining-owner consent.

The provisioning integration must establish provenance before approval; this layer
checks metadata association, **not native grant signature/HMAC validity**. Only actual
native admission can establish the latter. Never approve a browser-supplied bearer
or turn a claimed scope dict into proof of origin.

On explicit activation, an optional trusted private loader receives the complete
typed scope and must return the identical approved `ScopedPeerMaterial`. The registry
compares scope, URL, binding, catalog and private grant before native mutation, then
revalidates metadata after the await. Without a loader it uses its own approved
snapshot; without a scoped approval it fails closed. No unscoped `PeerMaterial` can
fall through. Inspection/readiness never calls that loader, resolves private material,
mints, seals, creates, registers or sends. Readiness reports durable consent/metadata
pins, not proof of private material availability or native admission.

The registry is installation-local trusted in-memory configuration; provisioning
must reconstruct scoped approvals after a restart. This batch adds no secret store,
new consent protocol, HTTP ingress, automatic retry or grant minting workflow.

## Activation idempotency and the material-channel seam (MVP.2a-fix)

`RoomInviteStore.mark_created` runs BEFORE any per-peer `groups.peer.register`
call, and `mark_peer_registered` runs only after that peer's native
registration is readback-verified (`RoomLobbyService.activate`). A lobby in
state `"created"` is therefore not proof that every remote roster member is
registered. `InviteRoomsService.activate` treats a `"created"` lobby as an
explicit idempotency check, not a success shortcut: it reads the store's
read-only `peer_registration_status` projection (zero native calls) and
returns the activated result only when every remote roster member is
durably `registered`; otherwise it raises `ServiceError("registration_incomplete")`
(HTTP 409). Recovery/resume for a lobby stuck in that state belongs to the
explicit reconcile packet (P2c) — this packet does not retry or resume a
partial registration. `InviteRoomsService.activation_status` exposes the
same projection as a redacted `registered: bool` per peer entry once the
lobby has reached `"created"`.

`InviteRoomsService(..., material_resolver=...)` and
`runtime_config.load_service(..., material_resolver=...)` are the explicit
constructor seam for the joining-owner material channel. Neither this module
nor the owner-private JSON config format invents a resolver configuration:
by default `material_resolver` stays `None`, and any activation attempt that
actually needs remote peer material fails closed with
`ServiceError("material_channel_unavailable")` (HTTP 503) BEFORE any native
RPC call, including `groups.capabilities`. `invite_rooms.routes._MATERIAL_RESOLVER`
is the single process-wiring point the routes layer reads today (`None`); P2b's
joining-owner integration is expected to replace that module attribute with a
real resolver coroutine, not to invent a new JSON field or a request-scoped
parameter.

**Known limitation, not fixed by this packet (P2c input):** if `/activate`
times out mid-flight, the coroutine is cancelled and no durable "ambiguous"
marker is written. The lobby is left exactly where the coordinator's own
native calls left it (possibly `"created"` with a partially registered
roster, or still `"sealed"`/`"open"` if the timeout hit before
`groups.create`). The next attempt either resumes cleanly through the
idempotency check above, or meets a native conflict (`409`-mapped
`native_room_conflict`/`native_peer_conflict`) if the prior attempt is still
in flight on the native side. Reconciling that ambiguity window is explicitly
out of scope here and is the next packet's (P2c) job.

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

## MVP.2b-core — joining-owner side (no HTTP, no transport promotion)

Five new, single-topic modules add the JOINING owner's local operations, symmetric
to P1's host-side `InviteRoomsService`: `invitation.py` (network-free invitation
parsing against a pinned, constructor-supplied origin, plus the joining owner's own
`ApprovedHostRegistry`), `consent.py` (the pure Ed25519 canonical/verify primitives
and an owner-private `ConsentKey`), `joining_store.py` (durable, schema-versioned,
fail-closed joining-side SQLite state — imports, confirmed scopes, consent receipts,
mint journal), `joining.py` (`JoiningOwnerService`: preview/redeem/confirm_scope/
consent), and `joining_material.py` (`TargetMaterialMint`: the target-side handlers
exercised during host activation, with a durable encrypted mint journal).

These preserve the accepted S2 lab contract (`lobby_owner.JoiningOwner`,
`lobby_material.TargetMint`/`MintJournal`, `lobby_consent`'s canonical encoding) by
reimplementing the same scope/receipt/envelope shapes rather than importing spike
code into product code; one test drives the REAL lab control classes against this
implementation to prove interoperability (see
`server/tests/test_room_invite_joining.py::RealLabParityTest`). No HTTP route exists
yet; a `ControlChannel`-shaped object (`.call(request) -> dict`) and a bounded async
`rpc(method, params)` callable (the exact contract in `native_rpc.py`) are injected,
so a later packet can wire either the pinned TLS transport or a headless adapter
without touching this module's logic.

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

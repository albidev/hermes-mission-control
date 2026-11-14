# Invite Rooms: agent operation runbook

This runbook is for an agent assisting **explicitly authorized local owners**. It uses the same MC HTTP services as the browser, not a private DB shortcut, an invented `hermes rooms` command or a generic remote RPC bridge. It does not authorize public deployment or automatic owner approval. See the [user guide](../invite-rooms-user-guide.md), [operator setup](invite-rooms-setup.md) and [API reference](../api.md#invite-only-rooms).

## Contract and stop rules

Before proceeding, record which owner authorized which installation/profile, peer, action and immutable history policy. The agent cannot act for the remote owner merely because it can reach that owner's HTTP service.

**Stop** on unknown ownership, missing/changed binding, failed SAS comparison, missing capability, an ambiguous mutation, expired invitation, unavailable authoritative state or absent explicit owner consent. Do not infer successful delivery from a lost response. No blind POST retries, automatic SAS confirmation, manufactured catalog, guessed ID, direct DB edit, downgrade to legacy authentication or source patch in Core.

A pairing code is not trust. An invitation is not participation consent. Participation consent is not history consent. Runtime `history_enabled` is not a Room policy. A successful activation is not permission to send a task. For both `full` and `postjoin`, owner-issued history approval and guest acceptance are separate required actions.

## Prerequisites and credentials

The [setup runbook](invite-rooms-setup.md) must already have established native readiness, private state, the actual listener/destination topology, compatible sidecar imports, invitation-origin pins and a running sidecar on each side. A 200 response from one route is not sufficient.

Use each owner's existing MC credential only on that owner's verified local MC base. Store the credential in an operator-provided private curl config (`0600` under a `0700` directory), never in the URL, public output, command history, issue/card text or this repository. Do not follow redirects or forward that credential to the peer/native data transport. Scoped execution uses its own product-issued grant.

Set these values explicitly for the owner whose step is being executed:

```bash
export MC_BASE=http://127.0.0.1:8765
export MC_AUTH=/absolute/private/owner-curl.conf
export RUN_DIR=/absolute/private/invite-room-operation
umask 077
mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"
```

The base above is an example sidecar listener, not permission to assume that port. `MC_AUTH` is a trusted credential file containing the existing Authorization header; do not generate a new admin credential. Check the file type/owner/mode and the verified endpoint before calling anything.

A bounded helper (no retries, no redirects, body saved privately rather than printed):

```bash
mc_get() {
  curl --config "$MC_AUTH" --silent --show-error --connect-timeout 3 --max-time 20 \
    --output "$RUN_DIR/$2" --write-out '%{http_code}\n' \
    "$MC_BASE/api/local/room-invites$1"
}
mc_post() {
  curl --config "$MC_AUTH" --silent --show-error --connect-timeout 3 --max-time 20 \
    --request POST --header 'Content-Type: application/json' --data-binary "@$RUN_DIR/$2" \
    --output "$RUN_DIR/$3" --write-out '%{http_code}\n' \
    "$MC_BASE/api/local/room-invites$1"
}
```

`$1` is a fixed route suffix below, using an ID from the authoritative response; it must never be an arbitrary URL. Reject responses that are not JSON or do not have the documented structure. Check both curl exit status and HTTP status: a timeout or a non-2xx is not success, even if a body file exists. Keep all result files private; invitation issuance contains a one-time secret.

## Execution order

This is a checkpointed sequence, **not** a script to run unattended. Switch to the other owner's separately authorized base/credential only for that owner's steps.

### A. Inspect; never create trust during a read

```bash
mc_get /paired-peers paired-peers.json
mc_get /pairing pairing.json
```

Expected: authenticated JSON. An empty peer list means pairing is not complete. 401 means auth failed; 404 can mean the additional feature config is absent. Do not interpret either as permission to seed the registry.

If pairing is needed, use the owner panels and [pairing API](../api.md#pairing). The owners exchange signed transcripts, compare the SAS independently and explicitly confirm both sides. The agent can relay public codes only within the authorization given; it cannot replace the independent comparison or sign off on behalf of an absent owner. Verify acknowledgement and peer IDs afterwards.

### B. Host: create one lobby with an explicit policy

Obtain the local handle and selected peer ID from this owner's configured context and `/paired-peers`. Prepare a JSON file such as `create.json`, replacing all example values with the reviewed scope:

```json
{
  "name": "Reviewed Room",
  "roster": [
    {"member_id": "local-owner", "handle": "owner"},
    {"member_id": "remote-member", "handle": "guest", "target": {"peer_id": "peer-REPLACE_WITH_APPROVED_ID"}}
  ],
  "history_policy": "postjoin"
}
```

```bash
mc_post /lobbies create.json lobby.json
```

Expected: 200 with a `lobby` object containing `lobby_id`, `planned_room_id`, `roster` and the selected `history_policy`. Read IDs from that object and preserve them unchanged. The server resolves actual profile/installation/catalog fields; do not supply a browser-selected native endpoint or full fabricated target.

The browser defaults to unchecked (`postjoin`); the API's omitted-policy default is `full`. **Headless callers must always send the explicitly approved policy** rather than depending on that difference. `creation_id` is an optional creation-identity field, not permission to retry an ambiguous Room mutation with a new value. No policy-update route exists.

### C. Host: issue the invitation once

Body file `issue.json`:

```json
{"member_id":"remote-member","invitee_label":"Invited owner"}
```

```bash
mc_post "/lobbies/$LOBBY_ID/invites" issue.json invitation.private.json
```

Expected: 200 with `invite_id`, `expires_at`, one-time `secret` and `invite_url` at the response root. Read privately and deliver through the owner's approved channel. Do not dump the response into logs or a task card. A lost issuance response is not a reason to loop POST; inspect existing state and use explicit revoke/reissue if authorized.

### D. Joining owner: import, read metadata, redeem

The joining owner supplies the complete invitation in private `import.json`:

```json
{"code":"REPLACE_PRIVATELY_WITH_COMPLETE_INVITATION"}
```

```bash
mc_post /imports/preview import.json imported.private.json
```

Expected: 200 with an `import` object containing the product projection/`import_id`. All import actions use this same `{"import": ...}` response envelope. Preview parses locally; it does not make an unknown host trusted. Obtain metadata over the pinned channel, then redeem **only after** explicit joining-owner authorization:

```json
{}
```

Save that empty object as `empty.json` for the fieldless operations:

```bash
mc_post "/imports/$IMPORT_ID/metadata" empty.json metadata.json
# OWNER CHECKPOINT: authorize redeem of this exact invitation.
mc_post "/imports/$IMPORT_ID/redeem" empty.json redeemed.json
```

Expected: 200, with authoritative state transitions. Preserve the same import on refresh/restart. On `redeem_ambiguous`, stop and use the supported owner-directed resolution path; do not resend a secret blindly.

### E. Host: approve the exact redeemed binding

```bash
mc_get "/lobbies/$LOBBY_ID/activation" activation.json
```

Require the current peer's non-null `invite_id` and `redeemed_binding`. The body of the explicit decision is:

```json
{
  "peer_id": "peer-REPLACE_WITH_APPROVED_ID",
  "approved": true,
  "expected_binding": {
    "fingerprint": "REPLACE_FROM_CURRENT_ACTIVATION_VIEW",
    "target_install_id": "REPLACE_FROM_CURRENT_ACTIVATION_VIEW",
    "target_profile": "REPLACE_FROM_CURRENT_ACTIVATION_VIEW",
    "catalog_digest": "REPLACE_FROM_CURRENT_ACTIVATION_VIEW",
    "policy_digest": "REPLACE_FROM_CURRENT_ACTIVATION_VIEW"
  }
}
```

After the host reviews those exact values:

```bash
mc_post "/lobbies/$LOBBY_ID/invites/$INVITE_ID/decision" decision.json host-decision.json
```

Expected: 200 with `{"redemption": ...}`. Copy the five binding values from the fresh view, do not hash/normalize/reconstruct them or replace them from an older invite. On `binding_changed` (409), fetch a fresh view and require another owner review. Never auto-approve a different binding.

### F. Joining owner: confirm scope, then consent to participation

The exact response/view determines `room_id` and the joining `member_id`. In `scope.json`:

```json
{"room_id":"REPLACE_FROM_REVIEWED_SCOPE","member_id":"remote-member"}
```

```bash
mc_post "/imports/$IMPORT_ID/scope" scope.json scope-result.json
```

Expected: 200 with that same scope. After the joining owner reviews the confirmed scope and authorizes participation, `participation.json` is:

```json
{"approve":true}
```

```bash
mc_post "/imports/$IMPORT_ID/consent" participation.json participation-result.json
```

Expected: 200 and durable consent/delivery state. Do not treat pending delivery as ready. Refusal is `{"approve":false}` and is not an authorization to send an alternative consent later without owner direction.

### G. Host: inspect readiness, activate explicitly

```bash
mc_get "/lobbies/$LOBBY_ID/activation" ready.json
# OWNER CHECKPOINT: ready=true; authorize activation of this same fixed roster.
mc_post "/lobbies/$LOBBY_ID/activate" empty.json activated.json
```

Expected: 200 with `status: "activated"` and the confirmed native `room_id` at the response root. A created lobby without confirmed remote registration is not success. On `creation_ambiguous`, `registration_ambiguous` or `registration_incomplete`, preserve all IDs, inspect state and use the existing explicit owner reconciliation flow; do not auto-retry or create another lobby. No prompt/task is sent by this step.

### H. Authorize history separately

These operations require `history_enabled: true` on the appropriate installations, a live admission and current trust. Both `full` and `postjoin` require both owner actions.

Host, after explicit read-access authorization, `history-owner.json`:

```json
{"recipient_id":"peer-REPLACE_WITH_APPROVED_ID"}
```

```bash
mc_post "/rooms/$ROOM_ID/history-approval" history-owner.json history-owner-result.json
```

Expected: 200 with `status: "owner_approved"`. The recipient is the paired peer ID, not a member ID or profile name.

Joining owner, after reviewing and explicitly accepting that access:

```bash
mc_post "/guest-rooms/$ROOM_ID/history-consent" empty.json history-accepted.json
mc_get "/guest-rooms/$ROOM_ID/log?since_seq=0&limit=200" history-page.json
```

Expected: 200; accepted read status is `ready`. Follow the returned cursor/`has_more` without inventing IDs or advancing beyond what the server returned. Preserve original `event_id`, `seq`, ordering and authority. Even if requesting `since_seq=0`, `postjoin` must not release pre-admission messages. The result is a bounded text-event projection, not private native metadata or execution permission.

## Recovery, approvals and evidence

- Guest bot snapshot: `GET /guest-rooms/{room_id}/my-bot`. Decisions use opaque returned request handles and exactly `{"choice":"approve"}` or `{"choice":"reject"}`; approve maps to once, not permanent approval. On 409, fetch a fresh snapshot and require review again.
- Guest leave: explicit owner authorization, then `POST /guest-rooms/{room_id}/leave` with `{}`. Do not mark it successful when native revocation fails.
- Unused-invite revoke, active leave, unpair and native Room disband are different authority boundaries. Never use one as an automatic substitute for another.
- Read failures/offline states never authorize a local-copy transcript or a synthetic empty conversation.
- Keep sanitized evidence: checkout revision, operator scope, exact step/status, IDs in a private record, invariant results and unresolved gates. Exclude secrets, grant bodies, private keys and approval command contents from public handoffs.
- Report implementation tests, in-process copied Core, independent native processes, cross-network and physical-device tests as **different proof classes**. No successful HTTP call or green CI can stand for all of them.

## Completion criteria

Report participation active only after confirmed native activation/registration, and history available only after both read decisions and an authorized canonical page. Report no bot task as executed unless a separate authorized send and its real result were observed. Leave networking, full two-installation recovery, physical-device/Safari checks and final release approval open until independently demonstrated.

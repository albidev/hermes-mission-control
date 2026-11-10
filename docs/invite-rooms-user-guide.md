# Invite-only Rooms: user guide

Use this guide to invite one member from another Hermes installation, or join a Room you have been invited to. This is **not** a public room directory. An invitation never gives its recipient administrative access to the other installation.

**Availability:** this feature is configuration-gated. These instructions describe the implementation on `feat/invite-rooms`; they do not certify a public release or a completed cross-network deployment. Read the [operator setup](runbooks/invite-rooms-setup.md) first if your installation is not prepared. For headless operation, use the [agent runbook](runbooks/invite-rooms-agent.md).

## Who does what

- **Host owner:** creates the Room, selects its fixed roster and history policy, issues the invitation, approves the redeemed identity, activates, and authorizes history access.
- **Joining owner:** verifies the host, imports and redeems the invitation, reviews the exact scope, consents to their member participating, and accepts history access.
- **Bots/agents:** act only within their approved scope. Pairing, a copied link and opening a panel are not consent to execute a task.

The current product scope is one local owner member and one remote member on a **distinct installation**. Multiple arbitrary guests, public discovery and dynamic roster editing are not implied by the controls below.

## Before you start

Both owners need a compatible running Hermes gateway, their own authenticated Mission Control, and an operator-provisioned connection between the installations. A healthy dashboard alone is insufficient: the native gateway must advertise `room_link.enabled: true`, and the MC sidecar needs its separate pairing/joining/data-plane configuration.

The current product transport configuration uses literal loopback listeners and destinations. A URL displayed in an invitation does not provision networking. Do not expose your MC token, terminal, native API or private keys to make a peer reachable. Cross-network connectivity remains a separate operator acceptance gate.

## 1. Pair the installations

Pairing establishes which other installation you trust; it does not join a Room.

1. Open the pairing section in Mission Control. The host owner creates an offer.
2. Exchange the offer and response using the pairing controls on the two installations. Complete the code exchange in the order shown by the panels.
3. **Compare the six-digit SAS through an independent trusted channel**, then each owner explicitly confirms their own side. If it differs, cancel; do not try to repair the code.
4. Wait for the control-channel acknowledgement. A locally confirmed pairing with acknowledgement pending is not a working transport.

Codes are public signed pairing transcripts, not administrative credentials. Nevertheless, copying a code is not a substitute for the independent comparison. An agent must not confirm the SAS for an owner who has not checked it.

## 2. Create the Room and choose history once

Open **Rooms → new Room**. The creation dialog contains **Create / Join** modes (labels follow your selected language). Select **Create**, choose the paired peer, name the Room and review the roster.

The history checkbox determines whether the invited member may read messages **before joining**:

| Checkbox | Stored policy | After explicit history authorization |
|---|---|---|
| Checked | `full` | Eligible canonical text history, including before admission |
| Unchecked | `postjoin` | Eligible messages at or after that member's admission boundary |

The checkbox starts unchecked in the browser form. **This policy is immutable for the lifetime of the Room.** Reissuing an invitation, revoking, re-pairing or readmitting a member cannot toggle it. If you need a different policy, create a different Room with its own explicitly reviewed scope.

This is separate from enabling the history subsystem on an installation. The runtime `history_enabled` flag neither grants read permission nor changes a Room's policy.

If an extended grant-duration choice is shown, it is a request to the target. It is not proof that the joining installation applied that duration. Invitation expiry and execution-grant expiry are different limits.

## 3. Issue and share the invitation

In the host lobby, issue the invitation for the selected remote member and copy the returned link.

- The invitation secret is returned by the issuance response only; it cannot be retrieved later by refreshing the lobby.
- Share the complete link privately. Do not paste it into public logs, issues, task cards or a bot prompt that will be retained publicly.
- If you lose it, use the supported revoke/reissue action; do not edit the database or reconstruct the secret.
- An expired or revoked link is not a new identity or permission. Do not keep redeeming it on a loop.

The operator pins the issuer's HTTPS invitation origin separately from browser CORS and network transport. The Join panel parses the code locally; it does not navigate to an arbitrary pasted URL.

## 4. Join and review the invitation

On the joining installation, open the same new-Room dialog and select **Join**. Paste the invitation, read its sanitized metadata, then explicitly choose to redeem it.

The host must already be paired and approved locally. A link cannot add an unknown host to your trust registry. Check the host, Room/member scope and policy shown by the UI; a mismatch is a stop, not a reason to override the stored binding.

## 5. Approve participation on both sides

The two owner decisions are distinct:

1. The **host** reviews the redeemed peer and the displayed binding, then explicitly approves or rejects it.
2. The **joining owner** reviews and confirms the exact Room/member scope, then explicitly gives or refuses participation consent on their own installation.

Wait for both decisions to be delivered and for readiness to be reported. Merely importing, redeeming, visiting a panel or pairing does not approve participation. If the binding changed, fetch and review the new state; do not auto-approve the replacement.

## 6. Activate explicitly

The host uses **Activate** and its confirmation step. Success requires the backend to confirm native Room creation and remote registration; a lobby marked created is not by itself proof that every member registered.

Activation does not send a task or message. Use the ordinary Room controls for that separately. If activation becomes ambiguous, keep the existing lobby and IDs, inspect its state and follow the recovery guidance below. Do not create another Room to hide uncertainty.

## 7. Authorize and accept history access

After admission, history has its own explicit exchange:

1. The host uses the history-access approval action for the intended peer.
2. The joining owner uses the history acceptance action in the guest view.
3. The guest reads the host's authorized canonical log over the pinned channel.

**Both steps are required for `full` and `postjoin`.** An OFF checkbox means post-admission history, not automatic access without consent. Neither owner approval nor guest acceptance changes the immutable policy selected at creation.

The guest timeline preserves authorized native message/event identities and ordering. It is not a copy of a local Room, a reconstructed conversation or access to private native metadata. A guest history view is read-only; do not infer a guest composer from local Room controls.

## Recovery and leaving

| What you see | What to do |
|---|---|
| No paired peers | Finish pairing; do not enter a guessed endpoint in place of an identity. |
| Feature unavailable | Ask the operator to check configuration and capability readiness. It is not an empty Room. |
| Waiting for approval/consent | The named owner must act on their own installation; do not simulate their consent. |
| Binding/catalog conflict | Reload and review the actual new identity/policy; no automatic approval. |
| Creation/registration ambiguous | Inspect activation status and ask the operator to reconcile the existing lobby. No blind retry/new IDs. |
| Host offline / history unavailable | Keep the status visible and retry a read when the host is available. Do not show an empty or fabricated transcript. |
| An approval expired | Fetch a fresh bot-approval snapshot; never reuse its opaque request handle for a different prompt. |
| Leave requested | Use the joining owner's Leave action. If native revocation fails, do not claim that leaving succeeded. |

Revoking an **unused invitation**, leaving an active membership and unpairing an installation are different actions. They are not interchangeable ways of deleting a Room. Unpairing cuts trust; re-pairing does not silently restore old approvals.

## Verification boundary

Implementation tests include HTTP, default factories, loopback mTLS, durable stores and copied Core methods. They are not a complete independent two-gateway-process journey across different networks. Browser emulation does not prove physical-phone or Safari acceptance. Operators must record those gates separately before claiming their installation is ready for other owners.

Continue with [setup and troubleshooting](runbooks/invite-rooms-setup.md), [headless agent operation](runbooks/invite-rooms-agent.md), or the [API reference](api.md#invite-only-rooms).

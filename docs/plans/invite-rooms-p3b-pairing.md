# Invite-only Rooms — P3b: peer pairing, joining side, two-instance e2e (v2)

**Status:** design frozen after P3b-R0 (REJECT) and P3b-R1 (ACCEPT_WITH_CHANGES, amendments in §8); partial product implementation now exists in PR #127. Pairing/joining modules, route handlers, and browser panels are present, but the control-listener lifecycle, complete two-installation E2E, and overall A2b2a acceptance remain open. The feature is configuration-gated and not deployed.
**Baseline:** `99866cb` on `feat/invite-rooms` (PR #127) is the historical design-review baseline, not the current implementation inventory.
**Goal:** two owners pair their Mission Control installations without hand-editing registry files; the joining
owner receives, inspects, redeems and consents to an invitation from their own MC; the whole flow runs between
two isolated Hermes instances on one Mac before anything touches the VPS lab.

## 0. Historical baseline at `99866cb` (confirmed by P3b-R0)

The table below records the code state at the design-review baseline only. It is intentionally retained for the design rationale and is **not** a current implementation inventory; see the current candidate snapshot in `docs/plans/invite-rooms-mc-mvp.md` and the route matrix in `docs/api.md`.

| Fact | Where | Consequence |
| --- | --- | --- |
| Host registry is rebuilt per request from an owner-private JSON (0600 file, 0700 parent, checked with `stat`, not no-follow/UID); no route writes it. | `routes.py:488-510`, `runtime_config.py:56-67,129-155` | Pairing needs a writer; reload is free. `_is_private_file` gives no no-follow/owner guarantee. |
| `PeerRegistry.register` requires a static `grant` and `adapter_url`. | `peer_registry.py:141-178` | Removed by P3b.0a. |
| The real grant is minted per room by the **target** at activation (`groups.peer.invite`, ttl 600), wrapped in an envelope signed with the joining consent key and sent over the control channel. The host→target trigger is not product-wired. | `joining_material.py:215-279` | Pairing never carries a grant. |
| Host `_check_scoped_material` compares to the static grant; `resolve_scoped_material` rejects a scope that has no in-memory approved association **before** calling the resolver; the per-request registry never has one; `_MATERIAL_RESOLVER=None`. | `peer_registry.py:270-323`, `routes.py:59,505` | A durable host receipt store is required (P3b.0b), not just a resolver. |
| Signature API is `consent.verify(public_key, signed, signature, expected_signed)` over `{scope, grant, catalog, purpose: "joining-owner-material-v1"}`. `validate_material` is scope hygiene; it does not authenticate the grant HMAC (the native target does, at `groups.peer.register`). | `consent.py:35-48`, `joining_material.py:144-193,265-269` | Host verifies the signature first, then scope, then core catalog + `bot_room` rule; native probe stays mandatory. |
| Joining side services exist (`preview` local only; `refresh_metadata` returns only name/expiry; `confirm_scope(room_id, member_id)` mandatory before `consent`); `JoiningStore` takes a directory and creates `joining.sqlite`. No routes, config or UI. | `joining.py:102-242`, `joining_store.py` | P3b.2 needs a scope read-model and an explicit confirm step, not just three routes. |
| Joining `HostBinding` needs the **host's** installation, profile, fingerprint, catalog and policy digests; it sets `endpoint_id = peer_id`. Host `PeerRegistry` keeps a separate `endpoint_id`. | `invitation.py:101-146`, `peer_registry.py:141-149,217-223` | Pairing carries a catalog from **both** sides and defines two directional bindings. |
| Control transport, PKI and the loopback data adapter with firewall exist only in `spikes/` (`tls_stream`, `lobby_control.PinnedControl` over an existing connector, `HostLobbyControl` bound to one lobby, `adapter.py`). `RoomLobbyService` needs a local `/links/<id>` URL. | spikes; `room_lobby_service.py:35-48` | P3b.3 is control transport **and** data adapter/forwarding, split. |
| `groups.capabilities` returns `room_link.catalog` without minting a grant, but may initialise the gateway grant secret, requires durable run storage and may report `room_link.enabled=false`. | core `tui_gateway/methods_groups.py:216-245` | Pairing asks for an explicit profile, rejects disabled/mismatch, core-validates. Catalog remains self-asserted until the native probe. |

## 1. Pairing protocol (P3b.1a)

### Threat model and claims

- A code is **public**: it contains no private key, no grant, no bearer; holding one gives no native call, no
  prompt and no registration without the local owner's authenticated decision.
- Signatures inside codes are self-signed: they bind the transcript, they do not authenticate a stranger.
- Authentication comes from a **short authentication string compared over a second channel** (voice/in person).
  **Claim:** an active attacker on the code channel who substitutes codes succeeds with probability ≈ 10⁻⁶ per
  single SAS comparison on a fresh, non-selectable session, because the SAS uses commit-then-reveal (below) and
  offline grinding of one session is not possible. The aggregate risk grows with the number of comparisons the
  owners perform, which is why pending attempts and retries are bounded (§8, R1-01). No formal proof is claimed
  for this exact instantiation; the structure follows the cited schemes.
  **Non-claim:** if the attacker also controls the second channel, the protocol cannot tell two owners from an
  intermediary. The UI says so.
- Pins are learned **only** from the confirmed transcript, never from a network handshake (no TOFU).

### Why three codes

A two-code exchange with a SAS computed over public transcripts lets an attacker who sees the honest accept grind
the substitute's nonce offline until the 6-digit SAS collides (review finding 02), and it also forced B to type a
SAS A could not compute yet (finding 01). The fix is the standard SAS-based message authentication with a hash
commitment (Vaudenay, *Secure communications over insecure channels based on short authenticated strings*,
CRYPTO 2005; the same structure as ZRTP's hash commitment): the party whose randomness completes the SAS commits
to it before seeing the other side's message and reveals it only afterwards.

```text
A (host)                                         B (joining)
1. offer   = sign_A{ids_A, keys_A, catalog_A, C = H("mc-pair-commit-v1" || r_A)}   ──►
                                                 2. accept = sign_B{H(offer), ids_B, keys_B, catalog_B, r_B}
                                       ◄──
3. reveal  = sign_A{H(offer), H(accept), r_A}    ──►   B checks H(r_A) == C
   SAS_A = SAS(offer, accept, r_A)                      SAS_B = SAS(offer, accept, r_A)
4. both owners read SAS aloud; each owner types the OTHER side's SAS into their own MC and confirms.
```

- A fixes `C` in the offer before seeing any accept; an attacker must fix the substitute accept (sent to A)
  before `r_A` is revealed, and fix the substitute offer (sent to B) with its own commitment before seeing B's
  accept. After that, nothing it controls can steer either SAS within that session (bounds on parallel/retried
  sessions in §8, R1-01).
- The other ordering (attacker first completes the A side, learns `r_A` and SAS_A, then runs the B side) is
  covered by `r_B`: B's fresh 32-byte nonce enters SAS_B through `H(accept)` and is chosen by B after it has
  received the (substituted) offer, so the attacker cannot steer SAS_B to SAS_A either. Each B attempt needs a
  manual paste by B's owner and is single-use.
- `r_A` is 32 random bytes, stored by A in its pairing journal only; never in the offer.
- A failed SAS comparison, an expired code, or a second accept/reveal for the same offer aborts the attempt
  permanently (tombstone); a new attempt needs a new offer.
- Code 3 grants nothing: it lets B compute the SAS. Neither side is operational until **both** typed-back
  confirmations exist **and** the application ack described under "Activation of a pairing".

### Encoding (finding 10)

- Wire: `mcpair1.<kind>.<b64url(payload)>.<b64url(sig)>`, `kind ∈ {offer, accept, reveal}`. Limits: ≤ 12 KiB
  encoded, ≤ 9 KiB decoded payload, checked before decoding; the budget is validated in tests against a real CA
  PEM plus a real `groups.capabilities` catalog from the scratch homes (if a real pair exceeds it, the limit is
  raised in the doc, never truncated).
- Payload bytes: strict JSON decoder (`object_pairs_hook` rejecting duplicate keys, `parse_float` and
  `parse_constant` rejecting floats/NaN, integer-only numbers, exact key sets per kind, bounded string lengths),
  then re-encoded with `consent.canonical`; a payload whose bytes differ from its canonical re-encoding is
  rejected.
- `sig = Ed25519(consent_key, "mc-pair-sig-v1\x00" || kind || "\x00" || payload_bytes)`.
- `H(x) = SHA-256` over raw bytes; hashes are hex in JSON. `H(offer)`/`H(accept)` are over the **payload bytes**
  of the respective code.
- `SAS = int.from_bytes(HKDF-SHA256(ikm = H(offer)‖H(accept)‖r_A, salt = b"mc-pair-sas-salt-v1",
  info = b"mc-pair-sas-v1", length = 8), "big") mod 10⁶`, zero-padded to 6 digits.
- `endpoint_id = "link-" + hex(HKDF(ikm = H(offer)‖H(accept), salt = b"mc-pair-link-v1", length = 16))`,
  recomputed by both sides at commit, never accepted from input; it is a public link name, not an authorization.
  A re-pair produces a new value; old values are tombstoned and never reused.

### Payloads

Each side includes, read from its **own native gateway** through the owner-authenticated native RPC, never from
the browser: `installation_id`, `profile` (explicit, configured), `catalog` (`groups.capabilities` →
`room_link.catalog`, rejected if `enabled=false` or installation/profile mismatch, then
`GatewayRoomCatalog.from_mapping` + `bot_room`/manual-approval rule), `consent_public_key`, `tls.ca_pem`,
`tls.leaf_fingerprint`, `expires_at` (integer, ≤ now + 15 min), `nonce`.

### Two directional bindings (finding 05)

- On A (`peers.json`): `{peer_id: local name, endpoint_id, installation_id: B, profile: B, fingerprint: B leaf,
  ca_pem: B, catalog: B, consent_public_key: B, pairing_generation}`. No `grant`, no `adapter_url`.
- On B (`hosts.json` → `ApprovedHostRegistry`): `{peer_id: local name, endpoint_id, installation_id: A,
  profile: A, fingerprint: A leaf, ca_pem: A, catalog_digest/policy_digest: A's catalog, consent_public_key: A,
  invitation_origin, pairing_generation}`. `HostBinding.binding` changes from `endpoint_id = peer_id` to the
  stored `endpoint_id` (tested change; not a loosening, both values are pinned).
- B's `self_binding` for a link is built from B's **own** native identity, catalog and leaf — owner-fixed local
  state, never learned from A or an invitation.

### Decision journal and publication (finding 08)

Owner-private `pairing.sqlite` (WAL, `BEGIN IMMEDIATE`) is the single source of truth; the JSON registries are
**projections**.

- States A: `offered → accept_imported → revealed → confirmed → acknowledged | aborted | expired`.
  States B: `offer_imported → accepted → reveal_imported → confirmed → acknowledged | aborted | expired`.
- Each transition is one transaction that checks the exact `H(offer)`/`H(accept)` binding, both expiries
  (no implicit renewal) and the current state; cancel/complete/abort are serialized by the same transaction.
  Consumed offers and accepts are tombstoned by hash on both sides.
- Projection: on `confirmed`, the row is marked `publish_pending` in the same transaction; a projector then
  takes an exclusive `flock` on a stable sibling lock file and performs **read → validate → modify → write temp
  (0600, `O_NOFOLLOW|O_EXCL`) → fsync → rename → fsync dir** while holding it, and marks `published` in
  `pairing.sqlite`. A crash between the two is reconciled at the next request: the projector recomputes the
  registry from the journal (idempotent). A JSON entry with no matching `published`/`acknowledged` journal row
  fails closed (feature 404 + sanitized warning), never silently trusted.
- Unknown, expired, used and aborted codes return the same `pairing_unavailable` from preview, commit and
  control operations; there is no unauthenticated polling route.

### Activation of a pairing (finding 03)

- After both owners confirm, A pins B (and B pins A) **from the transcript**. The first mTLS handshake on the
  control channel proves key possession only.
- Over that channel A sends `pair_ack = sign_A{"mc-pair-ack-v1", H(offer), H(accept), endpoint_id}`, emitted
  only after A's `confirmed` transaction committed. B verifies against its own `confirmed` row with the same
  hashes and moves to `acknowledged`, then answers with `sign_B{"mc-pair-ack-v1", …}`; A moves to
  `acknowledged` on receipt. Until then B's import routes reject invitations from that host with
  `host_pairing_incomplete`, and A cannot create a lobby that targets B. A unilateral `confirmed` record is
  visible as pending, never operational.

### Identity (finding 09)

- One identity per **installation/profile**: `identity_dir` is an explicit, required config field (no hardcoded
  path), distinct per Hermes home in the e2e gate. Contents: `consent.key` (existing `ConsentKey` format),
  `tls/ca.{key,pem}`, `tls/leaf.{key,pem}`, `identity.json` (installation/profile read from the local native,
  must match config).
- Initialization: lock file (`flock`), generate into `identity_dir/.staging-<random>`, validate certificates
  (strict X.509 profile from `pki.py`, leaf validity ≤ 1 year), fsync, atomic rename to final names, fsync dir.
  Pre-existing files with wrong mode/owner/type (symlink, non-regular) are refused, not repaired. Publication is
  a single directory rename of the complete bundle to `identity_dir/current` (§8, R1-04), so a crash leaves
  either no `current` or a complete one.
- Private keys never leave the machine; CA certificate and leaf fingerprint are public and are exactly what the
  codes carry. Leaf expiry or key compromise = re-pair every link that pins it; no silent rotation.

### Unpair (finding 11)

- `DELETE /peers/{peer_id}` writes `unpaired` + increments `pairing_generation` in the journal, tombstones
  `endpoint_id`, re-projects the registry.
- Every new operation (lobby creation, invite, redeem, consent, material request, mint callback, control
  channel accept) re-reads the current generation from the journal and rejects stale handlers/channels; open
  control channels for that link are closed by the transport registry on generation change. Old imports never
  become new consents.
- UI states it plainly: unpair does **not** revoke already-minted native grants and does **not** stop runs that
  were already admitted; the remote side may keep a stale record. Effects admitted before the cutover may
  complete; no new effect is admitted after it (durable admission boundary, §8, R1-05).

### HTTP routes (`/api/local/room-invites`, same auth/Origin/404 rules, `Cache-Control: no-store`, no body logging)

| Route | Side | Effect |
| --- | --- | --- |
| `POST /pairing/offers` | A | Journal `offered`, return offer code once. |
| `POST /pairing/offers/{id}/accept` `{code}` | A | Verify accept against the open offer, core-validate B's catalog, journal `accept_imported` → `revealed`, return reveal code once and SAS_A. |
| `POST /pairing/offers/{id}/confirm` `{sas}` | A | `sas` must be the value read aloud by B and equal SAS_A; journal `confirmed`; publish. Wrong SAS → `aborted`. |
| `POST /pairing/offers/{id}/cancel` | A | Abort. |
| `POST /pairing/imports` `{code}` | B | Verify offer, core-validate A's catalog, journal `offer_imported` → `accepted`, return accept code once and A's identity for review. |
| `POST /pairing/imports/{id}/reveal` `{code}` | B | Verify reveal and commitment, compute SAS_B, journal `reveal_imported`; return SAS_B. |
| `POST /pairing/imports/{id}/confirm` `{sas}` | B | Same rule as A; journal `confirmed`; publish. |
| `GET /pairing` | A/B | Redacted journal rows (id, role, state, expiry, peer identity once known). Never codes, `r_A` or SAS after confirmation. |
| `DELETE /peers/{peer_id}` | A/B | Unpair as above. |

### UI (P3b.1b, separate packet)

"Paired peers" section: `Pair a peer` (offer → copy → paste accept → copy reveal → read SAS → type B's SAS),
`I have a pairing code` (paste offer → review identity/policy → copy accept → paste reveal → read SAS → type A's
SAS). Countdown, explicit "this does not protect you if someone also controls the phone call", list with
installation, profile, policy summary (new deliberate contract, not a silent widening of `paired-peers`) and
`Unpair`. Nothing in localStorage.

## 2. Host material receipt (P3b.0b, finding 04)

- Owner-private `material_receipts.sqlite` (0600/0700), grant column encrypted at rest with the same mechanism
  `JoiningStore` uses for secrets.
- Mapping from the target envelope to the host `MaterialScope`: `scope.host_id → owner_installation_id` (must
  equal trusted owner), `scope.room_id → planned_room_id`, `lobby_id`/`member_id`/`invite_id` exact against the
  durable lobby, six binding fields exact against the pinned peer; `owner_profile` and `peer_id` come from local
  configuration and the lobby roster, never from the envelope.
- Admission order: signature (`consent.verify` with the pinned B consent key) → scope mapping → core catalog
  validation + `bot_room` rule → expected binding of the host decision → insert. The first validly signed
  envelope for a scope is immutable; a different one for the same scope is `material_scope_conflict` and zero
  native mutation; the identical one is idempotent.
- `PeerRegistry.resolve_scoped_material` gains a durable loader seam: the approved association is read from the
  receipt store on every request, so a fresh process (per-request registry) resolves it after restart. The
  existing negatives stay: unscoped/global material, unapproved loopback URL, binding/URL/catalog substitution,
  conflicting second envelope.
- The local adapter URL is not pairing data: it comes from the local transport registry for that
  `endpoint_id` (P3b.3b) and is validated with `_validate_adapter` at resolution time.

## 3. Joining side (P3b.2, finding 06)

- Config block `joining`: `store_dir` (directory, `JoiningStore` creates `joining.sqlite`), `identity_dir`
  (same as pairing), `hosts_path`. The invitation origin is pinned **per host** in the host binding at pairing
  time (owner-configured string, never from an invitation). MVP restriction kept explicit: one active host link
  per joining profile; a second is refused, not silently chosen.
- Routes wrapping `JoiningOwnerService` without changing its state machine:
  `POST /imports/preview` (local only, host selected by the owner from paired hosts) →
  `POST /imports/{id}/metadata` (`refresh_metadata`, name/expiry over the pinned channel) →
  `POST /imports/{id}/redeem` → `GET /imports/{id}` (status read-model incl. host decision, polled by the UI
  with backoff) → `POST /imports/{id}/scope` (`confirm_scope` with the `room_id`/`member_id` the host returned
  after its approval, shown to the owner verbatim; never auto-confirmed) → `POST /imports/{id}/consent`
  `{approve}` → `POST /imports/{id}/resolve` (redeem ambiguity, owner decision).
- Inbound material: `TargetMaterialMint.mint` is reachable only through the control dispatcher (§4), with B's
  local native RPC for `groups.peer.invite`; never over the browser HTTP surface.
- UI "Received invitations": paste → preview host identity → fetch metadata → redeem → wait for host approval →
  review exact scope/policy digest → confirm scope → consent (separate button) → wait for activation → open the
  canonical Room view where available. Accepting never sends a prompt.

## 4. Control transport and data adapter (P3b.3a / P3b.3b, finding 07)

- **P3b.3a control:** product `server/invite_rooms/control/`: port of `SequencedTLS`, pinned mTLS contexts
  (CA + exact leaf fingerprint from the confirmed binding), bounded frames, and a **link dispatcher** that maps
  an authenticated `endpoint_id` + generation to the allowed operations (`pair_ack`, `preview`, `redeem`,
  `consent`, `status`, `material`) for lobbies/invites that belong to that peer only. Handlers run concurrently
  (the target's `material` call during a host activation must not deadlock behind the host's pending request);
  each op has a timeout and a bounded queue. First transport is direct loopback mTLS between the two sidecars;
  the relay/WSS path is ported later under the unchanged network rule (Mac→VPS only, mTLS end-to-end, 403
  outside approved links). Spike modules then import the product package and parity tests are rerun (as in R7).
- **P3b.3b data adapter:** product port of the loopback `/links/<endpoint_id>` adapter with the fixed
  `room_firewall` policy, forwarding the native RoomLink HTTP calls over the same pinned link; registered per
  `endpoint_id` and generation; this is where the host's local adapter URL comes from.
- **What B sees:** the Room lives on A (authority). B's MC shows the import status and, after activation, the
  canonical Room through an existing read path for remote rooms if core offers one; otherwise P3b.2 shows status
  only and this is recorded as a limitation, never solved with a second `groups.create`. The e2e verifies A's
  canonical log and B's target run receipt (as Gate A does), not two equivalent Room views.

## 5. Two-instance e2e (P3b.3c)

`spikes/invite-rooms/gate_p3b_local.py`, scratch homes under the approved root: two `HERMES_HOME`s with
distinct identities and `identity_dir`s, host `hermes serve --isolated` + target gateway API server (Gate A
launch shapes, local oMLX), two sidecars on distinct loopback ports. Flow: pair (three codes, both SAS, acks) →
lobby → invite → preview/metadata/redeem → host approve → scope confirm → consent → activate → native readback
→ one `@peer` turn → exact event IDs/order and one target receipt. Negatives: tampered code, wrong SAS, grinding
attempt (substituted accept with matching public fields), expired offer, replayed accept/reveal, crash between
journal and projection, catalog drift after pairing, unpair with an open channel, mint callback concurrent with
activation, conflicting receipt, zero native effects before every decision. Browser QA on both sidecars after
the headless run.

## 6. Packets

0. **Design freeze** — re-review of this document (P3b-R1).
1. **P3b.0a** registry schema: drop static grant (no production compatibility flag; legacy fixtures only in
   tests/spikes), consent key + CA + endpoint required, directional host binding, journal-backed projection
   check; keep every negative listed in the P3b-R0 test table.
2. **P3b.0b** host receipt store and durable loader (fake channel in unit tests, no listener). Implemented
   together with P3b.0a in one packet, so no intermediate state accepts scoped material without either the
   static-grant check or the signed-envelope check; the journal-backed projection check moves to P3b.1a.
3. **P3b.1a** pairing codec + journal + projector + identity init + HTTP routes.
4. **P3b.1b** pairing UI.
5. **P3b.3a** control transport + dispatcher; **P3b.3b** data adapter + firewall.
6. **P3b.2** joining config, routes, read-model, UI.
7. **P3b.3c** wiring + two-instance harness + browser QA.

Each packet: RED → GREEN → independent review (`gpt-6.1-sol`), commit after Albi's approval, normal pushes to
PR #127. Core read-only, no VPS, `joining.py` never loosened.

## 7. Traceability to P3b-R0

| Finding | Resolution |
| --- | --- |
| 01 SAS circular | Three codes; SAS computable by both only after `reveal`; typed-back after that. §1 |
| 02 SAS grinding | Commit-then-reveal of `r_A`; explicit claim/non-claim. §1 |
| 03 handshake ≠ complete | Pins from transcript; signed `pair_ack` + ack-of-ack over the channel. §1 |
| 04 resolver/first material | Durable receipt store, exact mapping, signature-first admission, durable loader. §2 |
| 05 hosts.json data | Catalog from both sides; two directional bindings; stored `endpoint_id`. §1 |
| 06 joining scope | metadata, read-model, explicit `confirm_scope`, per-host origin, one-host MVP. §3 |
| 07 data plane | Dispatcher, concurrency, data adapter split; B view limitation stated. §4 |
| 08 atomic decision | Journal as source of truth, projector with locked read-modify-write, reconciliation. §1 |
| 09 identity | Explicit per-profile `identity_dir`, locked staged init, refusal of bad files. §1 |
| 10 canonicalisation | Strict decoder, domain separation, exact HKDF/hash/limits. §1 |
| 11 unpair caching | Generation re-check on every op, channel close, tombstoned endpoint. §1 |

## 8. Binding amendments from P3b-R1 (ACCEPT_WITH_CHANGES)

These are normative and override any looser wording above. Each is a gate of the named packet.

- **R1-01 attempts (P3b.1a/1b).** At most one pending pairing attempt per installation/profile; the session is
  labelled (short id shown on both sides) and selected before the SAS comparison; cancel + new attempt is
  explicit, with a persistent budget (3 failed comparisons per 24 h, then cooldown) and an anti-retry warning.
  `r_A`/`r_B` are fresh per session and never reused after reveal/abort/restart; a retried transition returns the
  same stored bytes without new randomness; a variant is a conflict.
- **R1-02 ack recovery (P3b.1a/3a).** A's `pair_ack` and B's reply are persisted bound to roles, both hashes,
  `endpoint_id` and generation; an identical duplicate on `confirmed`/`acknowledged` returns the same stored ack,
  a conflict or `unpaired` is refused. On a `confirmed` link only `pair_ack` is accepted; everything else needs
  `acknowledged`. Code expiries apply to the confirmations; once both confirmations are durable, ack completion
  and recovery use the durable record, not a new code window. Tests: loss before/after each commit, restart,
  late reply.
- **R1-03 journal/projection (P3b.0a/1a).** `protocol_state` is separate from `projection_revision` /
  `published_revision`. Lock order is always stable `flock` **then** `BEGIN IMMEDIATE`, for mutations and the
  projector alike. The snapshot is read only under both locks, published only from the journal, fsynced, and
  `published_revision` updated before release; a crash after rename reconciles from the same source. Every
  consumer compares binding, `endpoint_id`, generation, revision and required `protocol_state` exactly. Manual
  JSON edits are never trusted. Tests: two projectors, two confirmations, unpair concurrent with projection.
- **R1-04 identity bundle (P3b.1a).** `identity_dir` is a private parent; the complete bundle is built in a
  staging directory, every file and the directory fsynced, then published by one rename to
  `identity_dir/current`, parent fsynced. Readers use only `current` and validate the whole set plus the native
  binding. The lock lives outside the bundle; UID/type/no-follow and 0600/0700 checks apply to lock, stores and
  side files too. A partial final bundle is never silently repaired. Tests: crash after each write/rename, two
  concurrent initializers.
- **R1-05 admission boundary (P3b.0b/1a/3a/3b).** Every new effect (mint request, mint emission, register,
  data `POST /v1/runs`, lobby/invite/consent operations) passes a durable admission check for
  `endpoint_id`+generation serialized with unpair, taken **after** any queue/await and immediately before the
  effect. Unpair invalidates control and data routes, loaders and non-admitted jobs; re-pair never re-associates
  old receipts/imports. Effects admitted before the cutover may complete; an in-flight RPC is not physically
  cancelled. No DB transaction is held across IO. Tests: barrier before admission, after admission before
  reply, and an already open channel.
- **R1-06 adapter per scope (P3b.0b/3b).** `endpoint_id` names the pairing; the local data route uses a separate
  `adapter_id`, an opaque deterministic id derived locally from the full canonical `MaterialScope` and the
  generation: `/links/<adapter_id>`. The six-field binding keeps the pairing `endpoint_id`. The transport
  registry keeps a scope → firewall/route map without overwrite; the host receipt loader resolves the route for
  the exact scope. `_validate_adapter` and the fixed per-scope firewall stay. Tests: two lobbies/members on the
  same pairing at once, restart, unpair, no cross-routing.
- **R1-07 dispatcher (P3b.3a/3b).** MVP uses one independent mTLS connection per request/response RPC and per
  data request, each with exactly one `SequencedTLS` reader/writer; the link is a pinned logical association.
  Reserved executor capacity for completion callbacks, no link lock or DB transaction across IO, end-to-end
  deadlines, bounded overload. Distinct schemas and directions: `material_request` (A→B, only owner-verified
  `lobby_id`/`invite_id`, never native parameters) and `material_receipt` (B→A); explicit mapping
  `consent → joining_consent`. Tests: saturation with several activations and concurrent callbacks.
- **R1-08 encoding (P3b.1a).** Exact schemas for offer/accept/reveal are attached to the packet; nonces are
  base64url unpadded and must decode to exactly 32 bytes; KDF inputs are raw concatenated hashes, lowercase hex
  only inside JSON; endpoint HKDF `info = b"mc-pair-link-v1"`, `salt = b"mc-pair-link-salt-v1"`; booleans are
  distinct from integers; maximum depth 4 and per-field ranges; generic errors. Non-canonical padding, alphabet
  or segment count and signatures not exactly 64 bytes are rejected. The cap is on the whole wire string, with
  the maximum effective payload computed per kind; test vectors and the real PEM/catalog fit are a packet gate.
- **R1-09 receipt encryption (P3b.0b).** Separate host store and schema with its own private AES-256 key,
  distinct from consent/TLS keys and from the target store; serialized bootstrap; fresh 96-bit nonce per
  encryption; AAD domain-separated over the full canonical scope and pairing generation. An existing DB with a
  missing key or invalid key/ciphertext fails closed and never regenerates the key. Encrypt before INSERT (WAL).
  Tests: tamper, AAD substitution, restart, concurrent init. `JoiningStore`'s schema is unchanged.

## 9. Integration hardening before receipt-write wiring (P3b.3c-H)

Normative for the admission/receipt seam. These close the `p3b0f3-review` INFO findings R3B-02, R3B-04 and R3B-05
that had to be settled before any receipt-write callback is wired.

- **R3B-02 deadline is an admission check.** A `MaterialReceiptStore` operation validates expiry AFTER the
  process mutex, AFTER the `flock`, and before its first effect, so an already-expired request refuses even when
  both locks are free. The deadline bounds one operation only: it creates no durable claim, and expiry never
  revokes a previously committed receipt. Refusal code: `receipt_busy`.
- **R3B-04 bounded admission executor.** One process-wide `AdmissionExecutor` (owner: `peer_registry`, created
  once under a lock, shut down deterministically by `shutdown_admission_executor` + `atexit`) with `ADMISSION_WORKERS`
  threads, a bounded submission queue (`ADMISSION_QUEUE_CAP`, refusal `admission_overloaded`), and
  `ADMISSION_CALLBACK_RESERVED` worker(s) a `material` read may never occupy so receipt-write callbacks cannot
  starve. The request deadline is enforced before submission and again BEFORE the result is consumed
  (`admission_deadline_exceeded`): a material read still queued at its deadline is abandoned, a callback write is
  never abandoned mid-flight but a write finishing late is never reported as a confirmed admit.
  `resolve_scoped_material` never returns a value observed after its deadline.
- **R3B-05 write path re-reads the authoritative generation.** `admit_signed_material` keeps the exact
  signature/scope validation, then re-reads `peers.json` under the shared registry lock (so a generation cutover
  cannot interleave with the persist), requires the exact generation and six-field binding the caller validated,
  and restates it immediately before the first effect. A stale registry, a bumped on-disk generation or a missing
  authoritative source is refused with `peer_binding_changed` / `admission_unavailable` and nothing is written.
  The blocking body stays synchronous; loop callers MUST use `admit_signed_material_async`, which runs it on the
  callback lane of the admission executor (off-loop). No DB transaction is ever held across network IO.

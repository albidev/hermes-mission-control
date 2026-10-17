# Invite-only Rooms A0 preflight

This is a fail-closed behavioral harness only. It provisions two disposable Hermes homes under the designated scratch root and asks the native Hermes code for installation identity and effective RoomLink execution policy in short-lived child interpreters.

It does not start `hermes serve`, a gateway, a listener, a model, a relay, or a connector. It performs no network request.

## Gate D relay skeleton

`relay.py` is a transport-facing, in-memory routing core plus a thin websocket
handler. Operator provisioning creates a per-link connector credential; the
data path accepts only authenticated `AUTH` handshakes and exact opaque `DATA`
frames. Source identity, target/link/Room binding, connection incarnation,
stream count, frame size, aggregate queued bytes, and target availability are
checked before enqueueing. Queues are transient and bounded: the relay has no
offline replay or Room/event store.

`/health` is intentionally separate from `/data`: health reports listener
status, while data-path readiness requires an active authenticated connection.
The websocket layer can be served with an operator-provided TLS context for
WSS, but this Gate D code does not claim end-to-end confidentiality; opaque
payloads require the inner TLS/pinned-identity work in Gate E.

Run the relay behavioral tests with:

`/Users/albi/.hermes/hermes-agent/venv/bin/python -m unittest spikes.invite-rooms.tests.test_relay -v`

## Supported native contract discovered read-only

- Managed interpreter: `/Users/albi/.hermes/hermes-agent/venv/bin/python`. Every native probe invokes this exact interpreter; if it is unavailable, preflight blocks. The caller's `sys.executable` is never trusted.
- Headless server entry point: `hermes serve`, handled by `hermes_cli.subcommands.dashboard.build_serve_parser`.
- Supported runtime flags: `--isolated`, `--host`, `--port` (`0` is supported), and `--skip-build`.
- The documented launch shape for a later gate is:

  `hermes serve --isolated --host 127.0.0.1 --port 0 --skip-build`

- A0 does not execute that command. Port probing reports three independent states: `available` is a bind check, `reserved` is false because no socket is held, and `capability_ready` is false because no service is running. An open port is not readiness.
- `--port N` checks a requested lab port before any scoped home is created; an occupied port blocks. The default `--port 0` only checks OS allocation availability and does not claim capability readiness.

Native identity is resolved by the actual `hermes_cli.install_identity.get_install_id()` and `gateway.hosted_rooms.local_authority_gateway_id()` calls with each child `HERMES_HOME`; path hashes and marker-file identities are not accepted. Native policy is read back through `gateway.hosted_room_execution_policy.execution_policy_mapping()` from the scoped `config.yaml`. The explicit config selects `api_server: []`, manual approvals, one turn, and no terminal/file/web/browser/secret-capable toolset; the native result must still be exactly `bot_room`.

## Run

From the Mission Control checkout:

`/Users/albi/.hermes/hermes-agent/venv/bin/python -m unittest discover -s spikes/invite-rooms/tests -v`

The runtime root must already exist and is `/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930/preflight-homes`. The harness rejects `/Users/albi`, traversal components, sibling names that merely contain `recovery`, and symlink escapes before creating or writing a scoped home. It never mutates the caller environment; child environment restoration is tested.

The old recovery implementation and its rejected report remain untouched in scratch evidence.

## Gate A — actual groups RPC and remote member execution

Run from this checkout on **`feat/invite-rooms`**:

```sh
/Users/albi/.hermes/hermes-agent/venv/bin/python spikes/invite-rooms/gate_a_groups_runtime.py
```

The branch guard runs before any lab provisioning. The probe uses fresh preflight-approved host and target homes with distinct native installation IDs, a host `hermes serve --isolated` on loopback port 18790, and a target `hermes gateway run --external-supervisor` API server on loopback port 18791. Those ports must be free. The local oMLX endpoint `http://127.0.0.1:8083/v1` must actually serve `qwen3.8-27b-oq4e-mtp`; no production cloud credential is copied. Both effective target policies are read back and must keep the approved policy digest (`manual`, `bot_room` only, one iteration).

The real wire workflow is:

1. Authenticated target HTTP readiness and authenticated host WebSocket `groups.capabilities` with `driver=true`.
2. Target-issued native scoped grant; host `groups.create`, then `groups.peer.register`, which performs its own live native probe and exact catalog equality check. There is no separate `groups.peer.probe` method in the current contract.
3. An addressed `@peer` turn through `groups.send`; a repeated send preserves the canonical event ID and sequence.
4. A real remote model response, canonical host `message.member`/`turn.settled`, and replay preserving original event IDs/order.
5. Exact original dispatch coordinates reconstructed from the persisted native driver payload; duplicate native POST with the same `Idempotency-Key` returns the original run ID and `replayed=true`.
6. Read-only SQLite verification of exactly one matching target `run_idempotency` receipt, correlated with the host `hosted_room_remote_runs` receipt and completed target output.
7. Bounded teardown of only the owned subprocesses, with sanitized JSON evidence under the approved scratch root.

The host uses the existing Desktop SSH token-file contract solely for owner-local lab authentication: `$HOME/.hermes/desktop-ssh/<32hex>/<16hex>.token`, parent mode 0700, file mode 0600, 64 hexadecimal token characters, read/unlinked by the native launcher. Here `$HOME` is the disposable lab home. No owner token/grant is printed or sent to a remote member. This proves a **same-machine, direct native baseline**, not a cross-host relay or firewall acceptance gate.

`gate_a_runtime.py` is only the target admission/model/idempotency smoke. Its exit status refers to `admission_passed`; `gate_A_passed` stays false because it does not exercise the host groups workflow. Do not treat an admission-only report as full Gate A acceptance.

A long scratch path may produce a native `AF_UNIX path too long` watchdog warning; report it separately. Authenticated capabilities, actual reply, durable receipts and teardown are the readiness/acceptance evidence, not an open port or a heartbeat.

## Gate C — stable loopback adapter

`adapter.py` provides the external, per-link compatibility boundary without changing
the native catalog or client. `LoopbackAdapter.start()` accepts only `127.0.0.1` or
`::1`; every request must include an owner-registered `/links/<opaque-id>/` prefix.
Unknown links return 404 before the fixed backend is called. The adapter strips
transport headers and delegates the exact native capabilities/runs contract to
`room_firewall.py`; it never exposes Mission Control or a request-selected upstream.

`state.py` persists link ID, loopback target URL, and the complete owner scope with an
atomic replace. Re-registering a link after adapter restart must match the persisted
binding exactly; conflicting bindings fail closed. The native scope uses the actual
fields and types (`home_install_id`, `authority_gateway_id`, `target_install_id`, and
positive integer `authority_epoch`) from `HostedMemberDispatch`, not the earlier
invented firewall coordinates.

The adapter tests exercise IPv4, persistence/restart, unknown-link pre-dial rejection,
admin-route denial, and the unchanged native catalog payload. IPv6 is exercised when
the host kernel exposes an IPv6 loopback socket. The disposable runtime harness
`gate_c_runtime.py` starts only isolated native host/target processes, binds a real
owner-fixed firewall route, and runs the actual `groups.peer.register` path through
the adapter; its report records native probe/catalog equality, canonical remote
execution, duplicate event preservation, durable idempotency, and teardown. It does
does not modify core or production profiles/services.

## Gate E — inner mTLS and native identity pinning

`tls_stream.py` uses the Python standard-library `ssl` implementation with mandatory
TLS 1.3, mutual certificate verification, and manually approved SHA-256 peer
fingerprints. The relay only transports the resulting opaque bytes; it has no peer
private keys and cannot terminate this inner stream. `SequencedTLS` adds bounded,
length-delimited application frames and rejects malformed, replayed, or out-of-order
frames without inventing cryptography.

`pairing.py` binds the approved certificate fingerprint to the actual native
installation identity and profile returned by an independent local probe. A display
label is not part of the binding. Certificate rotation, wrong installation, and wrong
profile fail closed; there is no unverified identity fallback.

Behavioral coverage is in `tests/test_peer_identity.py`: real socket-pair mTLS
handshakes, wrong pinned peer, native identity/profile mismatch, replay rejection,
and a real TLS-record forwarding path through an opaque relay. The relay test sends
actual framed grant/prompt/output sentinels, verifies the authenticated peer receives
the original payload, verifies the relay-observed DATA contains none of those
sentinels, and exercises the relay's no-private-key/decryption failure boundary.
The tests use disposable OpenSSL-generated certificates only; no runtime keys or
credentials are stored in the repository.

## Gate F — local end-to-end relay composition

`gate_f_runtime.py` composes the real Gate C native workflow with two disposable
connectors and an opaque byte-forwarding relay.  Connector A is the adapter's
owner-side transport, Connector B terminates pinned mutual TLS and forwards the
allowlisted native request to the second isolated gateway.  The relay sees only
TLS records; it has no peer keys or native credentials.  The native host still
performs the actual capability probe, `groups.peer.register`, addressed
text-only turn, status/output read, stop, canonical log replay, and duplicate
idempotency check.

Run it with:

`PYTHONPATH=spikes/invite-rooms /Users/albi/.hermes/hermes-agent/venv/bin/python spikes/invite-rooms/gate_f_runtime.py`

The command starts two isolated native gateways and the local model-backed turn,
then tears down both gateways and deletes the temporary certificate directory.
The sanitized report is written under
`/Users/albi/.hermes/cache/scratch/room-network-spike-20261011-164930/`.
This is a local lab proof only: it does not claim two hosts, two networks,
inbound firewall behavior, or public WSS deployment.

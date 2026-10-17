# Gate B room-only firewall

`room_firewall.py` is the connector-side policy boundary for the network spike. It is transport-agnostic and accepts a parsed inner request plus an owner-configured backend callable; it never discovers or accepts an upstream from request data.

The policy is fail-closed:

- only `GET`/`POST` and the exact RoomLink capability/run/status/stop paths are accepted;
- only `HermesRoom` authorization is accepted; Bearer, cookies, hop-by-hop, forwarded, duplicate, and unknown headers are denied;
- request targets reject queries/fragments, encoded or traversal paths, ambiguous slashes, userinfo-style absolute URLs, CONNECT, and upgrades;
- dispatch bodies have exact fields, a complete validated owner-configured binding for all six coordinates, bounded text input, JSON content type, and an unchanged idempotency key;
- run status/stop calls invoke the final native verifier for the same run and scope;
- redirects, oversized/non-JSON responses, unallowlisted response headers, deeply nested/oversized response fields, and approval/tool/trace/session/config/history payloads never cross the boundary;
- the fixed backend has an owner-configured deadline (the daemon guard stops waiting, while the backend itself must provide cancellation/HTTP timeouts because Python callables cannot be forcibly cancelled).

Native `POST /v1/room-members/grants/refresh` accepts only a bounded `ttl_seconds`;
`/grants/revoke` and run stop accept literal `{}`. The final native verifier owns
scope, expiry, horizon and revocation. Revoking a grant does **not** stop an admitted run.
Post-backend failures (including malformed return values or withheld responses) are
ambiguous 5xx, never a 403 claiming non-admission. The credentialed HTTP backend disables
redirects before fetching any new origin, even if that origin is another loopback port.

Run the behavioral suite from the repository root:

`/Users/albi/.hermes/hermes-agent/venv/bin/python -m unittest discover -s spikes/invite-rooms/tests -v`

Local native interoperability has been exercised by `gate_f1_local.py`, `gate_g_native.py`,
`gate_h_native.py`, and the product-local `gate_p2_p4_local.py` (isolated homes, actual model).
Their individual reports prove only their named checks, not the complete G/H acceptance
matrix. H6 expiry was direct target-loopback, and G's reconnect prestate was queued, not
proven running. Cross-host F1 remains blocked until real hosts on distinct networks run it.
Unit-injected backends prove behavior only; they are not native interoperability evidence.

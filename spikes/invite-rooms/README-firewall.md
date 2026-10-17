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

The network-spike source plan also names native `POST /v1/room-members/grants/refresh`
and `/grants/revoke`. Gate B deliberately does not forward either path: the installed
native grant payload/verifier contract is unavailable in the supported lab (Gate A's
blocker), so accepting an invented body would weaken rather than prove the unchanged
scope/horizon/revocation checks. They remain explicit deny-by-default cases until a
native contract is available; this is a documented Gate B deviation, not an omitted
requirement.

Run the behavioral suite from the repository root:

`/Users/albi/.hermes/hermes-agent/venv/bin/python -m unittest discover -s spikes/invite-rooms/tests -v`

This Gate B implementation is not a claim of native end-to-end interoperability. Gate A's parent evidence records that the supported isolated lab currently lacks the native peer/register adapter and model authentication needed for a real dispatch; these tests therefore exercise the firewall's real request/response behavior and final-verifier hook without inventing a native run. The positive dispatch test uses only an injected behavioral backend and is not native acceptance.

import json
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from room_firewall import FirewallDenied, Request, RoomFirewall, UpstreamResponse


SCOPE = {
    "room_id": "room-1",
    "home_install_id": "home-1",
    "authority_gateway_id": "gateway-a",
    "authority_epoch": 7,
    "member_id": "member-2",
    "target_install_id": "install-b",
    "target_profile": "default",
}


def dispatch_body(**extra):
    payload = {"input": "hello", "hosted_room_dispatch": {
        "protocol_version": 1, **SCOPE, "task_id": "task-1", "execution_generation": 1,
        "source_event_seq": 2, "cancellation_scope_id": "cancel-1", "prompt": "hello",
        "prompt_digest": "a" * 64, "capability_digest": "b" * 64,
        "execution_policy_digest": "c" * 64, "trace_id": "trace-1",
    }}
    payload["hosted_room_dispatch"].update(extra)
    return json.dumps(payload).encode()


class FirewallBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.valid_runs = {"run-1"}

        def backend(request):
            self.calls.append(request)
            if request.path == "/v1/runs":
                return UpstreamResponse(200, body=json.dumps({
                    "run_id": "run-1", "task_id": "task-1", "execution_generation": 3,
                    "status": "accepted", "output": "ok",
                }).encode())
            return UpstreamResponse(200, body=json.dumps({"status": "ok"}).encode())

        self.firewall = RoomFirewall(
            expected_scope=SCOPE,
            backend=backend,
            verify_native=lambda run_id, scope: run_id in self.valid_runs and scope == SCOPE,
        )

    def req(self, method, target, body=b"", headers=None):
        base = [("Authorization", "HermesRoom grant"), ("Accept", "application/json")]
        base.extend(headers or [])
        return Request(method, target, tuple(base), body)

    def assertDenied(self, request):
        with self.assertRaises(FirewallDenied):
            self.firewall.forward(request)
        self.assertEqual(self.calls, [])

    def test_positive_native_dispatch_preserves_identity_and_scope(self):
        result = self.firewall.forward(self.req(
            "POST", "/v1/runs", dispatch_body(),
            [("Content-Type", "application/json"), ("Idempotency-Key", "idem-123")],
        ))
        self.assertEqual(result.status, 200)
        self.assertEqual(json.loads(result.body), {
            "run_id": "run-1", "task_id": "task-1", "execution_generation": 3,
            "status": "accepted", "output": "ok",
        })
        self.assertEqual(self.calls[0].path, "/v1/runs")
        self.assertIn(("idempotency-key", "idem-123"), self.calls[0].headers)
        self.assertNotIn("approval", result.body.decode())

    def test_wrong_scope_and_unrelated_run_fail_at_native_verifier(self):
        self.assertDenied(self.req("GET", "/v1/runs/run-unknown"))
        self.valid_runs.clear()
        self.assertDenied(self.req("POST", "/v1/runs/run-1/stop", headers=[("Content-Type", "application/json")]))

    def test_native_lifecycle_calls_pass_with_their_exact_wire_bodies(self):
        # Bodies exactly as tui_gateway/hosted_room_peer_http.py serializes them.
        json_ct = [("Content-Type", "application/json")]
        for target, body in (("/v1/runs/run-1/stop", b"{}"),
                             ("/v1/room-members/grants/revoke", b"{}"),
                             ("/v1/room-members/grants/refresh", b'{"ttl_seconds":86400}'),
                             ("/v1/room-members/grants/refresh", b'{"ttl_seconds":3600.0}')):
            self.assertEqual(self.firewall.forward(self.req("POST", target, body, json_ct)).status, 200, target)
        self.assertEqual([c.path for c in self.calls], ["/v1/runs/run-1/stop", "/v1/room-members/grants/revoke",
                                                         "/v1/room-members/grants/refresh",
                                                         "/v1/room-members/grants/refresh"])

    def test_queued_run_status_with_null_generation_is_forwarded(self):
        # Native projection of a queued run carries execution_generation=null (gate G3, 17 Oct).
        native = {"object": "hermes.run", "run_id": "run_1", "status": "queued", "execution_generation": None,
                  "output": "", "usage": {}, "pending_controls": [], "admission_id": "a" * 32}
        firewall = RoomFirewall(expected_scope=SCOPE, verify_native=lambda *_: True,
                                backend=lambda request: UpstreamResponse(200, body=json.dumps(native).encode()))
        body = json.loads(firewall.forward(self.req("GET", "/v1/runs/run_1")).body)
        self.assertEqual(body["status"], "queued")
        self.assertIsNone(body["execution_generation"])
        self.assertNotIn("admission_id", body)

    def test_dispatch_response_keeps_native_replayed_flag(self):
        # tui_gateway/hosted_room_peer_http.py reads result["replayed"] on dispatch.
        native = {"run_id": "run_1", "status": "started", "replayed": True}
        firewall = RoomFirewall(expected_scope=SCOPE, verify_native=lambda *_: True,
                                backend=lambda request: UpstreamResponse(202, body=json.dumps(native).encode()))
        body = {"input": "p", "hosted_room_dispatch": dict(SCOPE, protocol_version=2, task_id="t", execution_generation=1,
                source_event_seq=1, cancellation_scope_id="c", prompt="p", prompt_digest="d" * 64,
                capability_digest="d" * 64, execution_policy_digest="p" * 64, trace_id="tr")}
        response = firewall.forward(self.req("POST", "/v1/runs", json.dumps(body).encode(),
                                             [("Content-Type", "application/json"), ("Idempotency-Key", "room:t:1")]))
        self.assertIs(json.loads(response.body)["replayed"], True)

    def test_refresh_response_keeps_replacement_grant_only_on_its_route(self):
        native = {"object": "hermes.room_member.grant", "grant": "g" * 900, "expires_at": 1.0,
                  "status_expires_at": 2.0, "execution_policy": {"policy_digest": "c" * 64}}
        firewall = RoomFirewall(expected_scope=SCOPE, verify_native=lambda *_: True,
                                backend=lambda request: UpstreamResponse(200, body=json.dumps(native).encode()))
        json_ct = [("Content-Type", "application/json")]
        refreshed = json.loads(firewall.forward(self.req(
            "POST", "/v1/room-members/grants/refresh", b'{"ttl_seconds":3600}', json_ct)).body)
        self.assertEqual(refreshed["grant"], "g" * 900)
        self.assertEqual((refreshed["expires_at"], refreshed["status_expires_at"]), (1.0, 2.0))
        # The same field from any other route is dropped: no grant smuggling via run status.
        status = json.loads(firewall.forward(self.req("GET", "/v1/room-members/capabilities")).body)
        self.assertNotIn("grant", status)

    def test_native_error_codes_survive_but_messages_do_not(self):
        def failing(request):
            return UpstreamResponse(403, body=json.dumps({"error": {
                "message": "grant grant-abc for room-1 revoked by owner at /Users/x", "type": "gateway_auth_error",
                "code": "room_reauthorization_required"}}).encode())
        firewall = RoomFirewall(expected_scope=SCOPE, backend=failing, verify_native=lambda *_: True)
        result = firewall.forward(self.req("GET", "/v1/room-members/capabilities"))
        self.assertEqual(result.status, 403)
        self.assertEqual(json.loads(result.body)["error"]["code"], "room_reauthorization_required")
        self.assertNotIn(b"grant-abc", result.body)
        self.assertNotIn(b"/Users", result.body)

        def unknown(request):
            return UpstreamResponse(400, body=b'{"error":{"code":"secret_internal_detail","message":"x"}}')
        firewall = RoomFirewall(expected_scope=SCOPE, backend=unknown, verify_native=lambda *_: True)
        body = json.loads(firewall.forward(self.req("GET", "/v1/room-members/capabilities")).body)
        self.assertIsNone(body["error"]["code"])

    def test_admin_auth_and_sensitive_routes_are_denied(self):
        for target in ("/approval", "/v1/runs/run-1/approval", "/steer", "/events", "/traces",
                       "/v1/room-members/invites", "/v1/sessions", "/v1/config", "/v1/history", "/rpc", "/chat"):
            self.assertDenied(self.req("GET", target))
        json_ct = [("Content-Type", "application/json")]
        for body in (b'{"ttl_seconds":86401}', b'{"ttl_seconds":0}', b'{"ttl_seconds":true}',
                     b'{"ttl_seconds":60,"permissions":["dispatch"]}', b'{"grant":"x"}'):
            self.assertDenied(self.req("POST", "/v1/room-members/grants/refresh", body, json_ct))
        for body in (b'{"grant_id":"other"}', b'{ }', b'[]'):
            self.assertDenied(self.req("POST", "/v1/room-members/grants/revoke", body, json_ct))
        self.assertDenied(self.req("POST", "/v1/runs/run-1/stop", b'{"force":true}', json_ct))
        self.assertDenied(Request("GET", "/v1/room-members/capabilities", (("Authorization", "Bearer admin"),)))
        self.assertDenied(Request("GET", "/v1/room-members/capabilities", (("Authorization", "HermesRoom grant"), ("Cookie", "session=secret"))))

    def test_path_method_header_and_transport_ambiguity_is_denied(self):
        bad = [
            self.req("PUT", "/v1/runs/run-1"), self.req("CONNECT", "/v1/runs/run-1"),
            self.req("GET", "/v1//room-members/capabilities"), self.req("GET", "/v1/runs/../config"),
            self.req("GET", "/v1/runs/%2e%2e/config"), self.req("GET", "http://127.0.0.1/v1/runs/run-1"),
            self.req("GET", "/v1/room-members/capabilities", headers=[("Upgrade", "websocket")]),
            self.req("GET", "/v1/room-members/capabilities", headers=[("Host", "evil")]),
            self.req("GET", "/v1/room-members/capabilities", headers=[("Authorization", "HermesRoom grant"), ("Authorization", "HermesRoom other")]),
        ]
        for request in bad:
            with self.subTest(request=request):
                self.assertDenied(request)

    def test_dispatch_requires_exact_bounded_body_and_fixed_scope(self):
        cases = [
            json.dumps({"input": "x", "hosted_room_dispatch": SCOPE, "profile": "evil"}).encode(),
            dispatch_body(room_id="other"),
            json.dumps({"input": {"prompt": "x"}, "hosted_room_dispatch": SCOPE}).encode(),
            b"not-json",
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertDenied(self.req("POST", "/v1/runs", body, [("Content-Type", "application/json"), ("Idempotency-Key", "i")]))

    def test_owner_scope_must_bind_all_coordinates_before_any_forwarding(self):
        for incomplete in ({}, {"room_id": SCOPE["room_id"]}, {**SCOPE, "room_id": ""}):
            with self.subTest(scope=incomplete):
                with self.assertRaises(ValueError):
                    RoomFirewall(
                        expected_scope=incomplete,
                        backend=lambda _: self.fail("incomplete owner scope reached backend"),
                        verify_native=lambda *_: True,
                    )

        partial = dict(SCOPE)
        partial.pop("target_profile")
        with self.assertRaises(ValueError):
            RoomFirewall(
                expected_scope=partial,
                backend=lambda _: self.fail("partial owner scope reached backend"),
                verify_native=lambda *_: True,
            )

    def test_oversized_body_and_response_redirects_are_denied(self):
        self.assertDenied(self.req("POST", "/v1/runs", dispatch_body(input="x" * (256 * 1024 + 1)), [("Content-Type", "application/json"), ("Idempotency-Key", "i")]))

        def redirect(_):
            return UpstreamResponse(302, (("Location", "https://evil"),), b"redirect")
        firewall = RoomFirewall(expected_scope=SCOPE, backend=redirect, verify_native=lambda *_: True)
        with self.assertRaises(FirewallDenied):
            firewall.forward(self.req("GET", "/v1/room-members/capabilities"))

    def test_native_approval_payload_is_never_forwarded_even_when_grant_has_approve(self):
        self.assertDenied(self.req("POST", "/v1/runs/run-1/approval", b"{}", [("Content-Type", "application/json")]))

    def test_backend_deadline_fails_closed_without_waiting_for_runaway_callable(self):
        def hanging(_):
            time.sleep(1)
            return UpstreamResponse(200, body=b'{"status":"late"}')

        firewall = RoomFirewall(
            expected_scope=SCOPE, backend=hanging,
            verify_native=lambda *_: True, backend_timeout_seconds=0.01,
        )
        started = time.monotonic()
        with self.assertRaises(FirewallDenied):
            firewall.forward(self.req("GET", "/v1/room-members/capabilities"))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_response_headers_and_fields_are_bounded_and_allowlisted(self):
        def response_with_headers(_):
            return UpstreamResponse(200, (("X-Leak", "no"),), b'{"status":"ok"}')

        firewall = RoomFirewall(expected_scope=SCOPE, backend=response_with_headers, verify_native=lambda *_: True)
        with self.assertRaises(FirewallDenied):
            firewall.forward(self.req("GET", "/v1/room-members/capabilities"))

        cases = [
            {"status": "ok", "output": "x" * (64 * 1024 + 1)},
            {"status": "ok", "output": {"a": {"b": {"c": {"d": {"e": "too deep"}}}}}},
            {"status": "ok", "output": {str(i): i for i in range(33)}},
            {"status": "ok", "execution_generation": True},
            {"status": "ok", "output": {"tool": {"name": "secret"}}},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:80]):
                bounded = RoomFirewall(
                    expected_scope=SCOPE,
                    backend=lambda _, payload=payload: UpstreamResponse(200, body=json.dumps(payload).encode()),
                    verify_native=lambda *_: True,
                )
                with self.assertRaises(FirewallDenied):
                    bounded.forward(self.req("GET", "/v1/room-members/capabilities"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

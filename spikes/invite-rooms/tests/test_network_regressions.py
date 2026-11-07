"""Regressions reproduced by independent R1 review; no native/private credentials."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connector import native_http_backend, TargetConnector
from room_firewall import ForwardedRequest, Request, RoomFirewall
from relay import RelayError
from relay_daemon import RelayDaemon, credential_digest


SCOPE = dict(room_id="test-room", home_install_id="install:host", authority_gateway_id="install:host",
             authority_epoch=1, member_id="peer", target_install_id="install:target", target_profile="default")


class NetworkRegressionTest(unittest.TestCase):
    def test_credentialed_backend_never_follows_redirect_even_to_another_loopback_port(self):
        seen = []

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            def log_message(self, *_):
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        self.addCleanup(sink.server_close)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()
        self.addCleanup(sink.shutdown)

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/capture")
                self.end_headers()
            def log_message(self, *_):
                pass

        origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        self.addCleanup(origin.server_close)
        threading.Thread(target=origin.serve_forever, daemon=True).start()
        self.addCleanup(origin.shutdown)
        response = native_http_backend(f"http://127.0.0.1:{origin.server_port}")(
            ForwardedRequest("GET", "/v1/room-members/capabilities", (("authorization", "HermesRoom synthetic-only"),), b""))
        self.assertEqual(response.status, 302)
        self.assertEqual(seen, [], "not even a same-host credentialed redirect is permitted")

    def test_invalid_backend_return_after_side_effect_is_ambiguous_not_403(self):
        admitted = []
        def backend(request):
            admitted.append(request)
            return None
        firewall = RoomFirewall(expected_scope=SCOPE, backend=backend, verify_native=lambda *_: True)
        target = TargetConnector.__new__(TargetConnector)
        target.firewall = firewall
        dispatch = SCOPE | dict(protocol_version=2, task_id="task", execution_generation=1, source_event_seq=1,
                                cancellation_scope_id="cancel", prompt="hi", prompt_digest="d"*64,
                                capability_digest="c"*64, execution_policy_digest="p"*64, trace_id="trace")
        response = target.respond(Request("POST", "/v1/runs", (("authorization", "HermesRoom synthetic-only"),
                                  ("content-type", "application/json"), ("idempotency-key", "room:task:1")),
                                  json.dumps({"input": "hi", "hosted_room_dispatch": dispatch}).encode()))
        self.assertEqual(len(admitted), 1)
        self.assertEqual(response.status, 502)

    def test_destructive_reload_is_rejected_honestly_without_partial_mutation(self):
        with tempfile.TemporaryDirectory(prefix="relay-reload-") as tmp:
            path = Path(tmp) / "routes.json"
            entry = dict(source="a", target="b", room_id="r", link_id="l",
                         credential_sha256={"a": credential_digest("fake-a"), "b": credential_digest("fake-b")})
            path.write_text(json.dumps([entry]))
            daemon = RelayDaemon(path, host="127.0.0.1", port=0, tls=None)
            path.write_text("[]")
            with self.assertRaisesRegex((ValueError, RelayError), "restart"):
                daemon.reload()
            changed = entry | {"credential_sha256": {"a": credential_digest("replacement"), "b": credential_digest("fake-b")}}
            path.write_text(json.dumps([changed]))
            with self.assertRaisesRegex((ValueError, RelayError), "restart"):
                daemon.reload()
            path.write_text(json.dumps([entry]))
            self.assertEqual(daemon.reload(), 1)


if __name__ == "__main__":
    unittest.main()

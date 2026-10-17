import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from adapter import LoopbackAdapter
from room_firewall import Request, RoomFirewall, UpstreamResponse
from state import FixedLinkStore
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPClient


SCOPE = {
    "room_id": "room-a", "home_install_id": "install-a", "authority_gateway_id": "gateway-a",
    "authority_epoch": 1, "member_id": "member-b", "target_install_id": "install-b",
    "target_profile": "default",
}


def firewall(calls):
    def backend(request):
        calls.append(request)
        if request.path == "/v1/room-members/capabilities":
            return UpstreamResponse(200, body=json.dumps({
                "installation_id": "install-b", "protocol_versions": [1], "link_modes": ["direct"],
                "persistent_process": True, "text": True, "attachments": False,
                "execution_policy": {"policy_digest": "p" * 64}, "catalog_digest": "d" * 64,
            }).encode())
        return UpstreamResponse(200, body=json.dumps({"status": "ok"}).encode())
    return RoomFirewall(expected_scope=SCOPE, backend=backend, verify_native=lambda *_: True)


class LoopbackAdapterTests(unittest.TestCase):
    def request(self, url, method="GET", body=b"", headers=None):
        req = urllib.request.Request(url, data=body or None, method=method, headers={
            "Authorization": "HermesRoom " + "g" * 32, "Accept": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_ipv4_fixed_link_probe_and_catalog_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            adapter = LoopbackAdapter(state=FixedLinkStore(Path(tmp) / "links.json"))
            adapter.start(host="127.0.0.1")
            try:
                url = adapter.bind("opaque-link", firewall(calls), target_url="http://127.0.0.1:9999", scope=SCOPE)
                status, catalog = self.request(url + "/v1/room-members/capabilities")
                self.assertEqual(status, 200)
                self.assertEqual(catalog["installation_id"], "install-b")
                self.assertEqual(catalog["catalog_digest"], "d" * 64)
                self.assertEqual(len(calls), 1)
                native = PeerRunsHTTPClient(base_url=url, api_key="k" * 32)
                self.assertEqual(native.probe(grant="g" * 32), catalog)
            finally:
                adapter.close()

    def test_unknown_link_is_rejected_before_backend_dial_and_admin_is_denied(self):
        calls = []
        adapter = LoopbackAdapter()
        adapter.start(host="127.0.0.1")
        try:
            url = adapter.bind("known", firewall(calls), target_url="http://127.0.0.1:9999", scope=SCOPE)
            with self.assertRaises(urllib.error.HTTPError) as unknown:
                self.request(url.replace("/known", "/unknown") + "/v1/room-members/capabilities")
            self.assertEqual(unknown.exception.code, 404)
            self.assertEqual(calls, [])
            with self.assertRaises(urllib.error.HTTPError) as admin:
                self.request(url + "/v1/config")
            self.assertEqual(admin.exception.code, 403)
            self.assertEqual(calls, [])
        finally:
            adapter.close()

    def test_transport_loss_after_forwarding_stays_ambiguous_for_native_client(self):
        """Lost reply after admission must not look like a native 4xx non-admission."""
        from tui_gateway.hosted_room_peer_http import PeerRunsHTTPError
        admitted = []

        def admits_then_loses_reply(request):
            admitted.append(request.headers)
            raise ConnectionResetError("relay stream closed after admission")

        dispatch = {"protocol_version": 2, **SCOPE, "task_id": "t1", "execution_generation": 1,
                    "source_event_seq": 1, "cancellation_scope_id": "c", "prompt": "p", "prompt_digest": "d" * 64,
                    "capability_digest": "d" * 64, "execution_policy_digest": "p" * 64, "trace_id": "tr"}
        adapter = LoopbackAdapter()
        adapter.start(host="127.0.0.1")
        try:
            url = adapter.bind("lossy", RoomFirewall(expected_scope=SCOPE, backend=admits_then_loses_reply,
                                                     verify_native=lambda *_: True),
                              target_url="http://127.0.0.1:9999", scope=SCOPE)
            native = PeerRunsHTTPClient(base_url=url, api_key="k" * 32)
            with self.assertRaises(PeerRunsHTTPError) as caught:
                native._request("/v1/runs", method="POST", body={"input": "p", "hosted_room_dispatch": dispatch},
                                headers={"Idempotency-Key": "room:t1:1"}, room_grant="g" * 32)
            self.assertEqual(len(admitted), 1)
            self.assertEqual(caught.exception.status_code, 502)
            self.assertTrue(caught.exception.ambiguous)
            self.assertFalse(caught.exception.not_admitted)
            self.assertTrue(caught.exception.retryable)
        finally:
            adapter.close()

    def test_non_loopback_binding_is_rejected(self):
        with self.assertRaises(ValueError):
            LoopbackAdapter().start(host="0.0.0.0")

    def test_ipv6_loopback_is_supported_when_available(self):
        adapter = LoopbackAdapter()
        try:
            adapter.start(host="::1")
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {exc}")
        try:
            calls = []
            url = adapter.bind("v6", firewall(calls), target_url="http://[::1]:9999", scope=SCOPE)
            status, _ = self.request(url + "/v1/room-members/capabilities")
            self.assertEqual(status, 200)
            self.assertEqual(len(calls), 1)
        finally:
            adapter.close()

    def test_persisted_binding_survives_adapter_restart_with_same_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FixedLinkStore(Path(tmp) / "links.json")
            first = LoopbackAdapter(state=store)
            first.start(host="127.0.0.1", port=0)
            try:
                old_url = first.bind("stable", firewall([]), target_url="http://127.0.0.1:7777", scope=SCOPE)
                self.assertEqual(store.get("stable")["target_url"], "http://127.0.0.1:7777")
            finally:
                first.close()
            second = LoopbackAdapter(state=store)
            second.start(host="127.0.0.1", port=int(old_url.rsplit(":", 1)[1].split("/", 1)[0]))
            try:
                restored = second.restore(lambda record: firewall([]))
                self.assertEqual(restored, ("stable",))
                status, _ = self.request(old_url + "/v1/room-members/capabilities")
                self.assertEqual(status, 200)
            finally:
                second.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from invite_rooms.control import LoopbackAdapter, UpstreamResponse
from room_lobby_service import _validate_adapter

SCOPE = {"room_id":"r", "home_install_id":"h", "authority_gateway_id":"g",
         "authority_epoch":1, "member_id":"m", "target_install_id":"t", "target_profile":"default"}

class AdapterTests(unittest.TestCase):
    def test_product_url_and_pinned_connector(self):
        calls = []
        def connector(request, admit):
            admit(); calls.append(request)
            return UpstreamResponse(200, body=b'{"installation_id":"t","catalog":{}}')
        adapter = LoopbackAdapter()
        adapter.start()
        try:
            url = adapter.register_link("endpoint_a", 1, expected_scope=SCOPE,
                verify_native=lambda *_: True, connector=connector)
            _validate_adapter(url)
            req = urllib.request.Request(url + "/v1/room-members/capabilities", method="GET",
                headers={"Authorization":"HermesRoom " + "x" * 32, "Accept":"application/json"})
            with urllib.request.urlopen(req, timeout=2) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(len(calls), 1)
        finally:
            adapter.close()

    def test_generation_cutover_closes_route(self):
        adapter = LoopbackAdapter()
        adapter.start()
        try:
            url = adapter.register_link("endpoint_a", 1, expected_scope=SCOPE,
                verify_native=lambda *_: True,
                connector=lambda request, admit: UpstreamResponse(200, body=b'{"status":"ok"}'))
            adapter.observe_generation("endpoint_a", 2)
            req = urllib.request.Request(url + "/v1/room-members/capabilities", method="GET",
                headers={"Authorization":"HermesRoom " + "x" * 32})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=2)
            self.assertEqual(caught.exception.code, 404)
        finally:
            adapter.close()

    def test_non_loopback_rejected(self):
        with self.assertRaises(ValueError):
            LoopbackAdapter().start(host="0.0.0.0")

if __name__ == "__main__":
    unittest.main()

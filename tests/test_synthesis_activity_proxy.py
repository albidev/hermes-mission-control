import json

from synthesis_activity_proxy import load_synthesis_activity, revert_synthesis


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_load_activity_forwards_vault_scope(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append((request.full_url, request.method, timeout))
        return FakeResponse({"vault_id": "core", "activities": [], "count": 0})

    monkeypatch.setattr("synthesis_activity_proxy.urllib.request.urlopen", fake_urlopen)
    result = load_synthesis_activity("crossnection")

    assert result["vault_id"] == "core"
    assert seen == [("http://127.0.0.1:8643/api/synthesis-activity?vault_id=crossnection", "GET", 8)]


def test_revert_refreshes_graph_after_success(monkeypatch):
    calls = []

    def fake_request(path, *, method="GET", payload=None):
        calls.append((path, method, payload))
        if path == "/api/synthesis/revert":
            return {"status": "reverted", "refresh_required": True}
        return {"status": "refreshed"}

    monkeypatch.setattr("synthesis_activity_proxy._request", fake_request)
    result = revert_synthesis("op-1", "core")

    assert result["status"] == "reverted"
    assert result["graph_refresh"] == {"status": "refreshed"}
    assert calls == [
        ("/api/synthesis/revert", "POST", {"operation_id": "op-1", "vault_id": "core"}),
        ("/api/refresh-graph", "POST", {"vault_id": "core"}),
    ]

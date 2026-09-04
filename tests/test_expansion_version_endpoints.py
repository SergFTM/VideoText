"""HTTP surface for the version selector in the artifacts UI."""
from fastapi.testclient import TestClient


def test_versions_route_lists_newest_first(monkeypatch):
    import server

    class Row:
        def __init__(self, version, model, chars):
            self.version, self.model, self.chars = version, model, chars
            self.status, self.verdict, self.elapsedMs = "done", None, 100
            self.contentMd = "x" * chars
            class _D:
                def isoformat(self): return "2026-09-04T00:00:00"
            self.createdAt = _D()

    monkeypatch.setattr(server, "list_expansion_versions",
                        lambda v, m: [Row(2, "claude-opus-5", 20), Row(1, "claude-sonnet-4-6", 10)])
    client = TestClient(server.app)
    r = client.get("/videos/vid1/expansions/research/versions")
    assert r.status_code == 200
    body = r.json()
    assert [v["version"] for v in body["versions"]] == [2, 1]
    assert body["versions"][0]["model"] == "claude-opus-5"
    assert body["versions"][0]["chars"] == 20


def test_versions_route_404_when_empty(monkeypatch):
    import server
    monkeypatch.setattr(server, "list_expansion_versions", lambda v, m: [])
    client = TestClient(server.app)
    assert client.get("/videos/vid1/expansions/research/versions").status_code == 404

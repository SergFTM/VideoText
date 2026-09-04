"""The pipeline's single hard stop: no ТЗ on top of a refuted problem statement.

Everything else stays a warning — see §6.3 of the design.
"""
from fastapi.testclient import TestClient


class _Report:
    def __init__(self, verdict):
        self.verdict, self.status, self.contentMd = verdict, "done", "текст"


def _client(monkeypatch, report_verdict):
    import server

    class _Video:
        id, title, briefs, segments = "vid1", "T", [object()], []

    monkeypatch.setattr(server, "get_video", lambda *a, **k: _Video())
    monkeypatch.setattr(server, "get_expansion",
                        lambda v, m: _Report(report_verdict) if m == "report" else None)
    # raise_server_exceptions=False: requests that pass the gate fall through into
    # real generation (settings, DB, thread launch) and blow up. The subject here is
    # the gate, so a downstream failure must surface as a 500 to assert against —
    # not as a test error that hides whether the gate fired.
    return TestClient(server.app, raise_server_exceptions=False)


def test_refuted_blocks_spec(monkeypatch):
    client = _client(monkeypatch, "refuted")
    r = client.post("/videos/vid1/expand-spec", json={"mode": "spec"})
    assert r.status_code == 409
    assert "не подтверждена" in r.json()["detail"]


def test_override_lets_it_through(monkeypatch):
    client = _client(monkeypatch, "refuted")
    r = client.post("/videos/vid1/expand-spec", json={"mode": "spec", "override": True})
    assert r.status_code != 409


def test_confirmed_and_partial_do_not_block(monkeypatch):
    for verdict in ("confirmed", "partial", None):
        client = _client(monkeypatch, verdict)
        r = client.post("/videos/vid1/expand-spec", json={"mode": "spec"})
        assert r.status_code != 409, f"вердикт {verdict} не должен блокировать"


def test_refuted_does_not_block_other_modes(monkeypatch):
    client = _client(monkeypatch, "refuted")
    for mode in ("research", "report", "uiux", "ai_algorithms", "ai_skills"):
        r = client.post("/videos/vid1/expand-spec", json={"mode": mode})
        assert r.status_code != 409, f"{mode} не должен блокироваться вердиктом"

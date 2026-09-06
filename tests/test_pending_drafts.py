"""Drafts never reach the expand prompt — _current_doc_text reads applied edits only.

Silence here cost a 24k-char brief draft on Io_f4G7a_Eo; the endpoint makes it visible.
"""
from fastapi.testclient import TestClient


class _Draft:
    def __init__(self, chars):
        self.contentMd = "x" * chars
        class _D:
            def isoformat(self): return "2026-09-04T00:00:00"
        self.updatedAt = _D()


def test_lists_only_kinds_that_have_drafts(monkeypatch):
    import server
    drafts = {"brief": _Draft(23984), "essence": _Draft(1250)}
    monkeypatch.setattr(server, "get_transcript_draft", lambda v, k: drafts.get(k))
    client = TestClient(server.app)
    body = client.get("/videos/vid1/pending-drafts").json()
    kinds = {d["kind"]: d["chars"] for d in body["drafts"]}
    assert kinds == {"brief": 23984, "essence": 1250}


def test_empty_when_nothing_pending(monkeypatch):
    import server
    monkeypatch.setattr(server, "get_transcript_draft", lambda v, k: None)
    client = TestClient(server.app)
    assert client.get("/videos/vid1/pending-drafts").json()["drafts"] == []

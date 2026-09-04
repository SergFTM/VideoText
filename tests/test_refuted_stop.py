"""The pipeline's single hard stop: no ТЗ on top of a refuted problem statement.

Everything else stays a warning — see §6.3 of the design.

The version-aware half of this file guards the way the stop used to turn itself
off: `start_expansion` appends an empty `verdict=NULL` row, so a gate reading the
plain latest version stopped blocking the moment someone re-ran the report — and
stayed unblocked forever if that run then failed.
"""
from fastapi.testclient import TestClient

import store


class _Report:
    def __init__(self, verdict, version=1, status="done"):
        self.verdict, self.status, self.contentMd = verdict, status, "текст"
        self.version = version


def _client(monkeypatch, report_verdict):
    import server

    class _Video:
        id, title, briefs, segments = "vid1", "T", [object()], []

    monkeypatch.setattr(server, "get_video", lambda *a, **k: _Video())
    monkeypatch.setattr(server, "get_expansion", lambda v, m: None)
    monkeypatch.setattr(server, "get_latest_done_expansion",
                        lambda v, m: _Report(report_verdict) if m == "report" else None)
    # raise_server_exceptions=False: requests that pass the gate fall through into
    # real generation (settings, DB, thread launch) and blow up. The subject here is
    # the gate, so a downstream failure must surface as a 500 to assert against —
    # not as a test error that hides whether the gate fired.
    return TestClient(server.app, raise_server_exceptions=False)


def _versioned_client(monkeypatch, rows):
    """Endpoint wired to a fake store holding several report versions.

    `get_expansion` keeps its real contract (newest row whatever its status);
    `get_latest_done_expansion` returns the newest finished one. A gate that slips
    back to reading `get_expansion` fails these tests instead of passing quietly.
    """
    import server

    class _Video:
        id, title, briefs, segments = "vid1", "T", [object()], []

    def _newest(video_id, mode):
        return max(rows, key=lambda r: r.version) if mode == "report" else None

    def _newest_done(video_id, mode):
        done = [r for r in rows if r.status == "done"]
        return max(done, key=lambda r: r.version) if (done and mode == "report") else None

    monkeypatch.setattr(server, "get_video", lambda *a, **k: _Video())
    monkeypatch.setattr(server, "get_expansion", _newest)
    monkeypatch.setattr(server, "get_latest_done_expansion", _newest_done)
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


# ─── the stop must survive a report regeneration ───────────────────

def test_running_regeneration_does_not_lift_the_block(monkeypatch):
    """Re-running the report appends an empty running row — v1's refuted still holds."""
    rows = [_Report("refuted", version=1),
            _Report(None, version=2, status="running")]
    client = _versioned_client(monkeypatch, rows)
    r = client.post("/videos/vid1/expand-spec", json={"mode": "spec"})
    assert r.status_code == 409


def test_failed_regeneration_does_not_lift_the_block(monkeypatch):
    """The worst case: the block used to disappear permanently and silently."""
    rows = [_Report("refuted", version=1),
            _Report(None, version=2, status="error")]
    client = _versioned_client(monkeypatch, rows)
    r = client.post("/videos/vid1/expand-spec", json={"mode": "spec"})
    assert r.status_code == 409


def test_newer_confirmed_report_lifts_the_block(monkeypatch):
    """A successful re-run genuinely supersedes the refuted verdict."""
    rows = [_Report("refuted", version=1), _Report("confirmed", version=2)]
    client = _versioned_client(monkeypatch, rows)
    r = client.post("/videos/vid1/expand-spec", json={"mode": "spec"})
    assert r.status_code != 409


# ─── and the accessor it relies on, against a real DB ──────────────

async def _seed(db, video_id="vidR"):
    await db.video.create(data={"id": video_id, "url": "u", "source": "test"})


async def _start(video_id, mode="report"):
    return await store._start_expansion(
        video_id=video_id, mode=mode, source_title="бриф", source_md="",
        context_mode="both", model="claude-sonnet-4-6", num_ctx=32768, input_chars=10,
    )


async def test_latest_done_skips_a_running_row(db):
    await _seed(db)
    await _start("vidR")
    await store._finish_expansion(video_id="vidR", mode="report",
                                  content_md="v1", elapsed_ms=1, verdict="refuted")
    await _start("vidR")  # regeneration in flight: empty row, verdict NULL

    assert (await store._get_expansion("vidR", "report")).version == 2
    latest_done = await store._get_latest_done_expansion("vidR", "report")
    assert latest_done.version == 1
    assert latest_done.verdict == "refuted"


async def test_latest_done_skips_an_errored_row(db):
    await _seed(db)
    await _start("vidR")
    await store._finish_expansion(video_id="vidR", mode="report",
                                  content_md="v1", elapsed_ms=1, verdict="refuted")
    await _start("vidR")
    await store._fail_expansion(video_id="vidR", mode="report", error="упало")

    latest_done = await store._get_latest_done_expansion("vidR", "report")
    assert latest_done.version == 1
    assert latest_done.verdict == "refuted"


async def test_latest_done_takes_the_newer_finished_row(db):
    await _seed(db)
    await _start("vidR")
    await store._finish_expansion(video_id="vidR", mode="report",
                                  content_md="v1", elapsed_ms=1, verdict="refuted")
    await _start("vidR")
    await store._finish_expansion(video_id="vidR", mode="report",
                                  content_md="v2", elapsed_ms=1, verdict="confirmed")

    latest_done = await store._get_latest_done_expansion("vidR", "report")
    assert latest_done.version == 2
    assert latest_done.verdict == "confirmed"


async def test_latest_done_is_none_when_nothing_finished(db):
    await _seed(db)
    await _start("vidR")
    assert await store._get_latest_done_expansion("vidR", "report") is None

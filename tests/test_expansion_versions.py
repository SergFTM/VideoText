"""Versioning of Expansion rows.

Regeneration must append a version instead of overwriting, so two concurrent
clients (or two models) never clobber each other's artifact.
"""
import store


async def _seed_video(db, video_id="vidV"):
    await db.video.create(data={"id": video_id, "url": "u", "source": "test"})


async def _start(video_id, mode, model="claude-sonnet-4-6"):
    return await store._start_expansion(
        video_id=video_id, mode=mode, source_title="бриф", source_md="",
        context_mode="both", model=model, num_ctx=32768, input_chars=100,
    )


async def test_first_start_creates_version_1(db):
    await _seed_video(db)
    row = await _start("vidV", "research")
    assert row.version == 1
    assert row.fromVersion is None


async def test_second_start_appends_version_2(db):
    await _seed_video(db)
    await _start("vidV", "research")
    second = await _start("vidV", "research", model="claude-opus-5")
    assert second.version == 2
    assert second.fromVersion == 1


async def test_get_expansion_returns_latest_version(db):
    await _seed_video(db)
    await _start("vidV", "research")
    await store._finish_expansion(
        video_id="vidV", mode="research", content_md="первый", elapsed_ms=10)
    await _start("vidV", "research", model="claude-opus-5")
    await store._finish_expansion(
        video_id="vidV", mode="research", content_md="второй", elapsed_ms=20)

    current = await store._get_expansion("vidV", "research")
    assert current.version == 2
    assert current.contentMd == "второй"
    assert current.model == "claude-opus-5"


async def test_versions_are_per_mode(db):
    await _seed_video(db)
    await _start("vidV", "research")
    report = await _start("vidV", "report")
    assert report.version == 1, "нумерация версий не должна быть сквозной по режимам"


async def test_list_expansion_versions_newest_first(db):
    await _seed_video(db)
    await _start("vidV", "research")
    await store._finish_expansion(
        video_id="vidV", mode="research", content_md="a", elapsed_ms=1)
    await _start("vidV", "research")
    rows = await store._list_expansion_versions("vidV", "research")
    assert [r.version for r in rows] == [2, 1]


async def test_list_expansions_returns_one_row_per_mode(db):
    await _seed_video(db)
    await _start("vidV", "research")
    await _start("vidV", "research")
    await _start("vidV", "report")
    rows = await store._list_expansions("vidV")
    modes = sorted(r.mode for r in rows)
    assert modes == ["report", "research"], "модалка UI не должна видеть дубликаты версий"
    research = next(r for r in rows if r.mode == "research")
    assert research.version == 2


async def test_fail_marks_latest_version_only(db):
    await _seed_video(db)
    await _start("vidV", "research")
    await store._finish_expansion(
        video_id="vidV", mode="research", content_md="хороший", elapsed_ms=5)
    await _start("vidV", "research")
    await store._fail_expansion(video_id="vidV", mode="research", error="boom")

    versions = await store._list_expansion_versions("vidV", "research")
    assert versions[0].status == "error"
    assert versions[1].status == "done"
    assert versions[1].contentMd == "хороший", "предыдущая версия должна пережить ошибку"

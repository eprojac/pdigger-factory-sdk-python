"""Opt-in checks against a unit (`PDIGGER_FACTORY_URL`) and the complete recordings (`PDIGGER_FACTORY_RECORDINGS`).

Both are skipped unless their variable is set, so the Docker unit tests (no variable, no network) never run them.
Against a unit they are read-only: a guard transport refuses any control without `dry_run=1`, any stream without
`observe=1` and any long-poll with a viewer id, before it reaches the network, so the shared session is never moved and
no stream keeps the worker busy. See TESTING.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import TypeVar

import httpx
import pytest

from pdigger_factory_sdk import FactoryClient, FactoryError, FrameItem, PacketItem, Replay, Summary

from .support import GOLDEN

pytestmark = pytest.mark.integration
T = TypeVar("T")

URL = os.environ.get("PDIGGER_FACTORY_URL", "")
RECORDINGS = os.environ.get("PDIGGER_FACTORY_RECORDINGS", "")
TIMEOUT = float(os.environ.get("PDIGGER_FACTORY_TIMEOUT", "60"))

needs_unit = pytest.mark.skipif(not URL, reason="set PDIGGER_FACTORY_URL=http://<unit>:8120 to check a unit")


class ReadOnly(httpx.AsyncBaseTransport):
    """Sends a request only if it cannot change the unit: GET/HEAD, controls with `dry_run=1`, observing streams."""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        query, path = request.url.params, request.url.path
        if request.method not in ("GET", "HEAD") and query.get("dry_run") != "1":
            raise AssertionError(f"refused a control without dry_run=1: {request.method} {request.url}")
        if path.endswith(("/frames/stream", "/raw/stream")) and query.get("observe") != "1":
            raise AssertionError(f"refused a stream without observe=1: {request.url}")
        if path.endswith("/session/frames") and "cid" in query:
            raise AssertionError(f"refused a long-poll that counts as a viewer: {request.url}")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def outcome(call: Awaitable[T]) -> T | FactoryError:
    """The result of `call`, or the `FactoryError` it raised."""
    try:
        return await call
    except FactoryError as e:
        return e


# What a unit with no frame to give answers (an idle worker: nobody is watching, and these checks never count as a
# viewer): no retained frame, no world.
NO_FRAMES = ("not_retained", "not_ready", "world_unavailable")


def assert_no_frames(e: FactoryError, *also: str) -> None:
    assert e.code in NO_FRAMES + also, e


@pytest.fixture
async def factory() -> AsyncIterator[FactoryClient]:
    async with FactoryClient(URL, transport=ReadOnly(), observe=True, retries=2) as client:
        yield client


@needs_unit
async def test_the_unit_describes_itself(factory: FactoryClient) -> None:
    api = factory.api
    root = await api.get_api_root()
    assert root.api.startswith("2.")
    assert root.build
    assert (await api.get_health()).ok
    assert (await api.get_model()).raw_json
    assert (await api.get_topology()).joints == 70
    assert (await api.get_status()).raw_json
    assert "openapi" in await api.get_openapi()
    metrics = await api.get_metrics()
    assert metrics.samples
    ready = await outcome(api.get_ready())
    assert ready.code == "not_ready" if isinstance(ready, FactoryError) else ready.ready


@needs_unit
async def test_the_catalog(factory: FactoryClient) -> None:
    api = factory.api
    catalog = await api.list_videos()
    assert catalog is not None
    assert catalog.videos
    if catalog.etag is not None:
        assert await api.list_videos(if_none_match=catalog.etag) is None
    for source in catalog.videos:
        assert (await api.get_video(source.id)).id == source.id
    camera = await outcome(factory.live())
    if isinstance(camera, FactoryError):
        assert camera.code == "live_unavailable"
    else:
        assert camera.is_live
        assert not camera.seekable
        media = await outcome(api.get_video_media(camera.id))  # a camera has no media: its pictures come per frame
        assert isinstance(media, FactoryError)
        assert (media.status, media.code, media.field) == (404, "not_found", "id")
    recording = next((s for s in catalog.videos if not s.is_live), None)
    if recording is not None:
        head = await api.head_video_media(recording.id)
        assert head.status == 200
        part = await api.get_video_media(recording.id, range=(0, 1023))
        assert part.status == 206
        assert len(part.data) == 1024
        async with api.open_video_media(recording.id, range=(0, 1023)) as stream:
            assert b"".join([chunk async for chunk in stream]) == part.data


@needs_unit
async def test_the_session_and_its_clock(factory: FactoryClient) -> None:
    clock = await factory.session.clock()
    assert abs(clock.offset) < 3600
    assert clock.rtt >= 0
    progress = clock.progress()
    assert progress is None or 0.0 <= progress <= 1.0
    focus = await factory.api.get_focus()
    assert focus.raw_json is not None


@needs_unit
async def test_the_world_frames_and_renders(factory: FactoryClient) -> None:
    api = factory.api
    world = await outcome(api.get_world())
    if isinstance(world, FactoryError):
        assert_no_frames(world)
    else:
        log = await api.get_world_events(limit=5)
        # at most 5 events by whole frames: a frame that alone has more (every stone of a world that just started is
        # new) comes whole
        assert len(log.events) <= 5 or {e.g for e in log.events} == {log.events[0].g}
        if world.stones:
            track = await api.get_stone(world.stones[0].id)
            assert track.id == world.stones[0].id
    frame = await outcome(api.get_frame("latest", layers=["raw", "overlay", "scene", "cards", "world"]))
    if isinstance(frame, FactoryError):
        assert_no_frames(frame)
        return  # no retained frame: the unit's worker is idle
    if not isinstance(world, FactoryError):
        assert frame.session == world.session
    # each read below may come after the frame left the retained window (or its session ended)
    png = await outcome(api.get_frame_raster(frame.g, "regions"))
    if isinstance(png, FactoryError):
        assert_no_frames(png)
    else:
        assert png.startswith(b"\x89PNG")
    packet = await outcome(api.raw_packet("latest"))
    if isinstance(packet, FactoryError):
        assert_no_frames(packet)
    else:
        assert packet.schema == "sip1"
    tensors = await outcome(api.raw_tensors(frame.g))
    if isinstance(tensors, FactoryError):
        assert_no_frames(tensors, "debug_disabled")
    else:
        assert tensors
    overlay = await outcome(api.render_overlay2d(g=frame.g, draw="primitives"))
    if isinstance(overlay, FactoryError):
        assert_no_frames(overlay, "session_gone")
    else:
        assert overlay.g == frame.g
    scene = await outcome(api.render_scene3d(g=frame.g, surface="compact"))
    if isinstance(scene, FactoryError):
        assert_no_frames(scene, "session_gone")
    else:
        assert scene.raw_json


async def _first_frames(factory: FactoryClient, n: int) -> list[FrameItem]:
    got: list[FrameItem] = []
    async with factory.streams.frames(["world"], observe=True, backfill=True) as stream:
        async for item in stream:
            if isinstance(item, FrameItem):
                got.append(item)
                if len(got) == n:
                    break
    return got


async def _first_packet(factory: FactoryClient) -> PacketItem | None:
    async with factory.streams.raw_packets(format="ndjson", observe=True) as packets:
        async for item in packets:
            if isinstance(item, PacketItem):
                return item
    return None


@needs_unit
async def test_streams_observe(factory: FactoryClient) -> None:
    try:
        frames = await asyncio.wait_for(_first_frames(factory, 5), TIMEOUT)
        packet = await asyncio.wait_for(_first_packet(factory), TIMEOUT)
    except asyncio.TimeoutError:
        pytest.skip("no frames: nobody is watching, so the worker is idle (the checks never count as a viewer)")
    assert packet is not None
    assert packet.packet.schema == "sip1"
    assert [f.frame.g for f in frames] == sorted(f.frame.g for f in frames)
    reply = await factory.api.poll_frames(cursor=frames[-1].frame.cursor, layers=["world"], wait=0)
    assert reply.cursor >= frames[-1].frame.cursor


@needs_unit
async def test_the_interpreter_observes(factory: FactoryClient) -> None:
    interpreter = factory.interpreter(observe=True)

    async def follow() -> None:
        async with interpreter.updates() as updates:
            async for update in updates:
                assert all(e.describe() for e in update.events)
                if update.state.frames >= 25:
                    break

    try:
        await asyncio.wait_for(follow(), TIMEOUT)
    except asyncio.TimeoutError:
        pytest.skip("no frames: nobody is watching, so the worker is idle")
    assert interpreter.summary().frames >= 25
    assert interpreter.state.wall is not None  # from GET /session: the unit sends no clock event on connect


@needs_unit
async def test_the_sdk_ends_a_connection_at_its_limit(factory: FactoryClient) -> None:
    """The unit holds a stream about 10 s after its `limit`; the SDK closes it at once and resumes after it."""

    async def six() -> list[FrameItem]:
        got: list[FrameItem] = []
        async with factory.streams.frames(["world"], observe=True, limit=3) as stream:
            async for item in stream:
                if isinstance(item, FrameItem):
                    got.append(item)
                    if len(got) == 6:
                        break
        return got

    start = time.monotonic()
    try:
        frames = await asyncio.wait_for(six(), TIMEOUT)
    except asyncio.TimeoutError:
        pytest.skip("no frames: nobody is watching, so the worker is idle")
    assert [f.frame.g for f in frames] == sorted({f.frame.g for f in frames})
    assert time.monotonic() - start < 8.0  # a frame comes every 0.24 s; the unit's own close would take 10 s more


@needs_unit
async def test_controls_as_dry_runs(factory: FactoryClient) -> None:
    session = factory.session
    before = await session.state()
    assert (await session.watch(before.video, dry_run=True)).video == before.video
    if before.live is None and before.video != "live":  # the SDK's own test of a camera session
        await session.seek(0.0, dry_run=True)
    else:
        with pytest.raises(FactoryError, match="not_seekable"):
            await session.seek(0.0, dry_run=True)  # refused by the SDK, after reading the state
    await session.pause(dry_run=True)
    await session.play(dry_run=True)
    world = await outcome(factory.api.get_world())
    if isinstance(world, FactoryError):
        assert_no_frames(world)  # an idle unit has no world, so no stone to focus
    elif world.stones:
        await session.focus(world.stones[0].id, dry_run=True)
    await session.clear_focus(dry_run=True)
    after = await session.state()
    if after.session != before.session:  # only dry runs were sent (ReadOnly refuses any other control)
        print(f"another client moved the session: {before.session} -> {after.session} ({after.changed_by})")


# ---- the complete recordings ----------------------------------------------------------------------------------------
def _recordings() -> list[Path]:
    return sorted(Path(RECORDINGS).glob("*.world.ndjson.gz")) if RECORDINGS else []


def _expected_summaries(recording: Path) -> list[Path]:
    """The golden summary of `fixtures/golden/complete` and the published one downloaded beside the recording."""
    video = recording.name.split(".")[0]
    found = [
        p
        for p in (GOLDEN / "complete" / f"{video}.summary.json", recording.with_name(f"{video}.summary.json"))
        if p.exists()
    ]
    assert found, f"no summary for {video}: expected one in fixtures/golden/complete or {video}.summary.json beside it"
    return found


@pytest.mark.skipif(not RECORDINGS, reason="set PDIGGER_FACTORY_RECORDINGS=<folder of the published recordings>")
def test_the_folder_has_recordings() -> None:
    assert _recordings(), f"no <video>.world.ndjson.gz in {RECORDINGS}"


@pytest.mark.parametrize("recording", _recordings(), ids=lambda p: p.name)
def test_a_complete_recording(recording: Path) -> None:
    summary = Replay.open(recording).summary()
    for path in _expected_summaries(recording):
        expected = json.loads(path.read_bytes())
        assert summary.to_dict() == expected, path
        assert summary == Summary.from_dict(expected)
        text = path.with_suffix(".txt")
        if text.exists():
            assert summary.describe().encode("utf-8") == text.read_bytes(), text


@pytest.mark.parametrize("recording", _recordings()[:1], ids=lambda p: p.name)
async def test_a_complete_recording_async(recording: Path) -> None:
    expected = json.loads(_expected_summaries(recording)[0].read_bytes())
    assert (await Replay.open(recording).asummary()).to_dict() == expected

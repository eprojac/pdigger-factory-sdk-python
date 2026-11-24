"""The low level: one method per operation (32), against the fake server's recorded replies."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from pdigger_factory_sdk import Api, FactoryClient, FactoryError, Media, MediaStream, Metrics, Picture
from pdigger_factory_sdk.api import StreamMessage, join_layers, parse_metrics
from pdigger_factory_sdk.testing import LIVE_CAMERA, FakeFactory, FakeTime, Reply

from .support import API, SPEC, api_bytes, client, recording


@pytest.fixture
def fake() -> FakeFactory:
    return FakeFactory(replies=API)


def last(fake: FakeFactory) -> httpx.Request:
    return fake.requests[-1]


def test_join_layers() -> None:
    assert join_layers(None) is None
    assert join_layers("world,cards") == "world,cards"
    assert join_layers(("world", "cards")) == "world,cards"
    assert join_layers([]) == ""


async def test_meta_and_ops(fake: FakeFactory) -> None:
    async with client(fake) as factory:
        api = factory.api
        root = await api.get_api_root()
        assert root.api.startswith("2.")
        assert "openapi" in (await api.get_openapi())
        assert (await api.get_model()).checkpoint
        assert (await api.get_topology()).joints == 70
        assert (await api.get_health()).ok
        assert (await api.get_ready()).ready
        status = await api.get_status()
        assert status.worker.state
        metrics = await api.get_metrics()
    assert isinstance(metrics, Metrics)
    assert metrics.value("interp_frames_total", video="dry1") == 9425
    assert metrics.value("interp_frames_total", video="nope") is None
    assert len(metrics.find("interp_http_requests_total", route="v2")) >= 2
    assert last(fake).url.path == "/metrics"


def test_parse_metrics_edge_cases() -> None:
    text = '# HELP x\n\nup 1\nbad line here\nx{a="q\\"uote\\\\n\\n"} 2 1700000000\ny{} NaN\nz nope\nlonely\n'
    samples = parse_metrics(text)
    assert [(m.name, m.value) for m in samples][:2] == [("up", 1.0), ("x", 2.0)]
    assert samples[1].labels == {"a": 'q"uote\\n\n'}
    assert samples[2].name == "y"
    assert len(samples) == 3


async def test_sources(fake: FakeFactory) -> None:
    async with client(fake) as factory:
        api = factory.api
        catalog = await api.list_videos()
        assert catalog is not None
        assert "If-None-Match" not in last(fake).headers
        source = await api.get_video("dry1")
        assert source.id == "dry1"
        assert last(fake).url.path == "/api/v2/videos/dry1"
        with pytest.raises(FactoryError) as info:
            await api.get_video("a b/c")
        assert info.value.code == "unknown_video"
        assert last(fake).url.raw_path == b"/api/v2/videos/a%20b%2Fc"
        media = await api.get_video_media("dry1", range=(0, 1023))
        assert last(fake).headers["Range"] == "bytes=0-1023"
        await api.get_video_media("dry1", range=(10, None), if_range='"e"', if_none_match='"f"')
        assert last(fake).headers["Range"] == "bytes=10-"
        assert last(fake).headers["If-Range"] == '"e"'
        await api.get_video_media("dry1", range="bytes=-500")
        assert last(fake).headers["Range"] == "bytes=-500"
        await api.get_video_media("dry1")
        assert "Range" not in last(fake).headers
        head = await api.head_video_media("dry1", if_none_match='"x"')
        assert last(fake).method == "HEAD"
        await api.head_video_media("dry1")
    assert isinstance(media, Media)
    assert media.status == 206
    assert media.range == (0, 1023)
    assert media.total == 184866573
    assert media.etag == '"b04d70d-6af0f48f"'
    assert media.accept_ranges == "bytes"
    assert len(media.data) == 1024
    assert not media.not_modified
    assert head.data == b""


def test_media_helpers() -> None:
    assert Media(status=304, data=b"").not_modified
    whole = Media(status=200, data=b"abc", content_length=3)
    assert whole.range is None
    assert whole.total == 3
    assert Media(status=206, data=b"", content_range="bytes */99").range is None
    assert Media(status=206, data=b"", content_range="bytes */99").total == 99
    assert Media(status=206, data=b"", content_range="bytes 0-9/*").total is None
    assert Media(status=416, data=b"").total is None


async def test_a_camera_has_no_media_and_its_live_frames_have_pictures() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"), replies=API, live=True)
    async with client(fake) as factory:
        api = factory.api
        with pytest.raises(FactoryError) as info:
            await api.get_video_media(LIVE_CAMERA)
        assert (info.value.status, info.value.code, info.value.field) == (404, "not_found", "id")
        assert info.value.message.endswith("each frame's picture is /api/v2/session/frames/{g}/image.jpg")
        latest = await api.get_frame("latest")
        picture = await api.get_frame_picture("latest")
        assert isinstance(picture, Picture)
        assert last(fake).headers["Accept"] == "image/jpeg"
        assert picture.g == latest.g
        assert picture.jpeg[:4] == b"\xff\xd8\xff\xfe"  # a JPEG that starts with its comment
        assert picture.jpeg.endswith(b"\xff\xd9")
        assert f"FakeFactory {LIVE_CAMERA} g {latest.g}".encode() in picture.jpeg
        first = (await api.poll_frames(limit=1)).frames[0]
        again = await api.get_frame_picture(first.g, session=latest.session)
        assert again.g == first.g
        assert str(last(fake).url).endswith(f"/frames/{first.g}/image.jpg?session={latest.session}")
        refused = [
            (await outcome(api.get_frame_picture(first.g - 1)), 404, "not_retained"),  # a warm-up frame: never shown
            (await outcome(api.get_frame_picture("latest", session=latest.session - 1)), 410, "session_gone"),
        ]
        await factory.session.watch("dry1")
        refused.append((await outcome(api.get_frame_picture("latest")), 404, "not_found"))  # a recording has none
        # a stale session is gone whatever the session shows now: the 410 comes before the recording's 404
        refused.append((await outcome(api.get_frame_picture("latest", session=latest.session)), 410, "session_gone"))
    for error, status, code in refused:
        assert isinstance(error, FactoryError)
        assert (error.status, error.code) == (status, code)


async def outcome(call: Any) -> Any:
    try:
        return await call
    except FactoryError as e:
        return e


async def test_a_picture_reply_without_its_frame_is_a_decode_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=b"\xff\xd8\xff\xd9")

    async with FactoryClient("http://unit.test", transport=httpx.MockTransport(handler)) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.get_frame_picture(3)
    assert (info.value.code, info.value.status) == ("decode", 200)
    assert info.value.message == "the picture reply has no X-Interp-G header"


async def test_open_video_media_of_a_recording_by_range(fake: FakeFactory) -> None:
    async with client(fake) as factory, factory.api.open_video_media("dry1", range=(0, 1023)) as stream:
        data = b"".join([chunk async for chunk in stream])
        media = stream.media
    assert last(fake).headers["Range"] == "bytes=0-1023"
    assert (media.status, media.range, media.total, media.etag) == (206, (0, 1023), 184866573, '"b04d70d-6af0f48f"')
    assert data == api_bytes("getVideoMedia-range")


class _Endless(httpx.AsyncByteStream):
    """A body that never ends, as a live stream's; it records whether it was read or closed."""

    def __init__(self) -> None:
        self.sent = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.sent += 1
            await asyncio.sleep(0)
            yield b"\x00" * 64

    async def aclose(self) -> None:
        self.closed = True


async def test_get_video_media_refuses_a_body_without_an_end() -> None:
    bodies: list[_Endless] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(_Endless())
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, stream=bodies[-1])

    t = FakeTime()
    async with FactoryClient("http://unit.test", transport=httpx.MockTransport(handler), sleep=t.sleep) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.get_video_media("cam2")
        async with factory.api.open_video_media("cam2") as stream:
            assert len(await stream.take(3)) == 3
    assert (info.value.code, info.value.field, info.value.status) == ("not_seekable", "id", 200)
    assert info.value.message == "the media of 'cam2' is a live stream that never ends; read it with open_video_media()"
    assert (bodies[0].sent, bodies[0].closed) == (0, True)  # closed unread, and not retried
    assert len(bodies) == 2
    assert bodies[1].closed
    assert t.slept == []


def endless_unit(bodies: list[_Endless]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "video/mp4"
        bodies.append(_Endless())
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, stream=bodies[-1])

    return httpx.MockTransport(handler)


async def test_endless_media_is_read_as_a_stream_in_chunks() -> None:
    bodies: list[_Endless] = []
    async with FactoryClient("http://unit.test", transport=endless_unit(bodies)) as factory:
        async with factory.api.open_video_media("cam2", chunk_size=256) as stream:
            assert isinstance(stream, MediaStream)
            media = stream.media
            assert (media.status, media.content_type, media.content_length, media.data) == (200, "video/mp4", None, b"")
            assert stream.response.status_code == 200
            chunks: list[bytes] = []
            async for chunk in stream:
                chunks.append(chunk)
                if len(chunks) == 6:
                    break
            closed = [bodies[0].closed]  # still open after the break: the block closes it
        closed.append(bodies[0].closed)
    assert closed == [False, True]
    assert {len(c) for c in chunks} == {256}


async def test_cancelling_an_endless_media_read_closes_its_connection() -> None:
    bodies: list[_Endless] = []
    started = asyncio.Event()

    async def watch_forever(factory: FactoryClient) -> None:
        async with factory.api.open_video_media("cam2") as stream:
            async for _ in stream:
                started.set()

    async with FactoryClient("http://unit.test", transport=endless_unit(bodies)) as factory:
        task = asyncio.create_task(watch_forever(factory))
        await started.wait()
        closed = [bodies[0].closed]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    closed.append(bodies[0].closed)
    assert closed == [False, True]


async def test_get_video_media_reads_a_body_of_byte_ranges_without_a_length() -> None:
    class Chunked(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"ab"
            yield b"c"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Accept-Ranges": "bytes"}, stream=Chunked())

    async with FactoryClient("http://unit.test", transport=httpx.MockTransport(handler)) as factory:
        media = await factory.api.get_video_media("dry1")
    assert (media.status, media.data, media.content_length, media.total) == (200, b"abc", None, None)


async def test_a_media_stream_that_breaks_is_a_transport_error() -> None:
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"moof"
            raise httpx.ReadError("the camera went away")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "video/mp4"}, stream=Broken())

    async with FactoryClient("http://unit.test", transport=httpx.MockTransport(handler)) as factory:
        with pytest.raises(FactoryError) as info:
            async with factory.api.open_video_media("cam2") as stream:
                async for _ in stream:
                    pass
    assert info.value.code == "transport"
    assert info.value.message == "the media download broke: ReadError: the camera went away"


async def test_list_videos_not_modified() -> None:
    fake = FakeFactory(replies={"listVideos": Reply(status=304, body=b"", headers={"ETag": '"e"'})})
    async with client(fake) as factory:
        assert await factory.api.list_videos(if_none_match='"e"') is None
    assert last(fake).headers["If-None-Match"] == '"e"'


async def test_the_catalog_etag() -> None:
    fake = FakeFactory(recording=recording("joined-mid-move"), live=True)
    async with client(fake) as factory:
        catalog = await factory.api.list_videos()
        assert catalog is not None
        assert catalog.etag is not None
        assert await factory.api.list_videos(if_none_match=catalog.etag) is None
        again = await factory.api.list_videos(if_none_match='"stale"')
    assert again == catalog
    replied = FakeFactory(replies={"listVideos": Reply(body={"schema": "x", "videos": []})})
    async with client(replied) as factory:
        catalog = await factory.api.list_videos()
    assert catalog is not None
    assert catalog.etag is None


async def test_session_and_controls(fake: FakeFactory) -> None:
    async with client(fake) as factory:
        api = factory.api
        reply = await api.get_session()
        assert reply.state.video == "dry2"
        focus = await api.get_focus()
        assert focus.focus is None
        await api.put_focus(43, g=3079, session=reply.state.session, dry_run=True)
        assert json.loads(last(fake).content) == {"stone_id": 43, "g": 3079, "session": reply.state.session}
        await api.put_focus(43)
        assert json.loads(last(fake).content) == {"stone_id": 43}
        focus = await api.get_focus()
        assert focus.focus is not None
        assert focus.focus.id == 43
        assert focus.resolved is not None
        cleared = await api.delete_focus(dry_run=True)
        assert last(fake).method == "DELETE"
        assert last(fake).content == b""
        assert cleared.state.focus is None
        await api.delete_focus()
        sel = await api.session_select("dry1", 12.5, dry_run=True)
        req = last(fake)
        assert req.method == "POST"
        assert req.url.params["dry_run"] == "1"
        assert json.loads(req.content) == {"video": "dry1", "time": 12.5}
        assert sel.dry_run
        await api.session_select("dry2")
        assert json.loads(last(fake).content) == {"video": "dry2"}
        assert "dry_run" not in last(fake).url.params
        await api.session_seek(5.0, video="dry2", dry_run=True)
        assert json.loads(last(fake).content) == {"time": 5.0, "video": "dry2"}
        await api.session_seek(6.0)
        await api.session_play(dry_run=True)
        assert json.loads(last(fake).content) == {}
        paused = await api.session_pause()
        assert not paused.state.playing


async def test_world(fake: FakeFactory) -> None:
    async with client(fake) as factory:
        api = factory.api
        world = await api.get_world(precision="full")
        assert last(fake).url.params["precision"] == "full"
        assert world.stones
        log = await api.get_world_events(after=10, limit=5, types=("lift", "place"))
        assert dict(last(fake).url.params) == {"after": "10", "limit": "5", "type": "lift,place"}
        assert log.events is not None
        await api.get_world_events()
        assert dict(last(fake).url.params) == {}
        track = await api.get_stone(43)
        assert track.id == 43
        assert last(fake).url.path == "/api/v2/session/world/stones/43"


async def test_frames_and_codec(fake: FakeFactory) -> None:
    async with client(fake) as factory:
        api = factory.api
        frame = await api.get_frame("latest", layers=["world", "cards"], session=1795314885, precision="rounded")
        assert last(fake).url.path == "/api/v2/session/frames/latest"
        assert last(fake).url.params["layers"] == "world,cards"
        assert frame.g > 0
        assert (await api.get_frame(frame.g)).g == frame.g
        png = await api.get_frame_raster(frame.g, "regions", scale=2)
        assert png.startswith(b"\x89PNG")
        assert last(fake).url.path == f"/api/v2/session/frames/{frame.g}/regions.png"
        assert last(fake).headers["Accept"] == "image/png"
        packet = await api.raw_packet("latest", session=1795314885)
        assert packet.schema == "sip1"
        with pytest.raises(FactoryError) as info:
            await api.raw_tensors(frame.g)
        assert info.value.code == "debug_disabled"
        assert last(fake).headers["Accept"] == "application/octet-stream"


async def test_poll(fake: FakeFactory) -> None:
    async with client(fake) as factory:
        reply = await factory.api.poll_frames(
            cursor=0, rev=0, cid="viewer-0001", layers="world", limit=4, wait=0, precision="full"
        )
        assert dict(last(fake).url.params) == {
            "cursor": "0",
            "rev": "0",
            "cid": "viewer-0001",
            "layers": "world",
            "limit": "4",
            "wait": "0",
            "precision": "full",
        }
        assert reply.frames
        with pytest.raises(FactoryError) as info:
            await factory.api.poll_frames(cid="bad")
        assert info.value.field == "cid"


async def test_renders(fake: FakeFactory) -> None:
    async with client(fake) as factory:
        api = factory.api
        overlay = await api.render_overlay2d(g=2913, mode="stable", session=1795314885)
        assert overlay.schema
        prim = await api.render_overlay2d(t=582.1, draw="primitives")
        assert prim.primitives is not None
        scene = await api.render_scene3d(g=2913, surface="vertices", draw="shapes", mode="raw")
        assert scene.surface is not None
        compact = await api.render_scene3d(t=582.1, surface="compact")
        assert compact.surface is not None
        assert compact.surface.form == "compact"
        none = await api.render_scene3d(g=2913, surface="none")
        assert none.surface is None
        for call in (api.render_overlay2d, api.render_scene3d):
            with pytest.raises(ValueError, match="exactly one of g and t"):
                await call()
            with pytest.raises(ValueError, match="exactly one of g and t"):
                await call(g=1, t=2.0)


async def test_frames_stream_sse(fake: FakeFactory) -> None:
    async with client(fake) as factory:
        stream = await factory.api.stream_frames(
            layers="world", after=4000, limit=3, max_s=60, observe=True, precision="rounded", last_event_id=None
        )
        req = last(fake)
        assert req.headers["Accept"] == "text/event-stream"
        assert dict(req.url.params) == {
            "layers": "world",
            "format": "sse",
            "after": "4000",
            "limit": "3",
            "max_s": "60",
            "observe": "1",
            "precision": "rounded",
        }
        assert stream.response.status_code == 200
        async with stream:
            messages = [m async for m in stream]
    assert [m.event for m in messages] == ["frame", "frame", "frame"]
    assert messages[0].id == "a2011b037b-10765"
    assert messages[0].json()["cursor"] == 10765


async def test_frames_stream_ndjson_and_gap_lines() -> None:
    body = api_bytes("streamFrames-ndjson") + b'\n{"gap": 3}\n'
    fake = FakeFactory(
        replies={"streamFrames-ndjson": Reply(body=body, content_type="application/x-ndjson")}, on_exhausted="end"
    )
    async with client(fake) as factory:
        stream = await factory.api.stream_frames(format="ndjson", last_event_id="b-9")
        assert last(fake).headers["Accept"] == "application/x-ndjson"
        messages = [m async for m in stream]
    assert [m.event for m in messages] == ["frame", "frame", "frame", "gap"]
    assert messages[-1].json() == {"gap": 3}
    assert messages[0].id is None


async def test_raw_stream(fake: FakeFactory) -> None:
    async with client(fake) as factory:
        nd = await factory.api.raw_stream(limit=2, observe=True)
        assert last(fake).url.params["format"] == "ndjson"
        got = [m async for m in nd]
        sse = await factory.api.raw_stream(format="sse", after=1, max_s=5)
        got_sse = [m async for m in sse]
    assert len(got) == 2
    assert got[0].json()["schema"] == "sip1"
    assert [m.id for m in got_sse] == ["a2011b037b-10774", "a2011b037b-10775"]


async def test_stream_message_bad_json() -> None:
    with pytest.raises(FactoryError, match="clock event: the reply is not JSON"):
        StreamMessage(event="clock", data="{").json()


async def test_a_broken_stream_is_a_transport_error() -> None:
    fake = FakeFactory(recording=recording("joined-mid-move"))
    fake.drop_after(2)
    async with client(fake) as factory:
        stream = await factory.api.stream_frames()
        with pytest.raises(FactoryError) as info:
            async for _ in stream:
                pass
    assert info.value.code == "transport"
    assert fake.streams_closed == 1


async def test_an_event_stream_closed_unread_closes_its_connection() -> None:
    fake = FakeFactory(recording=recording("joined-mid-move"))
    async with client(fake) as factory:
        stream = await factory.api.stream_frames()
        assert fake.streams_opened == 1
        await stream.aclose()
        assert fake.streams_closed == 1
        async with await factory.api.raw_stream():
            pass
    assert fake.streams_opened == fake.streams_closed == 2


def test_every_operation_has_a_method() -> None:
    spec = json.loads((SPEC / "openapi.json").read_text(encoding="utf-8"))
    ops = [op["operationId"] for path in spec["paths"].values() for op in path.values() if "operationId" in op]
    assert len(ops) == 32
    for op in ops:
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", op).lower().replace("open_api", "openapi")
        assert callable(getattr(Api, name)), op

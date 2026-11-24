"""The mid level: frames over SSE with resume, revisions, duplicates and backfill; the long-poll; raw packets."""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

import httpx
import pytest

from pdigger_factory_sdk import (
    ClockItem,
    FactoryClient,
    FactoryError,
    FrameItem,
    GapItem,
    PacketItem,
    ResetItem,
    StreamItem,
    WorkerItem,
)
from pdigger_factory_sdk import streams as streams_module
from pdigger_factory_sdk.models import Frame
from pdigger_factory_sdk.streams import _Delivered, merge_layers
from pdigger_factory_sdk.testing import FakeExhausted, FakeFactory, FakeTime

from .support import BASE_URL, client, gated_client, recording, stream_requests
from .synth import HEADER, clock_line, frame, worker_line


def frames_of(items: list[StreamItem]) -> list[tuple[int, int, bool, bool]]:
    """`(g, rev, is_revision, is_backfill)` of the frame items."""
    return [
        (i.frame.g, i.frame.world.rev if i.frame.world else -1, i.is_revision, i.is_backfill)
        for i in items
        if isinstance(i, FrameItem)
    ]


async def collect(factory: FactoryClient, **kw: Any) -> list[StreamItem]:
    out: list[StreamItem] = []
    with pytest.raises(FakeExhausted):
        async for item in factory.streams.frames(**kw):
            out.append(item)
    return out


async def test_items_and_ids() -> None:
    """The unit sends no clock event on connect: the recording's first clock line is the state the stream starts in."""
    fake = FakeFactory(recording=[HEADER, clock_line(), frame(0), worker_line(), frame(1), frame(2)])
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False)]
    assert [type(i).__name__ for i in items] == ["FrameItem", "WorkerItem", "FrameItem", "FrameItem"]
    first = items[0]
    assert isinstance(first, FrameItem)
    assert first.id == "fakeb00t01-1"
    worker = items[1]
    assert isinstance(worker, WorkerItem)
    assert worker.worker.state == "running"
    assert [r.url.path for r in fake.requests] == ["/api/v2/session/frames/stream"]
    params = fake.requests[0].url.params
    assert params["format"] == "sse"
    assert params["precision"] == "rounded"


async def test_clock_on_connect_reads_the_session_after_every_connect() -> None:
    fake = FakeFactory(recording=[HEADER, clock_line(), *[frame(g) for g in range(4)]])
    fake.drop_after(2)
    start = fake.time.time()
    async with client(fake) as factory:
        items = await collect(factory, clock_on_connect=True)
        assert factory.session.last_state is not None  # the streams' clock items reach the session view
    kinds = [type(i).__name__ for i in items]
    assert kinds == ["ClockItem", "FrameItem", "FrameItem", "ClockItem", "FrameItem", "FrameItem"]
    clock = items[0]
    assert isinstance(clock, ClockItem)
    assert clock.id is None
    assert clock.server_now == start
    assert (clock.state.session, clock.state.revision, clock.state.anchor_wall) == (1, 1, 1000.0)
    paths = [r.url.path.rsplit("/", 1)[-1] for r in fake.requests]
    assert paths == ["stream", "session", "stream", "session", "stream"]  # the last open finds the recording served


async def test_clock_on_connect_goes_on_when_the_session_cannot_be_read(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeFactory(recording=[frame(0), frame(1)])
    fake.fail("getSession", status=404, code="not_found")
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False, clock_on_connect=True)]
    assert [type(i).__name__ for i in items] == ["FrameItem", "FrameItem"]
    assert "could not read the shared state after connecting" in caplog.text


async def test_resume_after_a_broken_connection(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(6)])
    fake.drop_after(3)
    async with client(fake) as factory:
        items = await collect(factory)
    assert [g for g, *_ in frames_of(items)] == [0, 1, 2, 3, 4, 5]
    assert fake.requests[1].headers["Last-Event-ID"] == "fakeb00t01-3"
    assert fake.streams_opened == fake.streams_closed == 2
    assert len(fake.time.slept) == 1  # one backoff wait after the break
    assert "the frames stream: the connection broke: ReadError: connection dropped by FakeFactory" in caplog.text
    assert "reconnecting in 0.08 s (attempt 1)" in caplog.text


async def test_limit_and_after() -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(5)])
    async with client(fake) as factory:
        items = await collect(factory, limit=2, after=1, max_s=60)
    assert [g for g, *_ in frames_of(items)] == [1, 2, 3, 4]
    first, second = fake.requests[0].url.params, fake.requests[1].url.params
    assert first["after"] == "1"
    assert first["limit"] == "2"
    assert first["max_s"] == "60"
    assert "after" not in second
    assert fake.requests[1].headers["Last-Event-ID"] == "fakeb00t01-3"
    assert fake.time.slept == []  # a connection that reached its limit is reopened at once


async def test_the_sdk_ends_a_connection_at_its_limit() -> None:
    """After `limit` frames the unit sends nothing more but holds the stream about 10 s: the SDK closes it itself."""
    fake = FakeFactory(recording=[frame(g) for g in range(5)], linger=10.0)
    async with client(fake) as factory:
        items = await collect(factory, limit=2)
        ids = [i.id for i in items]
    assert [g for g, *_ in frames_of(items)] == [0, 1, 2, 3, 4]
    resumed = [r.headers.get("Last-Event-ID") for r in stream_requests(fake)]
    assert resumed == [None, ids[1], ids[3], ids[4]]
    assert fake.time.slept == []  # neither the linger nor a backoff was waited for
    assert fake.streams_opened == fake.streams_closed


async def test_without_resume_a_limit_ends_the_stream() -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(5)], linger=10.0)
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False, limit=3)]
    assert [g for g, *_ in frames_of(items)] == [0, 1, 2]
    assert fake.time.slept == []
    assert fake.streams_opened == fake.streams_closed == 1


async def test_a_lingering_stream_is_waited_for_without_a_limit_of_its_own() -> None:
    """The fake's linger is what a unit does after the limit it was asked for (here 2, the SDK counting none)."""
    fake = FakeFactory(recording=[frame(g) for g in range(3)], linger=10.0)
    async with client(fake) as factory:
        stream = await factory.api.stream_frames(limit=2)
        async with stream:
            got = [m async for m in stream]
    assert [m.event for m in got] == ["frame", "frame"]
    assert fake.time.slept == [10.0]


async def test_the_sdk_ends_a_connection_at_its_max_s() -> None:
    """`max_s` is timed with the client's monotonic clock: at the deadline the SDK closes a connection the server keeps
    open, and reconnects at once."""

    async def delay(op: str, n: int) -> None:
        if n == 1 and fake.streams_opened == 1:
            await asyncio.Event().wait()  # the first connection sends one frame, then nothing

    fake = FakeFactory(recording=[frame(g) for g in range(3)], delay=delay)
    ticks = iter([100.0, 100.0, 100.995])  # the connect, the first read, then 5 ms before the deadline
    async with client(fake, monotonic=lambda: next(ticks, 200.0)) as factory:
        items = await collect(factory, max_s=1)
    assert [g for g, *_ in frames_of(items)] == [0, 1, 2]
    streams = stream_requests(fake)
    assert streams[0].url.params["max_s"] == "1"
    assert streams[1].headers["Last-Event-ID"] == items[0].id
    assert fake.time.slept == []
    assert fake.streams_opened == fake.streams_closed


async def test_a_connection_past_its_max_s_without_an_event_backs_off(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(2)])
    ticks = iter([100.0, 101.5])  # the connect, then a first read already past the deadline
    async with client(fake, monotonic=lambda: next(ticks, 200.0)) as factory:
        items = await collect(factory, max_s=1)
    assert [g for g, *_ in frames_of(items)] == [0, 1]
    assert len(stream_requests(fake)) == 3
    assert len(fake.time.slept) == 1
    assert "reached its max_s without an event" in caplog.text


async def test_max_s_with_a_stream_that_ends_or_breaks() -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(4)])
    fake.drop_after(2)
    async with client(fake) as factory:
        items = await collect(factory, max_s=60)
    assert [g for g, *_ in frames_of(items)] == [0, 1, 2, 3]
    assert len(fake.time.slept) == 1  # the break backs off; the normal end reconnects at once


async def test_after_is_kept_until_an_event_id_arrives() -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(4)])
    fake.drop_after(0)  # the first connection breaks before its first event
    async with client(fake) as factory:
        items = await collect(factory, after=2)
    assert [g for g, *_ in frames_of(items)] == [2, 3]
    first, second, third = stream_requests(fake)
    assert first.url.params["after"] == second.url.params["after"] == "2"
    assert "Last-Event-ID" not in second.headers
    assert "after" not in third.url.params
    assert third.headers["Last-Event-ID"] == items[-1].id


async def test_without_resume() -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(4)])
    fake.drop_after(2)
    async with client(fake) as factory:
        got: list[StreamItem] = []
        with pytest.raises(FactoryError) as info:
            async for item in factory.streams.frames(resume=False):
                got.append(item)
    assert info.value.code == "stream_closed"
    assert len(got) == 2


async def test_revisions_and_duplicates() -> None:
    lines = [frame(5), frame(6), frame(5, rev=1), frame(5, rev=1), frame(5, rev=0), frame(7)]
    fake = FakeFactory(recording=lines)
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False)]
    assert frames_of(items) == [(5, 0, False, False), (6, 0, False, False), (5, 1, True, False), (7, 0, False, False)]


async def test_backfill_merges_late_layers_in_order() -> None:
    late = frame(10, missing=["world"], with_world=False)
    lines = [frame(9), late, worker_line(), frame(11), frame(10), frame(12)]
    fake = FakeFactory(recording=lines)
    worker_seen = asyncio.Event()
    items: list[StreamItem] = []

    async def follow() -> None:
        async with gated_client(fake, "/frames/10", worker_seen) as factory:
            async for item in factory.streams.frames(resume=False, backfill=True):
                items.append(item)
                if isinstance(item, WorkerItem):
                    worker_seen.set()  # the backfill read answers only now

    await asyncio.wait_for(follow(), 5)
    kinds = [type(i).__name__ for i in items]
    assert kinds == ["FrameItem", "WorkerItem", "FrameItem", "FrameItem", "FrameItem"]  # side events are not held
    assert frames_of(items) == [
        (9, 0, False, False),
        (10, 0, False, True),
        (11, 0, False, False),
        (12, 0, False, False),
    ]
    backfilled = next(i for i in items if isinstance(i, FrameItem) and i.is_backfill)
    assert backfilled.frame.missing == []
    assert "missing" not in backfilled.frame.raw_json
    read = [r for r in fake.requests if r.url.path.endswith("/frames/10")]
    assert read[0].url.params["layers"] == "world"
    assert read[0].url.params["session"] == "1"
    assert fake.time.slept[0] == pytest.approx(1.5)


async def test_a_backfill_read_is_bounded_and_on_a_fixed_schedule(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(streams_module, "BACKFILL_TIMEOUT", 0.01)
    fake = FakeFactory(recording=[frame(1, missing=["world"], with_world=False), frame(2)])
    async with gated_client(fake, "/frames/1", asyncio.Event()) as factory:
        items = [i async for i in factory.streams.frames(resume=False, backfill=True)]
    assert frames_of(items) == [(1, -1, False, False), (2, 0, False, False)]
    assert fake.time.slept == [1.5, 1.0, 1.0]  # reads due 1.5, 2.5 and 3.5 s after the frame arrived
    assert "no reply within 0.01 s" in caplog.text


async def test_closing_the_client_ends_its_streams(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeFactory(recording=[frame(1), frame(2), frame(3)])
    fake.drop_after(1)
    factory = client(fake)
    got: list[StreamItem] = []
    with pytest.raises(FactoryError, match="the client was closed") as info:
        async for item in factory.streams.frames():
            got.append(item)
            await factory.aclose()
    assert info.value.code == "stream_closed"
    assert len(got) == 1
    assert "reconnecting" not in caplog.text
    with pytest.raises(FactoryError, match="the client is closed") as call:
        await factory.api.get_session()
    assert call.value.code == "stream_closed"


async def test_backfill_reads_with_the_stream_precision() -> None:
    lines = [frame(1, missing=["world"], with_world=False), frame(2), frame(1)]
    fake = FakeFactory(recording=lines)
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False, backfill=True, precision="full")]
    assert frames_of(items)[0] == (1, 0, False, True)
    stream, read = stream_requests(fake), stream_requests(fake, "/frames/1")
    assert stream[0].url.params["precision"] == read[0].url.params["precision"] == "full"


async def test_backfill_gives_up_and_releases(caplog: pytest.LogCaptureFixture) -> None:
    lines = [frame(1, missing=["world"], with_world=False), frame(2)]
    fake = FakeFactory(recording=lines)
    fake.fail("getFrame", status=503, code="busy", times=3)
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False, backfill=True)]
    assert frames_of(items) == [(1, -1, False, False), (2, 0, False, False)]
    assert len([r for r in fake.requests if "/frames/1" in r.url.path]) == 3
    assert fake.time.slept.count(1.0) == 2
    assert "frame g 1 of session 1: world still missing (HTTP 503 busy: injected by FakeFactory)" in caplog.text


async def test_backfill_not_retained_releases_at_once() -> None:
    lines = [frame(1, missing=["world"], with_world=False), frame(2)]
    fake = FakeFactory(recording=lines)
    fake.fail("getFrame", status=404, code="not_retained")
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False, backfill=True)]
    assert frames_of(items) == [(1, -1, False, False), (2, 0, False, False)]
    assert len([r for r in fake.requests if "/frames/1" in r.url.path]) == 1


async def test_backfill_of_some_layers() -> None:
    late = frame(3, missing=["world", "cards"], with_world=False)
    lines = [late, frame(3), frame(4)]
    fake = FakeFactory(recording=lines)
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False, backfill=True)]
    first = items[0]
    assert isinstance(first, FrameItem)
    assert first.is_backfill
    assert first.frame.world is not None
    assert first.frame.missing == ["cards"]
    assert first.frame.raw_json["missing"] == ["cards"]


def test_merge_layers_unknown_and_absent() -> None:
    held = Frame.from_json(frame(1, missing=["world", "sparkle"], with_world=False))
    reply = Frame.from_json(frame(1, with_world=False))
    merged, changed = merge_layers(held, reply)
    assert merged is held
    assert not changed


async def test_a_broken_source_delivers_held_frames_first() -> None:
    lines = [frame(1), frame(2, missing=["world"], with_world=False), frame(3), frame(4)]
    fake = FakeFactory(recording=lines)
    fake.drop_after(3)
    async with client(fake) as factory:
        got: list[StreamItem] = []
        with pytest.raises(FactoryError, match="stream_closed"):
            async for item in factory.streams.frames(resume=False, backfill=True):
                got.append(item)
    assert [g for g, *_ in frames_of(got)] == [1, 2, 3]


async def test_cancel_closes_the_connection() -> None:
    blocked = asyncio.Event()

    async def delay(op: str, n: int) -> None:
        if n == 2:
            blocked.set()
            await asyncio.Event().wait()

    fake = FakeFactory(recording=[frame(g) for g in range(5)], delay=delay)
    async with client(fake) as factory:
        got: list[StreamItem] = []

        async def follow() -> None:
            async for item in factory.streams.frames(backfill=True):
                got.append(item)

        task = asyncio.create_task(follow())
        await blocked.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(got) == 2
    assert fake.streams_opened == fake.streams_closed == 1
    assert fake.viewers == 0


async def test_closing_while_frames_are_held() -> None:
    lines = [frame(1, missing=["world"], with_world=False), frame(2), frame(3)]
    fake = FakeFactory(recording=lines)
    async with client(fake) as factory:
        stream = factory.streams.frames(backfill=True)
        first = await stream.__anext__()
        assert isinstance(first, FrameItem)
        await stream.aclose()
    assert fake.streams_opened == fake.streams_closed


async def test_reconnect_limits_and_errors() -> None:
    fake = FakeFactory(recording=[frame(0)])
    fake.fail("streamFrames", status=503, code="busy", times=100)
    async with client(fake, retries=0) as factory:
        with pytest.raises(FactoryError, match="gave up after 2 reconnects"):
            async for _ in factory.streams.frames(max_reconnects=2):
                pass
        assert len(fake.requests) == 3
        with pytest.raises(FactoryError) as info:
            async for _ in factory.streams.frames(layers="nope"):
                pass
        assert info.value.code == "unknown_layer"


async def test_empty_reconnects_wait_and_give_up() -> None:
    fake = FakeFactory(recording=[frame(0), frame(1)], on_exhausted="end")
    async with client(fake) as factory:
        got: list[StreamItem] = []
        with pytest.raises(FactoryError, match="stream_closed"):
            async for item in factory.streams.frames(max_reconnects=2):
                got.append(item)
    assert len(got) == 2
    assert len(fake.time.slept) == 2  # the third connection without an event gives up at once


async def test_side_events_gap_reset_and_unknown() -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(3)])
    fake.inject("gap", {"gap": 4}, after=1)
    fake.inject("hello", {"x": 1}, after=1)
    fake.inject("reset", {"boot": "other", "cursor": 2}, after=2)
    fake.drop_after(5)  # frame 0, gap, hello, frame 1, reset | frame 2
    async with client(fake) as factory:
        items = await collect(factory)
    assert [i.count for i in items if isinstance(i, GapItem)] == [4]
    assert [(i.boot, i.cursor) for i in items if isinstance(i, ResetItem)] == [("other", 2), ("fakeb00t01", 3)]
    assert fake.requests[1].headers["Last-Event-ID"] == "other-2"
    assert [g for g, *_ in frames_of(items)] == [0, 1, 2]  # the frames replayed after the second reset are dropped


async def test_an_event_that_does_not_decode_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """2.3 rule 9: a frame or side event that is not its type's shape is skipped with a warning; the stream goes on."""
    fake = FakeFactory(recording=[frame(0), frame(1)])
    fake.inject("frame", {"g": "nope"}, after=1)
    fake.inject("worker", {"state": 5}, after=1)
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False)]
    assert [g for g, *_ in frames_of(items)] == [0, 1]
    assert not any(isinstance(i, WorkerItem) for i in items)
    skipped = [r.getMessage() for r in caplog.records if "does not decode" in r.getMessage()]
    assert [m.split(" (")[0] for m in skipped] == [
        "the frames stream: skipped a frame event that does not decode",
        "the frames stream: skipped a worker event that does not decode",
    ]


async def test_the_id_of_an_event_that_does_not_decode_counts_for_the_resume() -> None:
    """2.3 rule 9: the resume id moves past an event that does not decode, so a reconnect does not read it again."""
    asked: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/session/frames/stream"):
            return httpx.Response(404, json={"error": "not here", "code": "not_found"})
        asked.append(request.headers.get("Last-Event-ID"))
        if len(asked) == 1:  # a frame, then an event that does not decode, and the server ends the stream
            body = f"id: b-1\nevent: frame\ndata: {json.dumps(frame(0))}\n\nid: b-2\nevent: frame\ndata: {{nope\n\n"
        else:
            body = f"id: b-3\nevent: frame\ndata: {json.dumps(frame(1))}\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body.encode())

    t = FakeTime()
    options: dict[str, Any] = {"rng": random.Random(7), "sleep": t.sleep, "monotonic": t.monotonic}
    options["wall_clock"] = t.time
    got: list[int] = []
    transport = httpx.MockTransport(handle)
    async with (
        FactoryClient(BASE_URL, transport=transport, **options) as factory,
        factory.streams.frames(resume=True, max_reconnects=2) as items,
    ):
        async for item in items:
            if isinstance(item, FrameItem):
                got.append(item.frame.g)
                if len(got) == 2:
                    break
    assert got == [0, 1]
    assert asked[:2] == [None, "b-2"]


async def test_long_poll() -> None:
    lines = [clock_line(), worker_line(), *[frame(g) for g in range(10)]]
    fake = FakeFactory(recording=lines)
    fake.fail("pollFrames", status=503, code="busy", times=4)
    async with client(fake) as factory:
        got: list[StreamItem] = []
        with pytest.raises(FakeExhausted):
            async for item in factory.streams.poll("world", cid="viewer-01", limit=4, wait=0):
                got.append(item)
                if len([i for i in got if isinstance(i, FrameItem)]) == 5:
                    await factory.session.pause()
    kinds = [type(i).__name__ for i in got]
    assert kinds[:2] == ["ClockItem", "WorkerItem"]
    assert kinds.count("ClockItem") == 2  # the pause changed the state's revision
    assert [g for g, *_ in frames_of(got)] == list(range(10))
    polls = [r for r in fake.requests if r.url.path.endswith("/session/frames")]
    assert polls[-1].url.params["cursor"] == "10"
    assert polls[0].url.params["cid"] == "viewer-01"


async def test_long_poll_counts_as_a_viewer_unless_it_observes() -> None:
    """A long-poll counts as a viewer only with a client id: the SDK sends a random one (the same for the whole stream)
    unless the stream observes."""
    fake = FakeFactory(recording=[frame(g) for g in range(6)])
    async with client(fake) as factory:
        with pytest.raises(FakeExhausted):
            async for _ in factory.streams.poll(limit=2):
                pass
        sent = {r.url.params.get("cid") for r in stream_requests(fake, "/session/frames")}
        assert len(sent) == 1
        cid = sent.pop()
        assert cid is not None
        assert re.fullmatch(r"sdk-python-[0-9a-f]{12}", cid)
        assert fake.cids == {cid}
        with pytest.raises(FakeExhausted):
            async for _ in factory.streams.poll(observe=True):
                pass
    assert "cid" not in fake.requests[-1].url.params
    fake = FakeFactory(recording=[frame(0)])
    async with client(fake, observe=True) as factory:
        with pytest.raises(FakeExhausted):
            async for _ in factory.streams.poll():
                pass
    assert all("cid" not in r.url.params for r in fake.requests)
    assert fake.cids == set()


async def test_long_poll_errors_and_idle_waits() -> None:
    fake = FakeFactory(recording=[frame(0)], on_exhausted="end")
    async with client(fake) as factory:
        with pytest.raises(FactoryError) as info:
            async for _ in factory.streams.poll(cid="bad"):
                pass
        assert info.value.field == "cid"
        stream = factory.streams.poll(wait=250)
        items = [await stream.__anext__() for _ in range(3)]
        await stream.aclose()
    assert [type(i).__name__ for i in items] == ["ClockItem", "WorkerItem", "FrameItem"]


async def test_raw_packets_sse_and_ndjson() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"))
    fake.drop_after(3, operation="rawStream")
    async with client(fake) as factory:
        sse: list[Any] = []
        with pytest.raises(FakeExhausted):
            async for item in factory.streams.raw_packets(after=0):
                sse.append(item)
        packets = [i for i in sse if isinstance(i, PacketItem)]
        assert len(packets) == 51
        assert packets[0].id is not None
        assert packets[0].packet.schema == "sip1"
        first = [r for r in fake.requests if r.url.path.endswith("/raw/stream")]
        assert first[0].url.params["after"] == "0"
        assert first[1].headers["Last-Event-ID"] in {p.id for p in packets[:3]}

    fake = FakeFactory(recording=recording("dry1-0296-0306-full"))
    fake.drop_after(2, operation="rawStream")
    async with client(fake) as factory:
        nd: list[Any] = []
        with pytest.raises(FakeExhausted):
            async for item in factory.streams.raw_packets(format="ndjson", observe=True):
                nd.append(item)
    assert len(nd) == 51
    reopened = [r for r in fake.requests if r.url.path.endswith("/raw/stream")][1]
    assert reopened.url.params["after"] == str(nd[1].packet.cursor)
    assert reopened.url.params["observe"] == "1"


async def spin_until(condition: Any, turns: int = 10_000) -> None:
    """Let the event loop run until `condition()` holds (no real time passes)."""
    for _ in range(turns):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


def made_backfill_read(fake: FakeFactory, g: int) -> bool:
    return any(r.url.path.endswith(f"/frames/{g}") for r in fake.requests)


async def test_the_stream_ends_while_a_frame_is_held() -> None:
    """The source ends (and a duplicate arrives) while a frame waits for its backfill: both are handled in order."""
    gate = asyncio.Event()
    lines = [frame(1), frame(2, missing=["world"], with_world=False), frame(1), frame(2)]
    fake = FakeFactory(recording=lines)

    async def sleep(seconds: float) -> None:
        if seconds == 1.5:
            await gate.wait()
        await fake.time.sleep(seconds)

    async def open_gate() -> None:
        await spin_until(lambda: fake.streams_closed == 1)
        for _ in range(50):
            await asyncio.sleep(0)  # the stream has ended; the frame is still held
        gate.set()

    async with FactoryClient("http://fake.test", transport=fake, sleep=sleep, monotonic=fake.time.monotonic) as f:
        opener = asyncio.create_task(open_gate())
        items = [i async for i in f.streams.frames(resume=False, backfill=True)]
        await opener
    assert frames_of(items) == [(1, 0, False, False), (2, 0, False, True)]


async def test_a_held_frame_completes_before_the_next_item() -> None:
    lines = [frame(1, missing=["world"], with_world=False), frame(2), frame(1), frame(3)]
    fake = FakeFactory(recording=lines)

    async def delay(op: str, n: int) -> None:
        if n == 1:  # hold the second frame back until the first one's backfill was read
            await spin_until(lambda: made_backfill_read(fake, 1))
            for _ in range(50):
                await asyncio.sleep(0)

    fake.delay = delay
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False, backfill=True)]
    assert frames_of(items) == [(1, 0, False, True), (2, 0, False, False), (3, 0, False, False)]


async def test_closing_with_a_read_in_flight() -> None:
    lines = [frame(1, missing=["world"], with_world=False), frame(2), frame(1)]
    fake = FakeFactory(recording=lines)

    async def delay(op: str, n: int) -> None:
        if n == 1:
            await asyncio.Event().wait()  # the second frame never comes

    fake.delay = delay
    async with client(fake) as factory:
        stream = factory.streams.frames(backfill=True)
        first = await stream.__anext__()
        await stream.aclose()
    assert isinstance(first, FrameItem)
    assert first.is_backfill
    assert fake.streams_opened == fake.streams_closed == 1


async def test_backfill_starts_at_once_when_it_is_late() -> None:
    lines = [frame(1, missing=["world"], with_world=False), frame(2), frame(1)]
    fake = FakeFactory(recording=lines)
    ticks = iter(range(0, 1000, 2))
    async with FactoryClient(
        "http://fake.test", transport=fake, sleep=fake.time.sleep, monotonic=lambda: float(next(ticks))
    ) as factory:
        items = [i async for i in factory.streams.frames(resume=False, backfill=True)]
    assert frames_of(items)[0] == (1, 0, False, True)
    assert 1.5 not in fake.time.slept


def test_delivered_keeps_a_bounded_window() -> None:
    from pdigger_factory_sdk import FrameItem as Item

    window = _Delivered(size=2)
    a, b, c = (Item(frame=Frame.from_json(frame(g))) for g in (1, 2, 3))
    assert window.deliver(a) is a
    assert window.deliver(b) is b
    assert window.deliver(c) is c
    assert window.deliver(a) is a  # forgotten: older than the window
    assert window.deliver(c) is None

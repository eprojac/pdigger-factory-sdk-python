"""The server-sent events parser and `AsyncStream`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from pdigger_factory_sdk import AsyncStream, FactoryError
from pdigger_factory_sdk._sse import LineSplitter, SseEvent, SseParser


def parse(*chunks: bytes) -> list[SseEvent]:
    parser = SseParser()
    out: list[SseEvent] = []
    for chunk in chunks:
        out += parser.feed(chunk)
    parser.close()
    return out


def test_fields_and_dispatch() -> None:
    events = parse(b'id: b-1\nevent: frame\ndata: {"g": 1}\n\n: a comment\ndata: x\ndata: y\n\n')
    assert events == [SseEvent("frame", '{"g": 1}', "b-1"), SseEvent("message", "x\ny", "b-1")]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_line_ends(newline: bytes) -> None:
    text = newline.join([b"event: clock", b"data: 1", b"", b"data: 2", b"", b""])
    assert [e.data for e in parse(text)] == ["1", "2"]


def test_crlf_split_between_chunks() -> None:
    assert [e.data for e in parse(b"data: a\r", b"\n\r", b"\ndata: b\r\n\r\n")] == ["a", "b"]


def test_cr_at_chunk_end_followed_by_text() -> None:
    assert [e.data for e in parse(b"data: a\r", b"\r", b"data: b\n\n")] == ["a", "b"]


def test_bom_utf8_and_split_characters() -> None:
    raw = "﻿data: café\n\n".encode()
    assert [e.data for e in parse(raw[:2], raw[2:5], raw[5:12], raw[12:])] == ["café"]
    assert parse(b"", b"data: \xc3", b"\xa9\n\n")[0].data == "é"


def test_field_rules() -> None:
    parser = SseParser()
    events = parser.feed(b"data\n\nretry: 1500\nretry: soon\nid: a\0b\nid\ndata:no space\n\nfoo: bar\n\n")
    assert [e.data for e in events] == ["", "no space"]
    assert events[1].id is None
    assert parser.retry == 1500
    assert parser.last_id is None


def test_id_persists_and_unfinished_event_is_dropped() -> None:
    parser = SseParser()
    assert parser.feed(b"id: 7\ndata: a\n\ndata: b\n\n")[1].id == "7"
    assert parser.feed(b"data: never ended") == []
    parser.close()
    assert parser.feed(b"\n\n") == []


def test_the_resume_id_is_the_last_dispatched_one() -> None:
    parser = SseParser()
    assert [e.id for e in parser.feed(b"id: a-1\ndata: x\n\nid: a-2\ndata: y\n")] == ["a-1"]
    assert parser.last_id == "a-1"  # the event with id a-2 was cut off before its blank line
    parser.close()
    assert parser.feed(b"data: z\n\n")[0].id == "a-1"


def test_lines_and_events_are_bounded() -> None:
    with pytest.raises(FactoryError, match="a line of the stream is longer than 8 characters") as info:
        SseParser(limit=8).feed(b"data: 123\n")
    assert info.value.code == "decode"
    parser = SseParser(limit=8)
    assert parser.feed(b"data: 12") == []
    with pytest.raises(FactoryError, match="a line of the stream"):
        parser.feed(b"3")  # an unfinished line past the limit
    with pytest.raises(FactoryError, match="an event of the stream is longer than 8 characters"):
        SseParser(limit=8).feed(b"data:123\ndata:123\ndata:123\n")


def test_line_splitter() -> None:
    lines = LineSplitter()
    assert lines.push("a") == []
    assert lines.push("b\r") == ["ab"]
    assert lines.push("\nc\r\nd") == ["c"]
    lines.close()
    assert lines.push("e\n") == ["e"]


async def numbers(n: int, closed: list[bool]) -> AsyncGenerator[int, None]:
    try:
        for i in range(n):
            yield i
    finally:
        closed.append(True)


async def test_async_stream_take_and_close() -> None:
    closed: list[bool] = []
    stream = AsyncStream(numbers(10, closed))
    assert await stream.take(3) == [0, 1, 2]
    assert closed == [True]
    assert [x async for x in stream] == []


async def test_async_stream_take_more_than_there_is() -> None:
    closed: list[bool] = []
    assert await AsyncStream(numbers(2, closed)).take(5) == [0, 1]
    assert closed == [True]


async def test_async_stream_closed_from_another_task() -> None:
    closed: list[bool] = []

    async def first_then_nothing() -> AsyncGenerator[int, None]:
        try:
            yield 0
            await asyncio.Event().wait()
            yield 1
        finally:
            closed.append(True)

    stream = AsyncStream(first_then_nothing())
    got: list[int] = []

    async def loop() -> None:
        async for x in stream:
            got.append(x)

    task = asyncio.ensure_future(loop())
    for _ in range(100):
        await asyncio.sleep(0)
    assert got == [0]
    await stream.aclose()  # while the loop waits for its next item
    await asyncio.wait_for(task, 5)
    assert got == [0]
    assert closed == [True]
    assert [x async for x in stream] == []


async def test_async_stream_context_manager() -> None:
    closed: list[bool] = []
    async with AsyncStream(numbers(5, closed)) as stream:
        assert stream.__aiter__() is stream
        assert await stream.__anext__() == 0
    assert closed == [True]

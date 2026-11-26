"""Replay: local files and URLs, plain and gzip (multi-member too), lazily read headers, pacing, errors."""

from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from pdigger_factory_sdk import FactoryError, InterpreterCore, Replay
from pdigger_factory_sdk.replay import RecordingHeader, _lines, classify_line
from pdigger_factory_sdk.testing import FakeFactory, FakeTime, use_fake

from .support import golden, golden_summary, recording, text_of
from .synth import HEADER, clock_line, ev, frame

NAME = "joined-mid-move"


def plain_text(name: str) -> bytes:
    return gzip.decompress(recording(name).read_bytes())


def write_lines(path: Path, lines: list[object]) -> Path:
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def test_plain_file(tmp_path: Path) -> None:
    path = tmp_path / f"{NAME}.ndjson"
    path.write_bytes(plain_text(NAME))
    replay = Replay(path)
    assert text_of(replay.events()) == golden(NAME, "events.txt")
    header = replay.header
    assert header is not None
    assert header.recording == "pfr1"
    assert header.video == "dry1"
    assert header.source is not None
    assert header.frames == 22
    assert replay.header is header


def test_lines_frames_and_odd_content(tmp_path: Path) -> None:
    path = tmp_path / "odd.ndjson"
    path.write_text(
        "\n".join(
            [
                json.dumps({**HEADER, "source": {"id": 5}}),
                "not json",
                "[1, 2]",
                "",
                json.dumps(clock_line()),
                json.dumps({"sse": "clock", "data": {"bad": True}}),
                json.dumps({"recording": "pfr1", "note": "a second header is just a line"}),
                json.dumps(frame(1, events=[ev("new", 1)])),
                json.dumps(frame(1, events=[ev("new", 1)])),
                json.dumps({"future": "line"}),
            ]
        ),
        encoding="utf-8",
    )
    replay = Replay.open(path)
    assert len(list(replay.lines())) == 7
    assert [f.g for f in replay.frames()] == [1, 1]  # duplicates are passed on as recorded
    assert [e.describe() for e in replay.events()] == ["0:00.2 stone 1 appears"]
    header = replay.header
    assert header is not None
    assert header.source is None  # a malformed source entry is left out
    assert header.layers == ["world"]


def test_header_is_none_without_one(tmp_path: Path) -> None:
    path = write_lines(tmp_path / "bare.ndjson", [frame(1), frame(2)])
    assert Replay(path).header is None
    assert Replay(write_lines(tmp_path / "notes.ndjson", [{"comment": 1}])).header is None
    path = write_lines(tmp_path / "late.ndjson", [{"comment": 1}, HEADER, frame(1)])
    assert Replay(path).header is None  # only the first line that is not blank is a header (spec 6)
    assert [f.g for f in Replay(path).frames()] == [1]
    blank = tmp_path / "blank.ndjson"
    blank.write_text("\n \n", encoding="utf-8")  # no line at all that is not blank
    assert Replay(write_lines(tmp_path / "empty.ndjson", [])).header is None
    assert Replay(blank).header is None


def test_a_frame_line_that_does_not_decode_is_skipped(tmp_path: Path) -> None:
    """Spec 6: readers ignore lines they do not understand, frame lines that do not decode too; the final update
    comes."""
    lines: list[object] = [
        HEADER,
        frame(1, events=[ev("new", 1)]),
        {"g": 2, "cursor": 3},
        {**frame(3), "g": "x"},
        frame(4),
    ]
    path = write_lines(tmp_path / "bad.ndjson", lines)
    assert [f.g for f in Replay(path).frames()] == [1, 4]
    updates = list(Replay(path).updates())
    assert [(u.frame.g if u.frame else None, u.final) for u in updates] == [(1, False), (4, False), (None, True)]
    assert Replay(path).summary().frames == 2


def test_classify_line() -> None:
    assert classify_line([1], False) == "other"
    assert classify_line({"recording": "pfr1"}, False) == "header"
    assert classify_line({"recording": "pfr1"}, True) == "other"
    assert classify_line({"sse": "gap"}, True) == "side"
    assert classify_line({"g": 1, "cursor": 1}, True) == "frame"


def test_header_model() -> None:
    header = RecordingHeader.from_json({"recording": "pfr1", "complete": {"frames": 9}, "sessions": [1, 2]})
    assert header.complete == {"frames": 9}
    assert header.sessions == [1, 2]


def test_line_splitting_and_gzip_detection() -> None:
    gz = gzip.compress(b'{"a": 1}\n{"b"') + gzip.compress(b": 2}\n")
    assert list(_lines([gz[:1], gz[1:7], gz[7:]])) == [b'{"a": 1}', b'{"b": 2}']
    assert list(_lines([b"{", b'"x": 1}'])) == [b'{"x": 1}']
    assert list(_lines([b"{"])) == [b"{"]  # shorter than the gzip magic: plain
    assert list(_lines([])) == []
    with pytest.raises(FactoryError, match="bad gzip data"):
        list(_lines([b"\x1f\x8bnot gzip at all"]))
    with pytest.raises(FactoryError, match="the gzip data ends early"):
        list(_lines([gz[:-9]]))
    with pytest.raises(FactoryError, match="the gzip data ends early"):
        list(_lines([b"\x1f\x8b"]))


def truncated(name: str) -> bytes:
    data = recording(name).read_bytes()
    return data[: len(data) * 2 // 3]


def test_a_truncated_file_is_a_decode_error(tmp_path: Path) -> None:
    path = tmp_path / "cut.ndjson.gz"
    path.write_bytes(truncated(NAME))
    with pytest.raises(FactoryError) as info:
        Replay(path).summary()
    assert info.value.code == "decode"
    assert info.value.message == "recording: the gzip data ends early (the file is truncated)"


async def test_a_truncated_file_is_a_decode_error_async(tmp_path: Path) -> None:
    path = tmp_path / "cut.ndjson.gz"
    path.write_bytes(truncated(NAME))
    with pytest.raises(FactoryError, match="the gzip data ends early"):
        await Replay(path).asummary()
    path.write_bytes(b"\x1f\x8b\x08not gzip")
    with pytest.raises(FactoryError, match="bad gzip data"):
        await Replay(path).asummary()
    with pytest.raises(FileNotFoundError):
        await Replay(tmp_path / "nowhere.ndjson").asummary()


async def test_a_truncated_download_is_a_decode_error() -> None:
    fake = FakeFactory(files={"/cut.ndjson.gz": truncated(NAME)})
    replay = Replay("http://data.test/cut.ndjson.gz", transport=fake)
    with pytest.raises(FactoryError, match="the gzip data ends early"):
        replay.summary()
    with pytest.raises(FactoryError, match="the gzip data ends early"):
        await replay.asummary()


def test_files_shorter_than_the_gzip_magic(tmp_path: Path) -> None:
    path = tmp_path / "tiny.ndjson"
    path.write_bytes(b"x")
    assert list(Replay(path).lines()) == []
    assert Replay(path).summary().frames == 0


async def test_files_shorter_than_the_gzip_magic_async(tmp_path: Path) -> None:
    path = tmp_path / "tiny.ndjson"
    path.write_bytes(b"7")
    assert (await Replay(path).asummary()).frames == 0
    fake = FakeFactory(files={"/tiny": b"7"})
    assert (await Replay("http://data.test/tiny", transport=fake).asummary()).frames == 0


class _Unreachable(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """A transport whose every request fails as a refused connection does."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)


class _Body(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A body that sends `head`, then breaks (`error`), or ends."""

    def __init__(self, head: bytes, error: Exception | None = None) -> None:
        self._head = head
        self._error = error
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield self._head
        if self._error is not None:
            raise self._error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for i in range(0, len(self._head), 4096):
            yield self._head[i : i + 4096]
        if self._error is not None:
            raise self._error

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


def _replies(status: int, body: _Body, headers: dict[str, str] | None = None) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, headers=headers, stream=body))


async def test_url_failures_are_factory_errors() -> None:
    url = "http://data.test/r.ndjson.gz"
    with pytest.raises(FactoryError) as info:
        Replay(url, transport=_Unreachable()).summary()
    assert (info.value.code, info.value.status) == ("transport", None)
    assert info.value.message == f"GET {url}: ConnectError: refused"
    with pytest.raises(FactoryError) as info:
        await Replay(url, transport=_Unreachable()).asummary()
    assert info.value.code == "transport"
    broken = _replies(200, _Body(recording(NAME).read_bytes()[:500], httpx.ReadError("reset")))
    with pytest.raises(FactoryError, match="ReadError: reset") as info:
        Replay(url, transport=broken).summary()
    assert info.value.code == "transport"
    with pytest.raises(FactoryError, match="ReadError: reset"):
        await Replay(url, transport=broken).asummary()
    undecodable = _replies(200, _Body(b"not gzip"), {"Content-Encoding": "gzip"})
    with pytest.raises(FactoryError) as info:
        Replay(url, transport=undecodable).summary()
    assert info.value.code == "decode"
    with pytest.raises(FactoryError) as info:
        await Replay(url, transport=undecodable).asummary()
    assert info.value.code == "decode"


async def test_an_error_reply_whose_body_breaks_keeps_its_status() -> None:
    url = "http://data.test/r.ndjson.gz"
    transport = _replies(503, _Body(b'{"co', httpx.ReadError("gone")))
    with pytest.raises(FactoryError) as info:
        Replay(url, transport=transport).summary()
    assert (info.value.code, info.value.status) == ("not_ready", 503)
    with pytest.raises(FactoryError) as info:
        await Replay(url, transport=transport).asummary()
    assert (info.value.code, info.value.status) == ("not_ready", 503)


def multi_member(name: str) -> bytes:
    text = plain_text(name)
    cut = len(text) // 2
    return gzip.compress(text[:cut]) + gzip.compress(text[cut:])


def test_url_sync() -> None:
    fake = FakeFactory(files={"/data/x.ndjson.gz": multi_member(NAME)})
    replay = Replay("http://data.test/data/x.ndjson.gz", transport=fake, headers={"X-Team": "a"})
    result = replay.interpret()
    assert text_of(result.events) == golden(NAME, "events.txt")
    assert result.summary.to_dict() == golden_summary(NAME)
    assert fake.requests[0].headers["User-Agent"].startswith("pdigger-factory-sdk-python/")
    assert fake.requests[0].headers["X-Team"] == "a"
    with pytest.raises(FactoryError) as info:
        list(Replay("http://data.test/nope.ndjson", transport=fake).lines())
    assert info.value.status == 404


async def test_url_async() -> None:
    fake = FakeFactory(files={"/x.ndjson": plain_text(NAME)})
    replay = Replay("https://data.test/x.ndjson", transport=fake)
    header = await replay.aheader()
    assert header is not None
    assert header.frames == 22
    assert await replay.aheader() is header
    assert replay.header is header
    assert len(fake.requests) == 1
    assert text_of([e async for e in replay.aevents()]) == golden(NAME, "events.txt")
    assert text_of([a async for a in replay.aactivities()]) == golden(NAME, "activities.txt")
    assert (await replay.asummary()).to_dict() == golden_summary(NAME)
    assert len([f async for f in replay.aframes()]) == 22
    with pytest.raises(FactoryError) as info:
        await Replay("https://data.test/missing", transport=fake).asummary()
    assert info.value.code == "not_found"


def _moved(body: bytes) -> httpx.MockTransport:
    """`/old.ndjson` has moved to `/new.ndjson`; any other path answers 304 (a 3xx that is not a move)."""

    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old.ndjson":
            return httpx.Response(302, headers={"Location": "http://data.test/new.ndjson"})
        if request.url.path == "/new.ndjson":
            return httpx.Response(200, content=body)
        return httpx.Response(304)

    return httpx.MockTransport(reply)


def test_a_moved_recording_is_followed() -> None:
    transport = _moved(plain_text(NAME))
    assert Replay("http://data.test/old.ndjson", transport=transport).summary().to_dict() == golden_summary(NAME)
    with pytest.raises(FactoryError) as info:
        Replay("http://data.test/same.ndjson", transport=transport).summary()  # never zero frames
    assert info.value.status == 304


async def test_a_moved_recording_is_followed_async() -> None:
    transport = _moved(plain_text(NAME))
    summary = await Replay("http://data.test/old.ndjson", transport=transport).asummary()
    assert summary.to_dict() == golden_summary(NAME)
    with pytest.raises(FactoryError) as info:
        await Replay("http://data.test/same.ndjson", transport=transport).asummary()
    assert info.value.status == 304


def test_the_summary_keeps_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """summary() counts as it reads: it never collects the events, activities and trays that interpret() keeps."""

    def collect(self: Replay) -> None:
        raise AssertionError("summary() went through interpret()")

    monkeypatch.setattr(Replay, "interpret", collect)
    assert Replay(recording(NAME)).summary().to_dict() == golden_summary(NAME)


async def test_realtime_pacing(tmp_path: Path) -> None:
    path = write_lines(
        tmp_path / "paced.ndjson",
        [HEADER, frame(0), frame(5), frame(10), frame(3, session=2), frame(8, session=2)],
    )
    t = FakeTime()
    replay = Replay(path, sleep=t.sleep, monotonic=t.monotonic)
    frames = [f.g async for f in replay.aframes(pace="realtime", speed=2.0)]
    assert frames == [0, 5, 10, 3, 8]
    assert t.slept == [pytest.approx(0.5), pytest.approx(0.5), pytest.approx(0.5)]
    updates = [u async for u in replay.aupdates(pace="realtime")]
    assert updates[-1].final
    with pytest.raises(ValueError, match="pace must be None or 'realtime'"):
        replay.aframes(pace="fast")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="speed must be above 0"):
        replay.aevents(speed=0)


async def test_realtime_pacing_never_waits_for_an_infinite_time(tmp_path: Path) -> None:
    # 1e400 reads as infinity (SDK-DESIGN.md 2.3): that frame comes at once, the next one when its time comes
    lines = [json.dumps(x) for x in (HEADER, frame(0), frame(5, t=123.25), frame(10))]
    path = tmp_path / "far.ndjson"
    path.write_text("\n".join(lines).replace("123.25", "1e400") + "\n")
    t = FakeTime()
    replay = Replay(path, sleep=t.sleep, monotonic=t.monotonic)
    assert [f.g async for f in replay.aframes(pace="realtime")] == [0, 5, 10]
    assert t.slept == [pytest.approx(2.0)]


async def test_defaults_from_use_fake() -> None:
    fake = FakeFactory(files={"/r.ndjson.gz": recording(NAME).read_bytes()})
    with use_fake(fake):
        replay = Replay("http://data.test/r.ndjson.gz")
        events = [e async for e in replay.aevents(pace="realtime", speed=100.0)]
    assert text_of(events) == golden(NAME, "events.txt")
    assert fake.time.slept


async def test_aheader_reads_only_the_first_lines(tmp_path: Path) -> None:
    bare = write_lines(tmp_path / "bare.ndjson", [frame(1), frame(2)])
    assert await Replay(bare).aheader() is None
    late = write_lines(tmp_path / "late.ndjson", [{"comment": 1}, HEADER, frame(1)])
    assert await Replay(late).aheader() is None  # only the first line that is not blank is a header (spec 6)
    first = await Replay(write_lines(tmp_path / "first.ndjson", [HEADER, frame(1)])).aheader()
    assert first is not None
    assert first.build == "synthetic"
    assert await Replay(write_lines(tmp_path / "notes.ndjson", [{"comment": 1}])).aheader() is None
    assert await Replay(write_lines(tmp_path / "empty.ndjson", [])).aheader() is None  # no line at all


def test_one_pass_gives_events_and_the_summary() -> None:
    core = InterpreterCore()
    replay = Replay(recording(NAME))
    assert text_of(replay.events(core=core)) == golden(NAME, "events.txt")
    assert core.summary().to_dict() == golden_summary(NAME)
    core = InterpreterCore()
    assert text_of(replay.activities(core=core)) == golden(NAME, "activities.txt")
    assert core.summary().to_dict() == golden_summary(NAME)


async def test_one_pass_gives_events_and_the_summary_async() -> None:
    core = InterpreterCore()
    replay = Replay(recording(NAME))
    assert text_of([e async for e in replay.aevents(core=core)]) == golden(NAME, "events.txt")
    assert core.summary().to_dict() == golden_summary(NAME)
    core = InterpreterCore()
    assert text_of([a async for a in replay.aactivities(core=core)]) == golden(NAME, "activities.txt")
    assert core.summary().to_dict() == golden_summary(NAME)
    core = InterpreterCore()
    updates = [u async for u in replay.aupdates(core=core)]
    assert updates[-1].final
    assert core.summary().frames == 22


async def test_the_event_loop_runs_during_an_async_replay(tmp_path: Path) -> None:
    """Local files are read in a worker thread and the loop gets a turn after every frame."""
    path = tmp_path / "odd.ndjson"
    path.write_bytes(plain_text(NAME) + b"[1]\nnot json\n")
    ticks = 0
    done = False

    async def ticker() -> None:
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    frames = [f async for f in Replay(path).aframes()]
    done = True
    await task
    assert len(frames) == 22
    assert ticks >= len(frames)


async def test_closing_an_async_replay_early_closes_the_download() -> None:
    body = _Body(recording(NAME).read_bytes())
    replay = Replay("http://data.test/x.ndjson.gz", transport=_replies(200, body))
    async with replay.aframes() as frames:
        first = await frames.__anext__()
        assert not body.closed
    assert first.g == 399
    assert body.closed

"""High level: the Interpreter over a recorded stream file, offline (SDK-DESIGN.md sections 2.5, 6).

A recording (`pfr1`) is NDJSON, optionally gzip: a header line, then every frame exactly as the API sent it and the
stream's side events (`{"sse": "clock" | "worker" | "gap" | "reset", "data": {...}, "recv": epoch}`) in the order
received. `Replay` reads local files or HTTP(S) URLs (streamed, never loaded whole):

    core = InterpreterCore()
    for event in Replay.open("dry1.world.ndjson.gz").events(core=core):
        print(event.describe())
    print(core.summary().describe())

Every iteration reads the file again from its start, with a fresh `InterpreterCore` unless you pass one (`core=`, then
its summary is the pass's). The async variants (`aupdates`, `aevents`, ...) read without blocking the event loop (a
local file is read and decompressed in a worker thread, and the loop gets a turn after every frame) and can pace frames
at their media times (`pace="realtime"`, divided by `speed`).

Every failure is a `FactoryError`: `transport` when a URL cannot be read, `decode` for data that is not gzip or ends
early (a truncated file), the API's codes for an HTTP error reply. A local file that cannot be opened raises the usual
`OSError` (`FileNotFoundError`, ...).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import zlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator, Iterable, Iterator
from dataclasses import dataclass, field
from typing import IO, Any, Literal

import httpx

from ._defaults import DEFAULTS
from ._http import http_error, loads
from ._parse import P, as_arr, as_int, as_obj, as_str
from ._stream import AsyncStream
from ._version import USER_AGENT
from .errors import FactoryError
from .interpreter import Activity, Event, InterpreterCore, Summary, Update
from .models import ClockEvent, Frame, GapEvent, ResetEvent, Source
from .trays import Tray, TrayFact, TrayReport, TraySpeckle

__all__ = ["RecordingHeader", "Replay", "ReplayResult", "classify_line"]

_GZIP_MAGIC = b"\x1f\x8b"
_CHUNK = 1 << 16
"""Bytes read from a local file at a time."""


@dataclass(slots=True, kw_only=True)
class RecordingHeader:
    """Line 1 of a `pfr1` recording: what was recorded, from where, and what was redacted."""

    recording: str
    """The format, `pfr1`."""
    name: str | None = None
    video: str | None = None
    source: Source | None = None
    """The catalog entry of the recorded source."""
    build: str | None = None
    api: str | None = None
    model: str | None = None
    backend: str | None = None
    layers: list[str] = field(default_factory=list)
    redacted: list[str] = field(default_factory=list)
    """The fields removed from a public recording (face keypoints, appearance masks, depth textures)."""
    recorded_from: str | None = None
    recorded_at: str | None = None
    frames: int | None = None
    g_first: int | None = None
    g_last: int | None = None
    sessions: list[int] = field(default_factory=list)
    notes: str | None = None
    complete: dict[str, Any] | None = None
    raw_json: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_json(cls, data: Any) -> RecordingHeader:
        """Parse a header line (a malformed `source` entry is left out)."""
        p = P(data, "RecordingHeader")
        try:
            source = p.opt("source", Source.from_json)
        except FactoryError:
            source = None
        return cls(
            recording=p.req("recording", as_str),
            name=p.opt("name", as_str),
            video=p.opt("video", as_str),
            source=source,
            build=p.opt("build", as_str),
            api=p.opt("api", as_str),
            model=p.opt("model", as_str),
            backend=p.opt("backend", as_str),
            layers=p.opt("layers", as_arr, []),
            redacted=p.opt("redacted", as_arr, []),
            recorded_from=p.opt("recorded_from", as_str),
            recorded_at=p.opt("recorded_at", as_str),
            frames=p.opt("frames", as_int),
            g_first=p.opt("g_first", as_int),
            g_last=p.opt("g_last", as_int),
            sessions=p.opt("sessions", as_arr, []),
            notes=p.opt("notes", as_str),
            complete=p.opt("complete", as_obj),
            raw_json=p.o,
        )


@dataclass(slots=True, frozen=True)
class ReplayResult:
    """One pass over a recording: its header, every event, activity and tray fact in output order, the summary, every
    tray, and the unit's speckle answers read (SDK-DESIGN.md 4.6.12)."""

    header: RecordingHeader | None
    events: list[Event]
    activities: list[Activity]
    summary: Summary
    tray_facts: list[TrayFact] = field(default_factory=list)
    trays: list[Tray] = field(default_factory=list)
    speckle: list[TraySpeckle] = field(default_factory=list)


def classify_line(obj: Any, have_header: bool) -> Literal["header", "side", "frame", "other"]:
    """What a decoded recording line is by its keys (SDK-DESIGN.md section 6): the header (an object with `recording`,
    when `have_header` is false: pass false only for the first line that is not blank), a side event (`sse`), a frame
    (`g` and `cursor`), or something a reader ignores. Whether a header, side or frame line decodes is not checked."""
    if not isinstance(obj, dict):
        return "other"
    if "recording" in obj and not have_header:
        return "header"
    if "sse" in obj:
        return "side"
    if "g" in obj and "cursor" in obj:
        return "frame"
    return "other"


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


_BLANK = object()  # a blank line, which is no line of the file


def _decode_line(raw: bytes) -> Any:
    """The JSON value of a line (bytes that are not UTF-8 read as U+FFFD): `_BLANK` for a blank line, None for a line
    that is not JSON."""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return _BLANK
    try:
        return loads(text)
    except ValueError:
        return None


def _header_or_none(obj: dict[str, Any]) -> RecordingHeader | None:
    """The header of a first line with `recording`; None when it does not decode (section 6: no header then)."""
    try:
        return RecordingHeader.from_json(obj)
    except FactoryError:
        return None


def _recv_ok(obj: dict[str, Any]) -> bool:
    """A side line's `recv`, when there, is a number (section 6)."""
    recv = obj.get("recv")
    return recv is None or (isinstance(recv, (int, float)) and not isinstance(recv, bool))


class _Gunzip:
    """Gzip-decompresses a byte stream that starts with the gzip magic (multi-member files too); passes others.

    Bad gzip data raises `decode` at once; `flush()` raises `decode` when the data ended inside a member (a truncated
    file or download).
    """

    def __init__(self) -> None:
        self._head = b""
        self._gzip: bool | None = None
        self._z = zlib.decompressobj(wbits=31)
        self._open = False  # a member has started and not ended

    def feed(self, chunk: bytes) -> bytes:
        if self._gzip is None:
            self._head += chunk
            if len(self._head) < 2:
                return b""
            self._gzip = self._head.startswith(_GZIP_MAGIC)
            chunk, self._head = self._head, b""
        if not self._gzip:
            return chunk
        out = []
        try:
            while chunk:
                self._open = True
                out.append(self._z.decompress(chunk))
                if not self._z.eof:
                    break
                self._open = False
                chunk = self._z.unused_data  # the next member of a multi-member file
                self._z = zlib.decompressobj(wbits=31)
        except zlib.error as e:
            raise FactoryError(code="decode", message=f"recording: bad gzip data ({e})") from None
        return b"".join(out)

    def flush(self) -> bytes:
        """The end of the input: what is left of a plain file shorter than 2 bytes; `decode` if the gzip data ends
        early."""
        if self._gzip is None:
            return self._head
        if self._open:
            raise FactoryError(code="decode", message="recording: the gzip data ends early (the file is truncated)")
        return b""


def _gunzip(chunks: Iterable[bytes]) -> Iterator[bytes]:
    gz = _Gunzip()
    for chunk in chunks:
        data = gz.feed(chunk)
        if data:
            yield data
    tail = gz.flush()
    if tail:
        yield tail


async def _agunzip(chunks: AsyncIterator[bytes]) -> AsyncGenerator[bytes, None]:
    gz = _Gunzip()
    async for chunk in chunks:
        data = gz.feed(chunk)
        if data:
            yield data
    tail = gz.flush()
    if tail:
        yield tail


def _split_lines(data: Iterable[bytes]) -> Iterator[bytes]:
    rest = b""
    for chunk in data:
        *done, rest = (rest + chunk).split(b"\n")
        yield from done
    if rest:
        yield rest


async def _asplit_lines(data: AsyncIterator[bytes]) -> AsyncGenerator[bytes, None]:
    rest = b""
    async for chunk in data:
        *done, rest = (rest + chunk).split(b"\n")
        for line in done:
            yield line
    if rest:
        yield rest


def _lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """The lines of a byte stream, gunzipped when it is gzip."""
    return _split_lines(_gunzip(chunks))


def _file_data(path: str) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        yield from _gunzip(iter(lambda: fh.read(_CHUNK), b""))


def _read_some(fh: IO[bytes], gz: _Gunzip) -> bytes | None:
    """Read and gunzip the next chunk of a file (in a worker thread); `None` at its end."""
    chunk = fh.read(_CHUNK)
    return None if not chunk else gz.feed(chunk)


async def _afile_data(path: str) -> AsyncGenerator[bytes, None]:
    """The bytes of a local file, gunzipped: read and decompressed in a worker thread, a chunk at a time."""
    fh = await asyncio.to_thread(open, path, "rb")
    try:
        gz = _Gunzip()
        while (data := await asyncio.to_thread(_read_some, fh, gz)) is not None:
            if data:
                yield data
        tail = gz.flush()
        if tail:
            yield tail
    finally:
        fh.close()


class Replay:
    """The Interpreter over a recording file: local path or HTTP(S) URL, `.ndjson` or `.ndjson.gz`.

    Args:
        source: A path, or an `http(s)://` URL (for example a published recording of
            https://factory.pdigger.com/developers/data/).
        transport: An httpx transport for URLs (sync for the sync iterators, async for the `a*` variants); tests pass
            a `testing.FakeFactory`.
        headers: Extra HTTP headers for URLs.
        sleep: The async sleep used by `pace="realtime"` (tests inject one that does not wait).
        monotonic: The clock used by `pace="realtime"`.
    """

    def __init__(
        self,
        source: str | os.PathLike[str],
        *,
        transport: Any = None,
        headers: dict[str, str] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        defaults = DEFAULTS.get()
        self.source = os.fspath(source)
        self._transport = transport if transport is not None else (defaults.transport if defaults else None)
        self._headers = {"User-Agent": USER_AGENT, **(headers or {})}
        self._sleep = sleep or (defaults.sleep if defaults and defaults.sleep else asyncio.sleep)
        self._monotonic = monotonic or (defaults.monotonic if defaults and defaults.monotonic else time.monotonic)
        self._header: RecordingHeader | None = None
        self._header_read = False

    @classmethod
    def open(cls, source: str | os.PathLike[str], **options: Any) -> Replay:
        """A replay of `source` (see the class for the options)."""
        return cls(source, **options)

    # ---- reading ---------------------------------------------------------------------------------------------------
    def _url_data(self) -> Iterator[bytes]:
        try:
            with (
                httpx.Client(
                    transport=self._transport, headers=self._headers, timeout=30.0, follow_redirects=True
                ) as client,
                client.stream("GET", self.source) as response,
            ):
                if not 200 <= response.status_code < 300:  # a moved recording is followed; any other status fails
                    try:
                        content = response.read()
                    except httpx.HTTPError:
                        content = b""  # the error body broke: the status still says what happened
                    raise FactoryError.from_reply(response.status_code, content, response.headers)
                yield from _gunzip(response.iter_bytes())
        except httpx.HTTPError as e:
            raise http_error(f"GET {self.source}", e) from e

    async def _aurl_data(self) -> AsyncGenerator[bytes, None]:
        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport, headers=self._headers, timeout=30.0, follow_redirects=True
                ) as client,
                client.stream("GET", self.source) as response,
            ):
                if not 200 <= response.status_code < 300:  # a moved recording is followed; any other status fails
                    try:
                        content = await response.aread()
                    except httpx.HTTPError:
                        content = b""  # the error body broke: the status still says what happened
                    raise FactoryError.from_reply(response.status_code, content, response.headers)
                async with contextlib.aclosing(_agunzip(response.aiter_bytes())) as chunks:
                    async for data in chunks:
                        yield data
        except httpx.HTTPError as e:
            raise http_error(f"GET {self.source}", e) from e

    def _objects(self) -> Generator[tuple[bool, dict[str, Any]], None, None]:
        """Every JSON object of the file, with whether it is the first line that is not blank."""
        data = self._url_data() if _is_url(self.source) else _file_data(self.source)
        first = True
        for raw in _split_lines(data):
            obj = _decode_line(raw)
            if obj is _BLANK:
                continue
            if isinstance(obj, dict):
                yield first, obj
            first = False

    async def _aobjects(self) -> AsyncGenerator[tuple[bool, dict[str, Any]], None]:
        data = self._aurl_data() if _is_url(self.source) else _afile_data(self.source)
        first = True
        async with contextlib.aclosing(data), contextlib.aclosing(_asplit_lines(data)) as lines:
            async for raw in lines:
                obj = _decode_line(raw)
                if obj is _BLANK:
                    continue
                if isinstance(obj, dict):
                    yield first, obj
                first = False

    def lines(self) -> Iterator[dict[str, Any]]:
        """Every JSON object of the file, in order (lines that are not JSON objects are skipped)."""
        with contextlib.closing(self._objects()) as objects:
            for _, obj in objects:
                yield obj

    def _header_of(self, first: bool, obj: dict[str, Any]) -> None:
        """Take the header from the first object read: the file's when that is its first line that is not blank."""
        self._header = _header_or_none(obj) if first and classify_line(obj, False) == "header" else None

    @property
    def header(self) -> RecordingHeader | None:
        """The recording header (read from the file's first lines on first use), or `None` if it has none.

        For a URL in async code use `await replay.aheader()`, which does not block the event loop.
        """
        if not self._header_read:
            with contextlib.closing(self._objects()) as objects:
                for first, obj in objects:
                    self._header_of(first, obj)
                    break
            self._header_read = True
        return self._header

    async def aheader(self) -> RecordingHeader | None:
        """`header`, read without blocking the event loop."""
        if not self._header_read:
            async with contextlib.aclosing(self._aobjects()) as objects:
                async for first, obj in objects:
                    self._header_of(first, obj)
                    break
            self._header_read = True
        return self._header

    def _take(self, obj: dict[str, Any], core: InterpreterCore, first: bool) -> Frame | None:
        """Process one object (`first`: the file's first line that is not blank): the frame to feed, or `None`."""
        kind = classify_line(obj, not first)
        if first:
            self._header_of(first, obj)
            self._header_read = True
        if kind == "side":
            if obj.get("sse") == "clock" and _recv_ok(obj):
                with contextlib.suppress(FactoryError):  # a clock line that does not decode is ignored
                    core.clock(ClockEvent.from_json(obj.get("data")))
            elif obj.get("sse") in ("gap", "reset") and _recv_ok(obj):
                with contextlib.suppress(FactoryError):  # frames dropped here; a line that does not decode is ignored
                    if obj["sse"] == "gap":
                        GapEvent.from_json(obj.get("data"))
                    else:
                        ResetEvent.from_json(obj.get("data"))
                    core.gap()
            return None
        if kind == "frame":
            try:
                return Frame.from_json(obj)
            except FactoryError:  # a frame line that does not decode is a line a reader does not understand: ignored
                return None
        return None

    def _pass(self, core: InterpreterCore) -> Iterator[Frame]:
        """Every frame of one pass, in file order; clock, gap and reset lines go to `core`."""
        with contextlib.closing(self._objects()) as objects:
            for first, obj in objects:
                got = self._take(obj, core, first)
                if got is not None:
                    yield got

    # ---- sync iteration --------------------------------------------------------------------------------------------
    def frames(self) -> Iterator[Frame]:
        """Every frame, in file order (revisions and duplicates included, as recorded)."""
        return self._pass(InterpreterCore())

    def updates(self, *, core: InterpreterCore | None = None) -> Iterator[Update]:
        """The Interpreter's update for every frame (duplicates skipped), then a final update (`frame=None`,
        `final=True`) with the activities that were still open and the facts of the last tray leaving.

        Args:
            core: The `InterpreterCore` to feed (a new one by default); after the pass, `core.summary()` is the
                summary of the whole recording, so one pass gives both.
        """
        core = InterpreterCore() if core is None else core
        for frame in self._pass(core):
            update = core.feed(frame)
            if not update.dropped:
                yield update
        yield core.end()

    def events(self, *, core: InterpreterCore | None = None) -> Iterator[Event]:
        """Every event, in output order (`core` as for `updates()`)."""
        for update in self.updates(core=core):
            yield from update.events

    def activities(self, *, core: InterpreterCore | None = None) -> Iterator[Activity]:
        """Every completed activity, in output order (the still-open ones at the end, as `interrupted`; `core` as for
        `updates()`)."""
        for update in self.updates(core=core):
            yield from update.activities

    def tray_facts(self, *, core: InterpreterCore | None = None) -> Iterator[TrayFact]:
        """Every tray fact, in output order (`core` as for `updates()`): trays arriving and leaving, tray stones
        removed, set down or no longer sure (SDK-DESIGN.md 4.6)."""
        for update in self.updates(core=core):
            yield from update.tray_facts

    def trays(self) -> list[Tray]:
        """Every tray of the recording, oldest first, with its `arrived` and `leaving` points, its tray stones and its
        facts (reads all of it). `tray.window()` is the span to replay or to show; on the shared session
        `factory.session.seek(tray.arrived.t)` shows the arrival."""
        core = InterpreterCore(trays_kept=None)
        for _ in self.updates(core=core):
            pass
        return core.trays()

    def tray_reports(self) -> list[TrayReport]:
        """The report of every tray that arrived, oldest first (reads all of it; SDK-DESIGN.md 4.7): which stones were
        removed, added and moved, and whether that is the tray's whole account."""
        return [r for r in (tray.report() for tray in self.trays()) if r is not None]

    def interpret(self) -> ReplayResult:
        """One pass: every event, activity and tray fact, the summary, every tray, and the speckle answers read."""
        events: list[Event] = []
        activities: list[Activity] = []
        facts: list[TrayFact] = []
        speckle: list[TraySpeckle] = []
        core = InterpreterCore(trays_kept=None)
        for update in self.updates(core=core):
            events += update.events
            activities += update.activities
            facts += update.tray_facts
            speckle += update.speckle
        return ReplayResult(
            header=self._header,
            events=events,
            activities=activities,
            summary=core.summary(),
            tray_facts=facts,
            trays=core.trays(),
            speckle=speckle,
        )

    def summary(self) -> Summary:
        """The summary of the whole recording (reads all of it, keeping nothing else)."""
        core = InterpreterCore()
        for _ in self.updates(core=core):
            pass
        return core.summary()

    # ---- async iteration -------------------------------------------------------------------------------------------
    @staticmethod
    def _check(pace: str | None, speed: float) -> bool:
        if pace not in (None, "realtime"):
            raise ValueError(f"pace must be None or 'realtime', not {pace!r}")
        if not speed > 0:
            raise ValueError("speed must be above 0")
        return pace == "realtime"

    def aframes(self, *, pace: Literal["realtime"] | None = None, speed: float = 1.0) -> AsyncStream[Frame]:
        """`frames()` as an async stream; with `pace="realtime"` frames come at their media times / `speed`."""
        return AsyncStream(self._apass(InterpreterCore(), self._check(pace, speed), speed))

    def aupdates(
        self, *, pace: Literal["realtime"] | None = None, speed: float = 1.0, core: InterpreterCore | None = None
    ) -> AsyncStream[Update]:
        """`updates()` as an async stream (optionally paced; `core` as for `updates()`)."""
        return AsyncStream(self._aupdates(self._check(pace, speed), speed, core))

    def aevents(
        self, *, pace: Literal["realtime"] | None = None, speed: float = 1.0, core: InterpreterCore | None = None
    ) -> AsyncStream[Event]:
        """`events()` as an async stream (optionally paced; `core` as for `updates()`)."""
        return AsyncStream(_flatten(self.aupdates(pace=pace, speed=speed, core=core), "events"))

    def aactivities(
        self, *, pace: Literal["realtime"] | None = None, speed: float = 1.0, core: InterpreterCore | None = None
    ) -> AsyncStream[Activity]:
        """`activities()` as an async stream (optionally paced; `core` as for `updates()`)."""
        return AsyncStream(_flatten(self.aupdates(pace=pace, speed=speed, core=core), "activities"))

    def atray_facts(
        self, *, pace: Literal["realtime"] | None = None, speed: float = 1.0, core: InterpreterCore | None = None
    ) -> AsyncStream[TrayFact]:
        """`tray_facts()` as an async stream (optionally paced; `core` as for `updates()`)."""
        return AsyncStream(_flatten(self.aupdates(pace=pace, speed=speed, core=core), "tray_facts"))

    async def atrays(self) -> list[Tray]:
        """`trays()` as a coroutine (the file or URL is read without blocking the event loop)."""
        core = InterpreterCore(trays_kept=None)
        async with self.aupdates(core=core) as updates:
            async for _ in updates:
                pass
        return core.trays()

    async def atray_reports(self) -> list[TrayReport]:
        """`tray_reports()` as a coroutine (the file or URL is read without blocking the event loop)."""
        return [r for r in (tray.report() for tray in await self.atrays()) if r is not None]

    async def asummary(self) -> Summary:
        """`summary()` as a coroutine (the file or URL is read without blocking the event loop)."""
        core = InterpreterCore()
        async with self.aupdates(core=core) as updates:
            async for _ in updates:
                pass
        return core.summary()

    async def _apass(self, core: InterpreterCore, paced: bool, speed: float) -> AsyncGenerator[Frame, None]:
        """Every frame of one pass (clock lines go to `core`), paced at media time when `paced`; the event loop gets
        a turn after every frame."""
        session: int | None = None
        anchor_clock = anchor_t = 0.0
        async with contextlib.aclosing(self._aobjects()) as objects:
            async for first, obj in objects:
                got = self._take(obj, core, first)
                if got is not None:
                    if paced:
                        if got.session != session:
                            session, anchor_clock, anchor_t = got.session, self._monotonic(), got.t
                        delay = anchor_clock + (got.t - anchor_t) / speed - self._monotonic()
                        if delay > 0:
                            await self._sleep(delay)
                    yield got
                    await asyncio.sleep(0)

    async def _aupdates(self, paced: bool, speed: float, core: InterpreterCore | None) -> AsyncGenerator[Update, None]:
        core = InterpreterCore() if core is None else core
        async with AsyncStream(self._apass(core, paced, speed)) as frames:
            async for frame in frames:
                update = core.feed(frame)
                if not update.dropped:
                    yield update
        yield core.end()


async def _flatten(
    updates: AsyncStream[Update], what: Literal["events", "activities", "tray_facts"]
) -> AsyncGenerator[Any, None]:
    async with updates:
        async for update in updates:
            for item in getattr(update, what):
                yield item

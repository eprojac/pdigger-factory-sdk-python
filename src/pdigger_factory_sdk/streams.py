"""Mid level: typed streams of frames with resume, revisions, duplicate drop and backfill (SDK-DESIGN.md section 2.3).

    async with FactoryClient(url) as factory:
        async with factory.streams.frames(layers=("world", "cards"), observe=True) as items:
            async for item in items:
                if isinstance(item, FrameItem):
                    print(item.frame.g, item.is_revision)

`frames()` follows the SSE frames stream. With `resume=True` (the default) a stream that the server ends or that breaks
is reopened with `Last-Event-ID`, so the caller sees one endless stream. `limit` and `max_s` bound each connection: the
SDK counts them itself and closes the connection when either is reached, then reconnects at once (the 20.20 unit sends
nothing more by then, but closes a finished stream only about 10 s later). A frame whose `world.rev` is higher than that
of a frame already delivered for the same `(session, g)` is passed on with `is_revision=True`; one with the same or a
lower revision is dropped. With `backfill=True` a frame released with `missing` layers is held, with every frame after
it, while its layers are read with `GET /frames/{g}` (1.5 s after it arrived, up to 3 tries 1 s apart); side events
(`clock`, `worker`, `gap`, `reset`) are never held. The unit sends a `clock` event only when the shared state changes,
none on connect: `clock_on_connect=True` reads the state (`GET /session`) after every (re)connect and passes it on
first, as a `ClockItem`.

The SDK logs reconnects, backoff waits and backfill give-ups to the `pdigger_factory_sdk` logger.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import random
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, TypeVar

from ._http import Backoff, SleepFn
from ._stream import AsyncStream
from .api import Api, EventStream, Layers, StreamMessage
from .errors import FactoryError
from .models import ClockEvent, Frame, GapEvent, ResetEvent, Sip1Packet, State, WorkerStatus

__all__ = [
    "BACKFILL_DELAY",
    "BACKFILL_INTERVAL",
    "BACKFILL_TIMEOUT",
    "BACKFILL_TRIES",
    "CLOCK_READ_TIMEOUT",
    "ClockItem",
    "FrameItem",
    "GapItem",
    "PacketItem",
    "PacketStreamItem",
    "ResetItem",
    "StreamItem",
    "Streams",
    "WorkerItem",
]

log = logging.getLogger(__name__)

BACKFILL_DELAY = 1.5
"""Seconds after a frame with missing layers arrived before the SDK reads them."""
BACKFILL_INTERVAL = 1.0
"""Seconds between the reads of a frame's missing layers."""
BACKFILL_TRIES = 3
"""Reads of a frame's missing layers before it is released without those still missing."""
BACKFILL_TIMEOUT = 1.0
"""Seconds one read of a frame's missing layers may take (one request, no retries): the reads are due 1.5, 2.5 and
3.5 s after the frame arrived, so a late layer holds the frames behind it for about 4.5 s at the most."""
CLOCK_READ_TIMEOUT = 1.0
"""Seconds the read of the shared state after a connect may take (one request); without it, wall times wait for a
`clock` event."""

_LAYERS = ("raw", "overlay", "scene", "cards", "world")


@dataclass(slots=True, frozen=True)
class FrameItem:
    """A frame of the stream."""

    frame: Frame
    id: str | None = None
    """The SSE event id, `<boot>-<cursor>` (none on the long-poll)."""
    is_revision: bool = False
    """True when a frame for the same `(session, g)` was delivered before with a lower `world.rev`."""
    is_backfill: bool = False
    """True when layers missing at release were read later and merged in (they are no longer in `missing`)."""


@dataclass(slots=True, frozen=True)
class ClockItem:
    """The shared state: a `clock` event (play, pause, seek, select, rate changed), a long-poll reply's state when its
    revision changed, or the state read with `GET /session` when a stream (re)connected (`clock_on_connect=True`)."""

    state: State
    server_now: float
    """The server's wall clock when it sent the state, epoch seconds."""
    id: str | None = None


@dataclass(slots=True, frozen=True)
class WorkerItem:
    """The interpreter's worker status (`worker` event)."""

    worker: WorkerStatus
    id: str | None = None


@dataclass(slots=True, frozen=True)
class GapItem:
    """The server skipped `count` items because this consumer was too slow (`gap` event)."""

    count: int
    id: str | None = None


@dataclass(slots=True, frozen=True)
class ResetItem:
    """The server restarted (`reset` event, a `Last-Event-ID` of another boot); the stream goes on from the newest."""

    boot: str
    cursor: int
    id: str | None = None


@dataclass(slots=True, frozen=True)
class PacketItem:
    """A raw `sip1` packet of `raw_packets()`."""

    packet: Sip1Packet
    id: str | None = None


StreamItem = FrameItem | ClockItem | WorkerItem | GapItem | ResetItem
"""What `Streams.frames()` and `Streams.poll()` yield."""
PacketStreamItem = PacketItem | ClockItem | WorkerItem | GapItem | ResetItem
"""What `Streams.raw_packets()` yields."""

I = TypeVar("I")  # noqa: E741 - the item type of a stream


class _Stop(enum.Enum):
    """Why a connection gives no further message."""

    END = "the server ended it"
    DEADLINE = "its max_s is up"


def _side(msg: StreamMessage) -> ClockItem | WorkerItem | GapItem | ResetItem | None:
    """A side event, or `None` for an event type this SDK does not know (ignored, forward compatible)."""
    if msg.event == "clock":
        clock = ClockEvent.from_json(msg.json())
        return ClockItem(state=clock, server_now=clock.server_now, id=msg.id)
    if msg.event == "worker":
        return WorkerItem(worker=WorkerStatus.from_json(msg.json()), id=msg.id)
    if msg.event == "gap":
        return GapItem(count=GapEvent.from_json(msg.json()).gap, id=msg.id)
    if msg.event == "reset":
        reset = ResetEvent.from_json(msg.json())
        return ResetItem(boot=reset.boot, cursor=reset.cursor, id=msg.id)
    return None


def _frame_item(msg: StreamMessage) -> StreamItem | None:
    if msg.event == "frame":
        return FrameItem(frame=Frame.from_json(msg.json()), id=msg.id)
    return _side(msg)


def _packet_item(msg: StreamMessage) -> PacketStreamItem | None:
    if msg.event == "frame":
        return PacketItem(packet=Sip1Packet.from_json(msg.json()), id=msg.id)
    return _side(msg)


class _Delivered:
    """The newest delivered `world.rev` per `(session, g)`, for the most recent `size` grid frames."""

    __slots__ = ("_revs", "_size")

    def __init__(self, size: int = 4096) -> None:
        self._revs: OrderedDict[tuple[int, int], int] = OrderedDict()
        self._size = size

    @staticmethod
    def _key(frame: Frame) -> tuple[tuple[int, int], int]:
        return (frame.session, frame.g), (frame.world.rev if frame.world is not None else 0)

    def is_duplicate(self, frame: Frame) -> bool:
        """Delivered before with the same or a higher revision."""
        key, rev = self._key(frame)
        old = self._revs.get(key)
        return old is not None and rev <= old

    def deliver(self, item: FrameItem) -> FrameItem | None:
        """The item to pass on (marked as a revision when it is one), or `None` for a duplicate."""
        key, rev = self._key(item.frame)
        old = self._revs.get(key)
        if old is not None and rev <= old:
            return None
        self._revs[key] = rev
        self._revs.move_to_end(key)
        if len(self._revs) > self._size:
            self._revs.popitem(last=False)
        return item if old is None else replace(item, is_revision=True)


class _Held:
    """A frame held back: waiting for its own backfill, or behind an earlier held frame (`done` already set)."""

    __slots__ = ("done", "item")

    def __init__(self, item: FrameItem, done: asyncio.Future[tuple[Frame, bool]]) -> None:
        self.item = item
        self.done = done

    def result(self) -> FrameItem:
        frame, merged = self.done.result()
        return replace(self.item, frame=frame, is_backfill=True) if merged else self.item


def merge_layers(frame: Frame, reply: Frame) -> tuple[Frame, bool]:
    """`frame` with the layers it misses taken from `reply` (a `GET /frames/{g}` of them), and whether any were."""
    raw = dict(frame.raw_json)
    changes: dict[str, Any] = {}
    still: list[str] = []
    for layer in frame.missing:
        value = getattr(reply, layer) if layer in _LAYERS else None
        if value is None:
            still.append(layer)
            continue
        changes[layer] = value
        raw[layer] = reply.raw_json[layer]
    if not changes:
        return frame, False
    if still:
        raw["missing"] = still
    else:
        raw.pop("missing", None)
    return replace(frame, missing=still, raw_json=raw, **changes), True


class Streams:
    """Typed streams of the shared session: `frames()` (SSE), `poll()` (long-poll) and `raw_packets()`.

    Reach it as `factory.streams`. Every stream is an `AsyncStream`: close it, leave its `async with` block, or cancel
    the task iterating it, and its connection is closed.
    """

    def __init__(
        self,
        api: Api,
        *,
        sleep: SleepFn,
        monotonic: Callable[[], float],
        backoff: Backoff,
        rng: random.Random,
        observe: bool,
        on_state: Callable[[State], None] | None = None,
    ) -> None:
        self._api = api
        self._sleep = sleep
        self._monotonic = monotonic
        self._backoff = backoff
        self._rng = rng
        self._observe = observe
        self._on_state = on_state

    # ---- public streams --------------------------------------------------------------------------------------------
    def frames(
        self,
        layers: Layers = None,
        *,
        after: int | None = None,
        observe: bool | None = None,
        precision: str | None = "rounded",
        resume: bool = True,
        backfill: bool = False,
        clock_on_connect: bool = False,
        limit: int | None = None,
        max_s: int | None = None,
        last_event_id: str | None = None,
        max_reconnects: int | None = None,
    ) -> AsyncStream[StreamItem]:
        """Frames of the shared session over SSE, with clock, worker, gap and reset side events.

        An event that does not decode (not JSON, or not its type's shape) is skipped with a warning in the log, and the
        stream goes on (its id still counts for resuming).

        Args:
            layers: The layers to ask for (the server's default `overlay,scene,cards,world` when `None`).
            after: Start after this cursor (default: the newest frames). It is asked for until the stream has an event
                id; from then on a reconnect continues with `Last-Event-ID`.
            observe: True does not count this stream as a viewer, so an idle worker stays idle; default: the client's.
            precision: `rounded` (the default) or `full`; backfill reads use the same.
            resume: Reopen the stream with `Last-Event-ID` when the server ends it, the connection breaks, or a
                connection reached `limit` or `max_s`.
            backfill: Hold frames with missing layers until they are read (the Interpreter's default).
            clock_on_connect: After every (re)connect, read the shared state (`GET /session`) and pass it on first, as
                a `ClockItem` (the unit sends a `clock` event only when the state changes; the Interpreter uses this).
            limit: End each connection after this many frames (asked of the server too, and counted here).
            max_s: End each connection after this many seconds (asked of the server too, and timed here).
            last_event_id: Resume after this event id (`<boot>-<cursor>`), as a reconnect would.
            max_reconnects: Give up (`stream_closed`) after this many reconnects in a row without an event.
        """
        obs = self._observe if observe is None else observe

        def open_stream(last_id: str | None) -> Awaitable[EventStream]:
            return self._api.stream_frames(
                layers=layers,
                format="sse",
                after=after if last_id is None else None,
                limit=limit,
                max_s=max_s,
                observe=obs,
                precision=precision,
                last_event_id=last_id,
            )

        items: AsyncGenerator[StreamItem, None] = self._connections(
            "the frames stream",
            open_stream,
            _frame_item,
            resume=resume,
            last_event_id=last_event_id,
            max_reconnects=max_reconnects,
            limit=limit,
            max_s=max_s,
            on_open=self._read_clock if clock_on_connect else None,
        )
        return AsyncStream(self._pipeline(items, backfill, precision))

    def poll(
        self,
        layers: Layers = None,
        *,
        cursor: int = 0,
        rev: int = 0,
        cid: str | None = None,
        observe: bool | None = None,
        limit: int | None = None,
        wait: int | None = None,
        precision: str | None = None,
        backfill: bool = False,
    ) -> AsyncStream[StreamItem]:
        """The long-poll equivalent of `frames()` (loops `pollFrames`), for environments without streaming.

        Each reply's state is passed on as a `ClockItem` when its revision changed, its worker as a `WorkerItem` when
        the worker's state or reason changed, then its frames; the next poll continues from the reply's `cursor` and
        `state.revision`.

        Like `frames()`, it counts as a viewer unless `observe` is true (default: the client's): a long-poll counts
        only with a client id, so the SDK sends a random one (`cid`, the same for the whole stream). Give your own
        `cid` to choose it; it always counts as a viewer.
        """
        obs = self._observe if observe is None else observe
        if cid is None and not obs:
            cid = f"sdk-python-{self._rng.getrandbits(48):012x}"
        polls = self._polls(layers, cursor, rev, cid, limit, wait, precision)
        return AsyncStream(self._pipeline(polls, backfill, precision))

    def raw_packets(
        self,
        *,
        format: Literal["sse", "ndjson"] = "sse",
        after: int | None = None,
        observe: bool | None = None,
        resume: bool = True,
        limit: int | None = None,
        max_s: int | None = None,
        last_event_id: str | None = None,
        max_reconnects: int | None = None,
    ) -> AsyncStream[PacketStreamItem]:
        """The raw `sip1` packets (`raw/stream`), with the same resume rules as `frames()`.

        Over SSE a reconnect sends `Last-Event-ID` (and `after` until the stream has an event id); over NDJSON (no
        event ids) it asks for `after` = the cursor of the last packet received. `limit` and `max_s` are counted here
        too.
        """
        obs = self._observe if observe is None else observe
        newest: list[int | None] = [after]

        def open_stream(last_id: str | None) -> Awaitable[EventStream]:
            if format == "ndjson":
                return self._api.raw_stream(format="ndjson", after=newest[0], limit=limit, max_s=max_s, observe=obs)
            return self._api.raw_stream(
                format="sse",
                after=after if last_id is None else None,
                limit=limit,
                max_s=max_s,
                observe=obs,
                last_event_id=last_id,
            )

        def parse(msg: StreamMessage) -> PacketStreamItem | None:
            item = _packet_item(msg)
            if isinstance(item, PacketItem) and item.packet.cursor is not None:
                newest[0] = item.packet.cursor
            return item

        return AsyncStream(
            self._connections(
                "the raw packet stream",
                open_stream,
                parse,
                resume=resume,
                last_event_id=last_event_id,
                max_reconnects=max_reconnects,
                limit=limit,
                max_s=max_s,
            )
        )

    # ---- internals -------------------------------------------------------------------------------------------------
    def _note(self, item: object) -> None:
        """Tell the client's session view about a state this stream saw."""
        if isinstance(item, ClockItem) and self._on_state is not None:
            self._on_state(item.state)

    async def _wait(self, what: str, reason: str, attempt: int, max_reconnects: int | None = None) -> None:
        """Back off before reconnect number `attempt + 1`, or give up (`stream_closed`) when that would be one more
        than `max_reconnects` in a row, or when the client was closed."""
        if self._api.is_closed:
            raise FactoryError(code="stream_closed", message=f"{what} ended: the client was closed")
        if max_reconnects is not None and attempt >= max_reconnects:
            raise FactoryError(
                code="stream_closed", message=f"{what} gave up after {max_reconnects} reconnects: {reason}"
            )
        delay = self._backoff.delay(attempt, self._rng)
        log.warning("%s: %s; reconnecting in %.2f s (attempt %d)", what, reason, delay, attempt + 1)
        await self._sleep(delay)

    async def _read_clock(self) -> ClockItem | None:
        """The shared state now (`GET /session`: one request, at most `CLOCK_READ_TIMEOUT`), for a stream that just
        (re)connected; `None` if it cannot be read."""
        read = self._api.with_options(retries=0, timeout=CLOCK_READ_TIMEOUT).get_session()
        try:
            reply = await asyncio.wait_for(read, CLOCK_READ_TIMEOUT)
        except (FactoryError, asyncio.TimeoutError) as e:
            why = str(e) or f"no reply within {CLOCK_READ_TIMEOUT:g} s"
            log.warning("could not read the shared state after connecting (%s); wall times wait for a clock event", why)
            return None
        return ClockItem(state=reply.state, server_now=reply.server_now)

    async def _next(self, stream: EventStream, deadline: float | None) -> StreamMessage | _Stop:
        """The connection's next message, or why there is none: the server ended it, or its `max_s` is up."""
        if deadline is None:
            try:
                return await stream.__anext__()
            except StopAsyncIteration:
                return _Stop.END
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            return _Stop.DEADLINE
        task: asyncio.Future[StreamMessage] = asyncio.ensure_future(stream.__anext__())
        try:
            await asyncio.wait((task,), timeout=remaining)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if task.cancelled():
            return _Stop.DEADLINE
        error = task.exception()
        if isinstance(error, StopAsyncIteration):
            return _Stop.END
        if error is not None:
            raise error
        return task.result()

    async def _connections(
        self,
        what: str,
        open_stream: Callable[[str | None], Awaitable[EventStream]],
        parse: Callable[[StreamMessage], I | None],
        *,
        resume: bool,
        last_event_id: str | None,
        max_reconnects: int | None,
        limit: int | None = None,
        max_s: int | None = None,
        on_open: Callable[[], Awaitable[Any]] | None = None,
    ) -> AsyncGenerator[I, None]:
        """Items over one or more connections: reconnects with `Last-Event-ID` when `resume`.

        Each connection ends when the server ends it, when it breaks, or when it reached `limit` frames or `max_s`
        seconds (counted here: the unit sends nothing more by then but closes the connection only about 10 s later).
        A connection that gave events and ended normally is reopened at once; one that failed, broke or gave no event
        after the backoff (a run of those counts towards `max_reconnects`). `on_open` gives an item to pass on first
        after every (re)connect. An event that does not decode is skipped with a warning, and the connection goes on.
        """
        last_id = last_event_id
        attempt = 0
        while True:
            try:
                stream = await open_stream(last_id)
            except FactoryError as e:
                if not (resume and e.is_retryable):
                    raise
                await self._wait(what, f"could not connect ({e})", attempt, max_reconnects)
                attempt += 1
                continue
            deadline = None if max_s is None else self._monotonic() + max_s
            got = frames = 0
            stop: _Stop | str = _Stop.END
            broken: FactoryError | None = None
            try:
                if on_open is not None:
                    first = await on_open()
                    if first is not None:
                        self._note(first)
                        yield first
                while True:
                    msg = await self._next(stream, deadline)
                    if isinstance(msg, _Stop):
                        stop = msg
                        break
                    got += 1
                    if msg.id:
                        last_id = msg.id
                    try:
                        item = parse(msg)
                    except FactoryError as e:  # the parsers raise decode only: the event is skipped, the stream goes on
                        log.warning("%s: skipped a %s event that does not decode (%s)", what, msg.event, e.message)
                        item = None
                    if isinstance(item, ResetItem):
                        last_id = f"{item.boot}-{item.cursor}"
                        log.info("%s: the server restarted (boot %s); it goes on from its newest", what, item.boot)
                    if item is not None:
                        self._note(item)
                        yield item
                    if msg.event == "frame":
                        frames += 1
                        if limit is not None and frames >= limit:
                            stop = f"its limit of {limit} frames"
                            break
            except FactoryError as e:
                if e.code != "transport":
                    raise
                if not resume:
                    raise FactoryError(code="stream_closed", message=f"{what} ended: {e.message}") from e
                broken = e
            finally:
                await stream.aclose()
            if not resume:
                return
            if broken is None and got:
                reason = stop.value if isinstance(stop, _Stop) else f"reached {stop}"
                log.debug("%s: %s after %d events; reconnecting at once", what, reason, got)
                attempt = 0
                continue
            if got:
                attempt = 0
            if broken is not None:
                reason = broken.message
            else:
                reason = "ended without an event" if stop is _Stop.END else "reached its max_s without an event"
            await self._wait(what, reason, attempt, max_reconnects)
            attempt += 1

    async def _polls(
        self,
        layers: Layers,
        cursor: int,
        rev: int,
        cid: str | None,
        limit: int | None,
        wait: int | None,
        precision: str | None,
    ) -> AsyncGenerator[StreamItem, None]:
        state_key: tuple[int, int] | None = None
        worker_key: tuple[str, str] | None = None
        attempt = 0
        while True:
            try:
                reply = await self._api.poll_frames(
                    cursor=cursor, rev=rev, cid=cid, layers=layers, limit=limit, wait=wait, precision=precision
                )
            except FactoryError as e:
                if not e.is_retryable:
                    raise
                await self._wait("the long-poll", f"failed ({e})", attempt)
                attempt += 1
                continue
            attempt = 0
            state = reply.state
            if (state.session, state.revision) != state_key:
                state_key = (state.session, state.revision)
                clock = ClockItem(state=state, server_now=reply.server_now)
                self._note(clock)
                yield clock
            worker = reply.worker
            if (worker.state, worker.reason) != worker_key:
                worker_key = (worker.state, worker.reason)
                yield WorkerItem(worker=worker)
            for frame in reply.frames:
                yield FrameItem(frame=frame)
            cursor, rev = reply.cursor, state.revision

    async def _complete(self, frame: Frame, arrived: float, precision: str | None) -> tuple[Frame, bool]:
        """Read a held frame's missing layers 1.5, 2.5 and 3.5 s after it arrived (until they come), each read one
        request bounded by `BACKFILL_TIMEOUT`."""
        api = self._api.with_options(retries=0, timeout=BACKFILL_TIMEOUT)
        merged = False
        last = f"{BACKFILL_TRIES} reads"
        for attempt in range(BACKFILL_TRIES):
            delay = arrived + BACKFILL_DELAY + attempt * BACKFILL_INTERVAL - self._monotonic()
            if delay > 0:
                await self._sleep(delay)
            read = api.get_frame(frame.g, layers=frame.missing, session=frame.session, precision=precision)
            try:
                reply = await asyncio.wait_for(read, BACKFILL_TIMEOUT)
            except asyncio.TimeoutError:
                last = f"no reply within {BACKFILL_TIMEOUT:g} s"
                continue
            except FactoryError as e:
                last = str(e)
                if e.status in (404, 410):
                    break  # not retained, or the session is gone: release it as it is
                continue
            frame, got = merge_layers(frame, reply)
            merged = merged or got
            if not frame.missing:
                break
        if frame.missing:
            log.warning(
                "frame g %d of session %d: %s still missing (%s); released without them",
                frame.g,
                frame.session,
                ", ".join(frame.missing),
                last,
            )
        return frame, merged

    async def _pipeline(
        self, items: AsyncGenerator[StreamItem, None], backfill: bool, precision: str | None
    ) -> AsyncGenerator[StreamItem, None]:
        """Drops duplicates, marks revisions and, with `backfill`, holds frames until their missing layers are read."""
        delivered = _Delivered()
        held: deque[_Held] = deque()
        pending: asyncio.Future[StreamItem] | None = None
        ended = False
        error: Exception | None = None
        try:
            while not ended or held:
                while held and held[0].done.done():
                    out = delivered.deliver(held.popleft().result())
                    if out is not None:
                        yield out
                if ended:
                    if held:
                        await asyncio.wait((held[0].done,))
                    continue
                if pending is None:
                    pending = asyncio.ensure_future(items.__anext__())
                if held:
                    await asyncio.wait((pending, held[0].done), return_when=asyncio.FIRST_COMPLETED)
                    if not pending.done():
                        continue
                future, pending = pending, None
                try:
                    item = await future
                except StopAsyncIteration:
                    ended = True
                    continue
                except Exception as e:  # the source broke: deliver the frames that arrived, then raise
                    error, ended = e, True
                    continue
                if not isinstance(item, FrameItem):
                    yield item
                    continue
                must_hold = backfill and bool(item.frame.missing)
                if not held and not must_hold:
                    out = delivered.deliver(item)
                    if out is not None:
                        yield out
                    continue
                if delivered.is_duplicate(item.frame):
                    continue
                if must_hold:
                    done: asyncio.Future[tuple[Frame, bool]] = asyncio.ensure_future(
                        self._complete(item.frame, self._monotonic(), precision)
                    )
                else:
                    done = asyncio.get_running_loop().create_future()
                    done.set_result((item.frame, False))
                held.append(_Held(item, done))
            if error is not None:
                raise error
        finally:
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
            for h in held:
                h.done.cancel()
            await items.aclose()

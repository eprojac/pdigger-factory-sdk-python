"""High level: the shared session (watch, seek, pause, play, focus) and its clock (SDK-DESIGN.md sections 2.6, 3).

There is one shared session per server: every control here moves it for every viewer (the page, the other SDK
clients, the operators' screens). Every control takes `dry_run=True`, which validates without moving anything.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .api import LIVE, Api, GridFrame, Picture
from .errors import FactoryError
from .models import ControlReply, Source, State

__all__ = ["LIVE", "Picture", "Session", "SessionClock"]

UNTIL_LIVE_POLL = 0.25
"""`Session.until_live` reads the shared state at most this often, seconds."""


def _not_seekable(message: str, field: str) -> FactoryError:
    return FactoryError(code="not_seekable", field=field, message=message)


@dataclass(slots=True, frozen=True)
class SessionClock:
    """The shared clock, sampled once: maps between media time, server wall time and local wall time.

    The offset comes from the reply's `server_recv` and the request's round trip, as the portal samples it:
    `offset = server_recv - (t_send + t_recv) / 2`, so `server = local + offset`. Media time runs at `rate` from
    `max(anchor_wall, hold_until)` (the worker's warm-up hold) while the session plays, and stands at `anchor_media`
    while it is paused. On a live camera `duration` is `None` and so is `progress()`; its media time counts seconds
    since the session started, so the useful timestamps there are the wall times (`wall_of`, `local_wall_of`).
    """

    state: State
    server_now: float
    """The server's wall clock when it answered, epoch seconds."""
    offset: float
    """Server wall clock minus local wall clock, seconds."""
    rtt: float
    """The sampling request's round trip, seconds."""
    local_clock: Callable[[], float] = field(repr=False, compare=False, default=time.time)
    """The local wall clock the offset was measured against (`time.time` in a client)."""

    @property
    def is_live(self) -> bool:
        """True when the session shows a live camera (its state has `live`)."""
        return _is_live(self.state)

    def server_time(self, local: float | None = None) -> float:
        """The server wall time at local wall time `local` (now when not given)."""
        return (self.local_clock() if local is None else local) + self.offset

    def local_time(self, server: float) -> float:
        """The local wall time at server wall time `server`."""
        return server - self.offset

    def media_time_at(self, server: float) -> float:
        """The shared playhead (media seconds) at server wall time `server`."""
        s = self.state
        if not s.playing:
            return s.anchor_media
        m = s.anchor_media + max(0.0, server - max(s.anchor_wall, s.hold_until)) * s.rate
        return m if s.duration is None else min(m, s.duration)

    def media_time_now(self) -> float:
        """The shared playhead now."""
        return self.media_time_at(self.server_time())

    def wall_of(self, t: float) -> float | None:
        """The server wall time when the playhead is (or was) at media time `t`, at the current rate.

        While paused, media times map back from the pause (`anchor_wall`); `None` for a rate of 0.
        """
        s = self.state
        if s.rate <= 0:
            return None
        start = max(s.anchor_wall, s.hold_until) if s.playing else s.anchor_wall
        return start + (t - s.anchor_media) / s.rate

    def local_wall_of(self, t: float) -> float | None:
        """The local wall time when the playhead is (or was) at media time `t`."""
        wall = self.wall_of(t)
        return None if wall is None else self.local_time(wall)

    def progress(self) -> float | None:
        """The playhead as a fraction of the source's duration (`None` on a live camera)."""
        d = self.state.duration
        return None if not d else min(1.0, self.media_time_now() / d)


class Session:
    """The shared session's controls and clock. Reach it as `factory.session`.

    Every control (`watch`, `seek`, `pause`, `play`, `focus`, `clear_focus`) changes what every viewer sees.

    Args:
        api: The low level it calls.
        wall_clock: The local wall clock of `clock()`.
        sleep: The async sleep of `until_live` (default `asyncio.sleep`).
        monotonic: The clock of `until_live`'s timeout (default `time.monotonic`).
    """

    def __init__(
        self,
        api: Api,
        wall_clock: Callable[[], float],
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._api = api
        self._wall = wall_clock
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        self._last: State | None = None

    @property
    def last_state(self) -> State | None:
        """The newest shared state this client has seen, without a request: from `state()`, `clock()`, the replies of
        its controls (not of dry runs), and the clock items of its streams. `None` before any; it may be out of
        date, since other viewers move the session too."""
        return self._last

    def _note(self, state: State) -> None:
        """Keep `state` unless it is older than the one kept (the same session, a lower revision)."""
        old = self._last
        if old is None or old.session != state.session or state.revision >= old.revision:
            self._last = state

    def _reply(self, reply: ControlReply, dry_run: bool) -> State:
        """The state of a control's reply (a dry run's is the state it would make, so it is not kept)."""
        if not dry_run:
            self._note(reply.state)
        return reply.state

    async def state(self) -> State:
        """The shared state now (`GET /session`)."""
        state = (await self._api.get_session()).state
        self._note(state)
        return state

    async def watch(self, source: str | Source, at: float | None = None, *, dry_run: bool = False) -> State:
        """Make the shared session show `source` (an id such as `"dry1"` or a camera's serial, or a `Source`), for
        every viewer.

        Plays first if the session is paused (a select keeps the paused or playing state, and playing first gives
        the worker its hold to warm up), then selects. `at` is a media time; a live camera has none: giving one for a
        camera `Source`, or for an id the catalog lists as a camera, raises `not_seekable` (field `time`) without
        the select. Returns the new state: on a camera it is `connecting`, and `until_live()` waits out the
        connection and the warm-up.
        """
        sid = source.id if isinstance(source, Source) else source
        if at is not None and await self._is_camera(source):
            raise _not_seekable(f"{sid} is a live camera: it has no time; leave `at` out", "time")
        if not (await self.state()).playing:
            self._reply(await self._api.session_play(dry_run=dry_run), dry_run)
        return self._reply(await self._api.session_select(sid, time=at, dry_run=dry_run), dry_run)

    async def _is_camera(self, source: str | Source) -> bool:
        """Whether `source` is a live camera: a `Source` says so itself; an id is looked up in the catalog."""
        if isinstance(source, Source):
            return source.is_live
        if source == LIVE:
            return True
        catalog = await self._api.list_videos()
        return any(s.id == source and s.is_live for s in catalog.videos) if catalog is not None else False

    async def seek(self, t: float, *, video: str | None = None, dry_run: bool = False) -> State:
        """Move the shared playhead to media time `t`, for every viewer (a new session starts).

        A live camera cannot be seeked; pause and play instead. Without `video`, the SDK reads the shared state first
        (`GET /session`: another viewer may have switched the session since this client last looked), and when it
        shows a camera it raises `not_seekable` (field `video`) without sending the seek. With the id of a recording
        in `video`, it sends the seek in that recording without reading the state. The unit refuses a seek whose video
        is a camera (409 `not_seekable`): one naming a camera, or one without `video` that races another viewer's
        switch to a camera. A seek naming a recording is applied even while a camera shows, and moves every viewer to
        that recording.
        """
        if video == LIVE or (video is None and _is_live(await self.state())):
            raise _not_seekable("a live camera cannot be seeked; pause and play instead", "video")
        return self._reply(await self._api.session_seek(t, video=video, dry_run=dry_run), dry_run)

    async def pause(self, *, dry_run: bool = False) -> State:
        """Pause the shared playhead for every viewer (on a live camera: freeze the shared view and stop the
        interpreter)."""
        return self._reply(await self._api.session_pause(dry_run=dry_run), dry_run)

    async def play(self, *, dry_run: bool = False) -> State:
        """Resume the shared playhead for every viewer (on a live camera: continue at the live edge, after a new
        warm-up; `g` jumps over the pause)."""
        return self._reply(await self._api.session_play(dry_run=dry_run), dry_run)

    async def until_live(self, timeout: float = 30.0) -> State:
        """Wait until the session's camera shows live frames (`state.live.phase` is `live`) and return that state.

        After `watch(camera)` the session is `connecting` until the camera's first frame, then `warming` while the
        interpreter runs on its first `hold` seconds (3 on the plant cameras; nothing is released meanwhile), then
        `live`; after `play()`, and when a playing camera sent no frame for 5 s, it is `connecting` again until a new
        warm-up ends. This reads the shared state at most every 0.25 s and sends no control; on a recording, and on a
        paused camera that was live (a pause keeps the phase), it returns the state at once. The unit interprets only
        while someone watches: an open stream (the interpreter's), the page, or for 15 s the address that sent a control
        that is not a dry run (its worker idles 20 s after the last). A `watch()` that is not a dry run counts, so
        `watch()` then `until_live()` goes live on a unit nobody else watches, while a camera session nobody watches
        stays `connecting` (the worker's reason: `nobody is watching`). After `timeout` seconds it raises
        `live_timeout`, with the phase it saw last in the message (the worker's reason in `factory.api.get_session()`
        says why: nobody watching, or a camera that does not answer).
        """
        deadline = self._monotonic() + timeout
        while True:
            state = await self.state()
            if state.live is None or state.live.phase == "live":
                return state
            left = deadline - self._monotonic()
            if left <= 0:
                raise FactoryError(
                    code="live_timeout",
                    message=f"{state.video} is still {state.live.phase} after {timeout:g} s",
                )
            await self._sleep(min(UNTIL_LIVE_POLL, left))

    async def picture(self, g: GridFrame = "latest", *, session: int | None = None) -> Picture:
        """The camera picture live frame `g` (the newest by default) was interpreted from: `Picture(g, jpeg)`.

        Draw a frame's layers over its own picture: `await session.picture(frame.g)`. The newest 64 are kept (older:
        `not_retained`); a recording has none (`not_found`); `session` refuses a frame of an ended session
        (`session_gone`).
        """
        return await self._api.get_frame_picture(g, session=session)

    async def focus(
        self, stone_id: int, *, g: int | None = None, session: int | None = None, dry_run: bool = False
    ) -> State:
        """Highlight stone `stone_id` for every viewer (`g`: the frame it was picked on; `session`: refuse if stale)."""
        return self._reply(await self._api.put_focus(stone_id, g=g, session=session, dry_run=dry_run), dry_run)

    async def clear_focus(self, *, dry_run: bool = False) -> State:
        """Clear the highlight for every viewer."""
        return self._reply(await self._api.delete_focus(dry_run=dry_run), dry_run)

    async def clock(self) -> SessionClock:
        """Sample the shared clock (`GET /session`) with the request's round trip."""
        t_send = self._wall()
        reply = await self._api.get_session()
        t_recv = self._wall()
        self._note(reply.state)
        return SessionClock(
            state=reply.state,
            server_now=reply.server_now,
            offset=reply.server_recv - (t_send + t_recv) / 2.0,
            rtt=t_recv - t_send,
            local_clock=self._wall,
        )


def _is_live(state: State) -> bool:
    return state.live is not None or state.video == LIVE

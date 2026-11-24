"""Testing tools: a fake pDigger Factory server, so code that uses the SDK is unit-tested offline (section 12).

`FakeFactory` is an httpx transport (async, and sync for `Replay` URLs) that answers the v2 API:

- from a recording (a `pfr1` file or its lines): the catalog (the header's `source` entry), the shared state (its
  clock lines), `frames/{g}`, `world`, `world/events`, `world/stones/{id}`, `raw/packets/{g}`, the long-poll and the
  `frames/stream` and `raw/stream` streams (SSE and NDJSON) over its frames, honouring `layers`, `after`,
  `Last-Event-ID`, `limit` and `max_s` (`max_s` counts media seconds: no clock runs), with its clock and worker lines
  as side events. As on the unit, a stream gets no `clock` event on connect: the clock lines before its first frame
  are the state it starts in (`GET /session` has it), and the later ones are sent as the changes they were. The
  long-poll takes them as it passes them too: a reply ends before the next line, and the next reply carries that
  line's state with the frames after it; `GET /session` answers the state of the last line a stream or poll passed;
- from recorded replies (the `fixtures/api` layout: `INDEX.json` plus files, or a map of `Reply`s) for what a
  recording cannot answer (topology, model, metrics, renders, rasters...), and for everything when there is neither a
  recording nor a camera;
- controls validated like the server, in its error shape, codes and order (`dry_run` honoured: 0 or 1), applied to
  the fake's state: a select or seek starts a new session (its frames are the recording's from that time on), and
  every change (a phase of the camera too) bumps `revision` and is sent as a `clock` event;
- fault injection: an error reply or a broken connection for the next N requests of an operation (`fail`), a stream
  that breaks after K events (`drop_after`), injected `gap`, `reset`, `clock` or `worker` events (`inject`), a delay
  hook; nothing sleeps for real.

With `live=True` it also offers a live camera, `cam-live` (`LIVE_CAMERA`), as the 20.20 unit (build live3) offers its
plant cameras: listed after the recording, and shown by the session at the start, already live (also without a
recording: then it is live with no frames, nothing is retained, the world is unavailable and it has no world events).
The camera sends the recording's frames as one session (`g` from 0 and `i` the same, `t` the arrival since the session
started; the raw packet's `g`, `i`, `t` and `cursor` are the frame's). A select of the camera starts a session as the
unit does, `connecting` (for `CONNECT_S`), then `warming` while the interpreter runs on the first `hold` (3) seconds of
frames, which are never released, then `live` (`LIVE_LAG_S` behind the camera). The phases follow the fake's clock while
the session plays (`session.until_live()` sleeps on it, `advance()` moves it); nothing runs while it is paused (a pause
keeps the phase). A play goes back to `connecting` in its own reply and plans the phases again from then (as on the
unit, warming changes only the phase: its `lag` stays the last one); on a camera that was live, the camera's next frames
come after a break (`g` jumps by 5) and their warm-up is never released either. A stream that comes to a phase before
its time moves the fake's clock on to it, and so does a long-poll for the phases within its `wait`; a stream on a paused
camera waits for a control. The newest `KEEP_PICTURES` (64) released frames have a picture (`frames/{g}/image.jpg`, a
small JPEG); the camera has no media (404), a seek in it is refused (409 `not_seekable`, field `video`; a seek naming a
recording is applied and leaves the camera) and so is a select with any time, null too (400, field `time`).

A resuming stream (the Interpreter's default) never ends by itself; once the fake has served the end of its recording,
the next request that would get no more frames raises `FakeExhausted`, which ends the test's loop:

    import pytest
    from pdigger_factory_sdk import FactoryClient
    from pdigger_factory_sdk.testing import FakeExhausted, FakeFactory

    async def test_counts_lifts() -> None:
        fake = FakeFactory(recording="fixtures/recordings/dry1-0000-0130.ndjson.gz")
        lifts = 0
        async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep) as factory:
            with pytest.raises(FakeExhausted):
                async for event in factory.interpreter().events():
                    lifts += event.type == "lift"
        assert lifts == 9
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import json
import math
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeGuard
from urllib.parse import unquote

import httpx

from ._defaults import DEFAULTS, Defaults

__all__ = [
    "CONNECT_S",
    "KEEP_PICTURES",
    "LIVE_CAMERA",
    "LIVE_LAG_S",
    "DelayFn",
    "FakeExhausted",
    "FakeFactory",
    "FakeTime",
    "Reply",
    "use_fake",
]

LIVE_CAMERA = "cam-live"
"""The id of the fake's live camera (`FakeFactory(live=True)`)."""
CONNECT_S = 0.5
"""Seconds the fake's camera takes, after a select, to send its first frame (the session is `connecting` meanwhile)."""
LIVE_LAG_S = 0.25
"""Seconds the fake's live picture is behind its camera (the interpreter's time): `state.live.lag`."""
KEEP_PICTURES = 64
"""How many camera pictures the fake keeps, as the unit's worker does: those of the newest released frames of the
camera session; an older frame that is still retained has none (404 `not_retained`)."""
_LIVE_HOLD = 3600.0
"""On a camera, `hold_until` stays this far ahead of the select until the session goes live (as on the unit)."""
_LIVE_GAP = 4
"""Grid frame numbers a camera session skips at a break, as the unit's worker does: the first frame after a play of a
camera that was live is the last one's `g` + 1 + 4."""
_G = re.compile(r"latest|[0-9]{1,10}")
"""The unit's grid frame parameter: an integer of 1 to 10 digits, or `latest`."""
_INTEGER = re.compile(r"[+-]?[0-9]+")
_WAIT = (None, math.inf)
"""What a stream yields to wait for a control (a paused camera's next phase comes only after a play)."""
_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABQODxIPDRQSEBIXFRQYHjIhHhwcHj0sLiQySUBMS0dARkVQWnNiUFVtVkVGZIhlbXd7gYKBTmCNl4x9"
    "lnN+gXz/2wBDARUXFx4aHjshITt8U0ZTfHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHz/wAARCAAJABAD"
    "ASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAP/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAH/xAAUEQEA"
    "AAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCYCD//2Q=="
)
"""A 16 x 9 grey JPEG: the picture of every frame of the fake's camera (with the frame in a comment segment)."""

LAYERS = ("raw", "overlay", "scene", "cards", "world")
DEFAULT_LAYERS = ("overlay", "scene", "cards", "world")
EVENT_TYPES = ("new", "confirm", "freeze", "push", "lift", "hinge", "carry", "place", "drop", "gone", "delete", "split")

DelayFn = Callable[[str, int], Awaitable[None]]
"""`delay(operation, index)`: awaited before event number `index` of a stream of `operation`."""


class FakeExhausted(Exception):
    """A stream or long-poll asked for more after the fake served the end of its recording."""


_DEFAULT_WALL = 1794958239.0
"""`FakeTime`'s default wall clock, epoch seconds: 2026-11-17 23:30:39 UTC, when the session of the SDK's first
dry1 fixtures began."""


class FakeTime:
    """A clock that moves only when slept on: `sleep(s)` advances it by `s` at once (and yields to the event loop)."""

    def __init__(self, wall: float = _DEFAULT_WALL, monotonic: float = 1000.0) -> None:
        self._wall = wall
        self._mono = monotonic
        self.slept: list[float] = []
        """Every sleep asked for, in seconds."""

    def time(self) -> float:
        """The fake wall clock, epoch seconds."""
        return self._wall

    def monotonic(self) -> float:
        """The fake monotonic clock."""
        return self._mono

    def advance(self, seconds: float) -> None:
        """Move both clocks forward."""
        self._wall += seconds
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        """Advance by `seconds` (no real waiting) and let other tasks run once."""
        self.slept.append(seconds)
        self.advance(max(0.0, seconds))
        await asyncio.sleep(0)


@contextlib.contextmanager
def use_fake(fake: FakeFactory, *, seed: int = 0) -> Iterator[FakeFactory]:
    """Within the block, every `FactoryClient` and `Replay` made without a transport uses `fake` and its `FakeTime`.

    For code that creates its own client, such as an example script.
    """
    token = DEFAULTS.set(
        Defaults(transport=fake, sleep=fake.time.sleep, monotonic=fake.time.monotonic, wall=fake.time.time, seed=seed)
    )
    try:
        yield fake
    finally:
        DEFAULTS.reset(token)


@dataclass(slots=True)
class Reply:
    """A canned reply: status, body (bytes, text, or a JSON-able value), content type and headers."""

    status: int = 200
    body: Any = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    def content(self) -> bytes:
        """The body as bytes."""
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            return self.body.encode("utf-8")
        return _dumps(self.body).encode("utf-8")

    def json(self) -> Any:
        """The body decoded as JSON, or `None` for an error reply or a body that is not JSON."""
        if self.status >= 300:
            return None
        try:
            return json.loads(self.content())
        except ValueError:
            return None


@dataclass(slots=True)
class _Fault:
    reply: Reply | BaseException
    times: int


@dataclass(slots=True)
class _Injection:
    event: str
    data: dict[str, Any]
    after: int
    times: int


@dataclass(slots=True)
class _Entry:
    """One playlist line: a frame (with its event cursor `seq`) or a side event."""

    kind: str
    data: dict[str, Any]
    seq: int = 0
    applied: bool = False
    """A clock line whose state the fake has taken (a stream started after it or passed it, or the fake made it)."""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _error(status: int, code: str, message: str, field: str | None = None, **extra: Any) -> Reply:
    body: dict[str, Any] = {"error": message, "code": code}
    if field is not None:
        body["field"] = field
    return Reply(status=status, body={**body, **extra})


def _read_recording(source: str | os.PathLike[str]) -> list[dict[str, Any]]:
    raw = Path(source).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        with contextlib.suppress(ValueError, RecursionError):  # a line nested too deep does not decode either
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _read_replies(source: str | os.PathLike[str] | Mapping[str, Any]) -> dict[str, Reply]:
    if isinstance(source, Mapping):
        return {k: v if isinstance(v, Reply) else Reply(body=v) for k, v in source.items()}
    root = Path(source)
    index = json.loads((root / "INDEX.json").read_text(encoding="utf-8"))
    return {
        key: Reply(
            status=int(entry.get("status", 200)),
            body=(root / entry["file"]).read_bytes(),
            content_type=entry.get("content_type", "application/json"),
            headers={k: v for k, v in entry.get("headers", {}).items() if k.lower() != "content-length"},
        )
        for key, entry in index.items()
    }


_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    (method, re.compile(pattern), op)
    for method, pattern, op in (
        ("GET", r"/api/v2", "getApiRoot"),
        ("GET", r"/api/v2/openapi\.yaml", "getOpenApi"),
        ("GET", r"/api/v2/model", "getModel"),
        ("GET", r"/api/v2/topology", "getTopology"),
        ("GET", r"/api/v2/health", "getHealth"),
        ("GET", r"/api/v2/ready", "getReady"),
        ("GET", r"/api/v2/status", "getStatus"),
        ("GET", r"/metrics", "getMetrics"),
        ("GET", r"/api/v2/videos", "listVideos"),
        ("GET", r"/api/v2/videos/(?P<id>[^/]+)", "getVideo"),
        ("GET", r"/api/v2/videos/(?P<id>[^/]+)/media", "getVideoMedia"),
        ("HEAD", r"/api/v2/videos/(?P<id>[^/]+)/media", "getVideoMedia"),
        ("GET", r"/api/v2/session", "getSession"),
        ("POST", r"/api/v2/session/select", "sessionSelect"),
        ("POST", r"/api/v2/session/seek", "sessionSeek"),
        ("POST", r"/api/v2/session/play", "sessionPlay"),
        ("POST", r"/api/v2/session/pause", "sessionPause"),
        ("GET", r"/api/v2/session/focus", "getFocus"),
        ("PUT", r"/api/v2/session/focus", "putFocus"),
        ("DELETE", r"/api/v2/session/focus", "deleteFocus"),
        ("GET", r"/api/v2/session/world", "getWorld"),
        ("GET", r"/api/v2/session/world/events", "getWorldEvents"),
        ("GET", r"/api/v2/session/world/stones/(?P<id>[^/]+)", "getStone"),
        ("GET", r"/api/v2/session/frames/stream", "streamFrames"),
        ("GET", r"/api/v2/session/frames", "pollFrames"),
        ("GET", r"/api/v2/session/frames/(?P<g>[^/]+)/image\.jpg", "getFramePicture"),
        ("GET", r"/api/v2/session/frames/(?P<g>[^/]+)/(?P<raster>[^/]+)\.png", "getFrameRaster"),
        ("GET", r"/api/v2/session/frames/(?P<g>[^/]+)", "getFrame"),
        ("GET", r"/api/v2/raw/stream", "rawStream"),
        ("GET", r"/api/v2/raw/packets/(?P<g>[^/]+)", "rawPacket"),
        ("GET", r"/api/v2/raw/tensors/(?P<g>[^/]+)", "rawTensors"),
        ("GET", r"/api/v2/session/render/overlay2d", "renderOverlay2d"),
        ("GET", r"/api/v2/session/render/scene3d", "renderScene3d"),
    )
]
_API_PATH = re.compile(r".*?(/api/v2(?:/.*)?|/metrics)")
_STREAMS = frozenset({"streamFrames", "rawStream"})
_CONTROLS = frozenset({"sessionSelect", "sessionSeek", "sessionPlay", "sessionPause", "putFocus", "deleteFocus"})
_CID = re.compile(r"[A-Za-z0-9_-]{8,40}")


def _int_param(value: str | None, default: int, name: str, lo: int, hi: int, unit: str = "") -> int | Reply:
    """An integer query parameter within `lo..hi`, its default, or the server's 400 (`unit` ends its range: ` ms`)."""
    if value is None:
        return default
    try:
        n = int(value)
    except ValueError:
        return _error(400, "invalid_param", f"{name} must be an integer", name)
    if not lo <= n <= hi:
        return _error(400, "invalid_param", f"{name} must be within {lo}..{hi}{unit}", name)
    return n


def _ints(params: httpx.QueryParams, specs: Sequence[tuple[str, int, int, int]]) -> list[int] | Reply:
    """Several integer parameters `(name, default, lo, hi)`, or the first 400 (`wait` is in milliseconds)."""
    out = []
    for name, default, lo, hi in specs:
        n = _int_param(params.get(name), default, name, lo, hi, " ms" if name == "wait" else "")
        if isinstance(n, Reply):
            return n
        out.append(n)
    return out


def _layers_param(value: str | None) -> list[str] | Reply:
    if value is None:
        return list(DEFAULT_LAYERS)
    layers = [x.strip() for x in value.split(",") if x.strip()]
    for layer in layers:
        if layer not in LAYERS:
            return _error(400, "unknown_layer", f"unknown layer '{layer}'", "layers")
    return layers


def _filter_layers(frame: Mapping[str, Any], layers: Sequence[str]) -> dict[str, Any]:
    out = {k: v for k, v in frame.items() if (k not in LAYERS and k != "missing") or k in layers}
    missing = [x for x in frame.get("missing") or [] if x in layers]
    if missing:
        out["missing"] = missing
    return out


def _number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _start_wall(lines: Sequence[Mapping[str, Any]], frames: Sequence[Mapping[str, Any]], session: Any) -> float:
    """Where a fake's clock starts (see `FakeFactory`'s `time`): the wall time at which the recording's first clock
    line has the playhead at the first frame (playing: `max(anchor_wall, hold_until) + (t - anchor_media) / rate`, or
    the end of the hold when the first frame comes during it, before the anchor), or the line's `server_now` (paused,
    or a frame of another session); else the recorded `getSession` reply's `server_now`; else `FakeTime`'s default."""
    clock: Mapping[str, Any] = next(
        (x["data"] for x in lines if x.get("sse") == "clock" and isinstance(x.get("data"), dict)), {}
    )
    keys = ("anchor_media", "anchor_wall", "hold_until", "rate")
    first = frames[0] if frames else {}
    if (
        clock.get("playing") is True
        and all(_number(clock.get(k)) for k in keys)
        and clock["rate"] > 0
        and first.get("session") == clock.get("session")
        and _number(first.get("t"))
    ):
        start = max(float(clock["anchor_wall"]), float(clock["hold_until"]))
        return start + max(0.0, float(first["t"]) - float(clock["anchor_media"])) / float(clock["rate"])
    for now in (clock.get("server_now"), session.get("server_now") if isinstance(session, dict) else None):
        if _number(now):
            return float(now)
    return _DEFAULT_WALL


def _playhead(state: Mapping[str, Any], now: float) -> Any:
    """The shared playhead at wall time `now`, as the unit computes `media_time`: `anchor_media`, plus
    `(now - max(anchor_wall, hold_until)) * rate` while it plays, at most the `duration`. A state whose anchors are not
    numbers keeps its own `media_time`."""
    if not all(_number(state.get(k)) for k in ("anchor_media", "anchor_wall", "hold_until", "rate")):
        return state.get("media_time")
    t = float(state["anchor_media"])
    if state.get("playing") is True:
        t += max(0.0, now - max(float(state["anchor_wall"]), float(state["hold_until"]))) * float(state["rate"])
    duration = state.get("duration")
    return min(t, float(duration)) if _number(duration) else t


def _camera_source(like: Mapping[str, Any] | None) -> dict[str, Any]:
    """The catalog entry of the fake's live camera, sized like the recording's source (its grid frame rate, 5.0 when it
    has no `fps`; its sizes, else the unit's), shaped as a plant camera's."""
    like = like or {}
    fps, step = like.get("fps"), like.get("grid_step")
    grid_fps = float(fps) / max(float(step) if _number(step) else 0.0, 1.0) if _number(fps) and fps > 0 else 5.0
    proxy_wh, native_wh = like.get("proxy_wh"), like.get("native_wh")
    return {
        "id": LIVE_CAMERA,
        "title": "Live camera",
        "subtitle": "The recording, replayed as a live camera",
        "kind": "camera",
        "live": True,
        "seekable": False,
        "duration": None,
        "frames": None,
        "default_start": None,
        "fps": math.floor(grid_fps * 10 + 0.5) / 10,  # to 0.1, halves away from zero
        "grid_step": 1,
        "hold": 3.0,
        "proxy": "",
        "proxy_wh": proxy_wh if isinstance(proxy_wh, list) else [960, 540],
        "native_wh": native_wh if isinstance(native_wh, list) else [1920, 1080],
        "held_out": [],
        "station": "FakeFactory",
    }


def _time_range(src: Mapping[str, Any]) -> str:
    """The unit's message for a time outside a source (one without a duration has no end)."""
    duration = src.get("duration")
    return f"time must be within 0..{float(duration):.3f} s" if _number(duration) else "time must be at least 0 s"


def _packet(frame: Mapping[str, Any], cursor: Any) -> dict[str, Any]:
    """A frame's raw packet as the unit serves it: the frame's event cursor, session, video and time in it."""
    own = {k: frame[k] for k in ("session", "video", "g", "i", "t") if k in frame}
    return {**frame["raw"], **own, "cursor": cursor}


def _picture(video: str, g: int) -> bytes:
    """The fake camera's JPEG of frame `g`: `_JPEG` with `FakeFactory <video> g <g>` in a comment segment."""
    note = f"FakeFactory {video} g {g}".encode()
    return _JPEG[:2] + b"\xff\xfe" + (len(note) + 2).to_bytes(2, "big") + note + _JPEG[2:]


class FakeFactory(httpx.AsyncBaseTransport, httpx.BaseTransport):
    """A fake pDigger Factory server, as an httpx transport: pass it as `FactoryClient(..., transport=fake)`.

    Args:
        recording: A `pfr1` recording (`.ndjson` or `.ndjson.gz` path) or its lines as dicts.
        replies: A `fixtures/api` directory (`INDEX.json` plus files), or a map of reply keys (`getModel`,
            `getFrame-latest`, ...) to `Reply`s or JSON values. With a recording, the operations the recording
            answers come from it and replies answer the rest.
        live: Offer the live camera `cam-live`, made from the recording, and show it at the start, already live
            (`live_phase()` is its phase, `advance()` moves the clock on to the next ones). Without a recording the
            camera has no frames; the fake still answers the frames, the world, the long-poll and the streams itself.
        boot: The fake server's boot id (frame event ids are `<boot>-<cursor>`).
        time: The `FakeTime` of waits and `server_now`. By default a new one, set to where the recording's first clock
            line has the shared playhead at the recording's first frame (at that line's `server_now` while its state
            is paused), so the fake's state agrees with the frames it serves: `session.clock().media_time_now()` is
            the first frame's media time (the line's `anchor_media` when it is paused before the first frame, as in
            `dry2-0582-0600`, or when the first frame comes during its hold, before the anchor, as in
            `dry2-pause-seek`). Without clock lines it starts at the recorded `getSession` reply's
            `server_now`, else at `FakeTime`'s default, and (without a recorded state either) the session plays the
            recording from its first frame at once, without a select's hold.
        delay: A hook awaited before each streamed event (`delay(operation, index)`).
        realtime: Pace streamed frames at their media times, sleeping with `sleep`.
        linger: Seconds a stream that reached its `limit` or `max_s` stays open, sending nothing, before the fake
            closes it (slept with `sleep`). The 20.20 unit holds a finished stream about 10 s; the default 0 closes
            it at once.
        sleep: The sleep of `realtime`, `linger` and long-poll waits (default `time.sleep`, which does not really
            wait).
        on_exhausted: `raise` (default): once a stream has served the end of the recording, a request for a stream or
            a long-poll that would get no more frames raises `FakeExhausted`. `end`: such a stream just ends and a
            long-poll waits, as an idle server would (use it with `resume=False`).
        files: Static files to serve by URL path (for `Replay` over a URL), as paths or bytes.

    A `Last-Event-ID` of another boot gets a `reset` event and then the recording from its start (a real server
    continues from its newest frame; the fake has no "now").
    """

    def __init__(
        self,
        recording: str | os.PathLike[str] | Sequence[Mapping[str, Any]] | None = None,
        replies: str | os.PathLike[str] | Mapping[str, Any] | None = None,
        *,
        live: bool = False,
        boot: str = "fakeb00t01",
        time: FakeTime | None = None,
        delay: DelayFn | None = None,
        realtime: bool = False,
        linger: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_exhausted: Literal["raise", "end"] = "raise",
        files: Mapping[str, str | os.PathLike[str] | bytes] | None = None,
    ) -> None:
        self.boot = boot
        self.live = live
        self.delay = delay
        self.realtime = realtime
        self.linger = linger
        self.on_exhausted = on_exhausted
        self.replies: dict[str, Reply] = {} if replies is None else _read_replies(replies)
        self.files = {k: v if isinstance(v, bytes) else Path(v).read_bytes() for k, v in (files or {}).items()}
        self.requests: list[httpx.Request] = []
        """Every request received, in order."""
        self.streams_opened = 0
        self.streams_closed = 0
        self.viewers = 0
        """Open streams that count as viewers (not `observe=1`); unlike the unit, the fake counts neither a long-poll
        with a `cid` nor a control as one."""
        self.cids: set[str] = set()
        """The client ids of the long-polls seen (recorded; `viewers` does not count them)."""
        self.exhausted = False
        """True once a stream reached the end of the current session's playlist."""
        self._faults: dict[str, list[_Fault]] = {}
        self._drops: dict[str, list[int]] = {}
        self._injections: dict[str, list[_Injection]] = {}
        self._next_seq = 0
        self._phases: list[tuple[float, dict[str, Any]]] = []
        """The camera session's phases still to come: each state with the wall time it begins."""
        self._held: list[dict[str, Any]] = []
        """The frames of the camera's run released when it goes live (those after its warm-up)."""
        self._cam_g, self._cam_t = 0, 0.0
        """The `g` and `t` after the camera's last run: where its next run goes on after a break."""
        self._change: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None
        if isinstance(recording, (str, os.PathLike)):
            lines = _read_recording(recording)
        else:
            lines = [dict(x) for x in recording or []]
        self.header: dict[str, Any] | None = next((x for x in lines if "recording" in x), None)
        self._lines = [x for x in lines if x is not self.header]
        self._frames = [x for x in self._lines if "g" in x and "cursor" in x]
        self.has_recording = bool(self._frames) or live
        """True when the fake answers frames, the world and streams itself: from the recording's frames, or from its
        camera's (`live=True`, also without a recording); else everything comes from the replies."""
        recorded_session = self._recorded("getSession") or {}
        self.time = time or FakeTime(wall=_start_wall(self._lines, self._frames, recorded_session))
        self.sleep = sleep or self.time.sleep
        source = (self.header or {}).get("source")
        named = self._frames[0].get("video") if self._frames else None
        if not isinstance(source, dict) and isinstance(named, str) and named:  # the video the frames name, as Go's
            source = {
                "id": named,
                "title": named,
                "duration": None,
                "fps": 10.0,
                "frames": None,
                "grid_step": 2,
                "default_start": 0.0,
                "hold": 2.2,
                "proxy": f"/api/v2/videos/{named}/media",
                "proxy_wh": [960, 540],
                "native_wh": [1920, 1080],
            }
        listed = self._recorded("listVideos") or {}
        self.catalog: list[dict[str, Any]] = (
            [dict(source)] if isinstance(source, dict) else list(listed.get("videos", []))
        )
        """The sources the fake offers."""
        if live:
            self.catalog.append(_camera_source(self.catalog[0] if self.catalog else None))
        self.state: dict[str, Any] = self._initial_state(recorded_session.get("state"))
        """The fake's shared state (the `State` schema), as of the last request (`advance()` and `live_phase()` take
        a camera's phases whose time has come)."""
        side = [x["data"] for x in self._lines if x.get("sse") == "worker" and isinstance(x.get("data"), dict)]
        self.worker: dict[str, Any] | None = side[-1] if side else recorded_session.get("worker")
        """The worker status the fake reports: the recording's last worker line or the recorded `getSession` reply's,
        else (`None`) running, without a reason, in the session the fake shows."""
        self.stable: dict[str, Any] = recorded_session.get("stable", {"state": "running"})
        self._playlist: list[_Entry] = []
        if live:
            self._offer_camera()
        else:
            self._playlist = self._first_playlist()

    # ---- fault injection -------------------------------------------------------------------------------------------
    def fail(
        self,
        operation: str,
        *,
        status: int = 503,
        code: str = "busy",
        message: str = "injected by FakeFactory",
        times: int = 1,
        retry_ms: int | None = None,
        retry_after: str | None = None,
        body: bytes | None = None,
        exc: BaseException | None = None,
    ) -> None:
        """The next `times` requests of `operation` (an operationId such as `getFrame`) get this error reply, or
        raise `exc` (for example `httpx.ConnectError("refused")`) as a broken connection would."""
        if exc is not None:
            self._faults.setdefault(operation, []).append(_Fault(exc, times))
            return
        err: dict[str, Any] = {"error": message, "code": code}
        if retry_ms is not None:
            err["retry_ms"] = retry_ms
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        reply = Reply(status=status, body=_dumps(err).encode() if body is None else body, headers=headers)
        self._faults.setdefault(operation, []).append(_Fault(reply, times))

    def drop_after(self, events: int, *, operation: str = "streamFrames", times: int = 1) -> None:
        """The next `times` streams of `operation` break (a read error) after `events` events."""
        self._drops.setdefault(operation, []).extend([events] * times)

    def inject(
        self, event: str, data: Mapping[str, Any], *, operation: str = "streamFrames", after: int = 0, times: int = 1
    ) -> None:
        """The next `times` streams of `operation` send the side event `event` (`gap`, `reset`, `clock`, `worker`)
        with `data` after `after` events (NDJSON streams carry only `gap`, as `{"gap": n}` lines)."""
        self._injections.setdefault(operation, []).append(_Injection(event, dict(data), after, times))

    # ---- state -----------------------------------------------------------------------------------------------------
    def _recorded(self, key: str) -> Any:
        """The JSON of a successful recorded reply, or `None`."""
        reply = self.replies.get(key)
        return None if reply is None else reply.json()

    def _initial_state(self, recorded: Any) -> dict[str, Any]:
        """The state the fake starts in: its camera's, live (`live=True`); else the recording's first clock line, the
        recorded `getSession` reply's state, or the recording's own state (even when its source is a camera)."""
        first = self._frames[0] if self._frames else {}
        session = int(first.get("session") or 1)
        if self.live:  # as if the camera was selected just long enough ago to be live now
            live_t = self._camera_run(LIVE_CAMERA, session, 0, 0.0)[1]
            since = self.time.time() - live_t - LIVE_LAG_S
            start = self._connecting(LIVE_CAMERA, session, 1, since, True, "start")
            return self._camera_phases(start, since, live_t)[1][1]
        clocks = [x["data"] for x in self._lines if x.get("sse") == "clock" and isinstance(x.get("data"), dict)]
        if clocks:
            state = {k: v for k, v in clocks[0].items() if k != "server_now"}
            state.setdefault("media_time", state.get("anchor_media", 0.0))
            return state
        if isinstance(recorded, dict):
            return dict(recorded)
        video = str(first.get("video") or (self.catalog[0]["id"] if self.catalog else ""))
        held = self.time.time()  # a recording plays from the start, without a select's hold (as Go's initialState)
        return self._recording_state(video, float(first.get("t") or 0.0), session, 1, True, "start", held)

    def _served(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """A state as the fake sends it: `media_time` is the shared playhead on the fake's clock now."""
        return {**state, "media_time": _playhead(state, self.time.time())}

    def _source(self, video: str) -> dict[str, Any] | None:
        return next((s for s in self.catalog if s.get("id") == video), None)

    def _is_camera(self, video: str) -> bool:
        """A live camera: one the catalog lists as live, or the design's old id `live` (as Go's fake decides)."""
        return video == "live" or (self._source(video) or {}).get("live") is True

    def _new_state(self, video: str, at: float, session: int, revision: int, playing: bool) -> dict[str, Any]:
        """The state of a new session that a viewer starts on `video` from media time `at` (a camera: connecting)."""
        now = self.time.time()
        if self._is_camera(video):
            return self._connecting(video, session, revision, now, playing, "viewer")
        hold = (self._source(video) or {}).get("hold")
        held = now + (float(hold) if _number(hold) else 1.4)
        return self._recording_state(video, at, session, revision, playing, "viewer", held)

    def _recording_state(
        self, video: str, at: float, session: int, revision: int, playing: bool, changed_by: str, hold_until: float
    ) -> dict[str, Any]:
        return {
            "session": session,
            "revision": revision,
            "video": video,
            "playing": playing,
            "rate": 0.8,
            "anchor_media": at,
            "anchor_wall": self.time.time(),
            "hold_until": hold_until,
            "duration": (self._source(video) or {}).get("duration"),
            "changed_by": changed_by,
            "focus": None,
            "media_time": at,
        }

    # ---- the live camera -------------------------------------------------------------------------------------------
    def _warmup(self, video: str) -> float:
        """The warm-up of camera `video`: its `hold`, 3 s by default."""
        hold = (self._source(video) or {}).get("hold")
        return float(hold) if _number(hold) and hold > 0 else 3.0

    def _camera_run(
        self, video: str, session: int, g0: int, t0: float
    ) -> tuple[list[dict[str, Any]], float, int, float]:
        """A run of camera `video`'s frames in session `session`: the recording's frames, its sessions end to end, as
        the camera sends them once connected at session time `t0` (seconds since the session started), numbered from
        `g0` and arriving from `CONNECT_S` after `t0` at the recording's pace. Returns the frames released after the
        run's warm-up (those of its first `hold` seconds are numbered but never released; they get their cursors when
        they are released), the `t` the run goes live at (its first released frame's; the warm-up's end when there is
        none), and the `g` and `t` after its last frame."""
        warm_end = t0 + CONNECT_S + self._warmup(video)
        released: list[dict[str, Any]] = []
        segment: Any = None
        base_g, base_t, rec_g0, rec_t0 = 0, 0.0, 0, 0.0
        next_g, next_t = g0, t0 + CONNECT_S
        for n, frame in enumerate(self._frames):
            if n == 0 or frame.get("session") != segment:
                segment, base_g, base_t = frame.get("session"), next_g, next_t
                rec_g0, rec_t0 = int(frame["g"]), float(frame.get("t") or 0.0)
            g = base_g + int(frame["g"]) - rec_g0
            t = round(base_t + float(frame.get("t") or 0.0) - rec_t0, 6)
            next_g, next_t = max(next_g, g + 1), max(next_t, t + 0.2)
            if t >= warm_end - 1e-9:
                released.append(self._camera_frame(frame, session, video, g, t))
        return released, float(released[0]["t"]) if released else warm_end, next_g, next_t

    @staticmethod
    def _camera_frame(frame: Mapping[str, Any], session: int, video: str, g: int, t: float) -> dict[str, Any]:
        """A recorded frame as frame `g` (at `t`) of camera `video`'s session `session`, numbered as the unit numbers a
        camera's frames: `i` = `g`, the world's and the raw packet's `g` and `t` the frame's (the packet's `i` too),
        the overlay's `prev_g` moved with `g`."""
        out = {**frame, "session": session, "video": video, "g": g, "i": g, "t": t}
        world, raw, overlay = out.get("world"), out.get("raw"), out.get("overlay")
        if isinstance(world, dict):
            out["world"] = {**world, "session": session, "video": video, "g": g, "t": t}
        if isinstance(raw, dict):
            out["raw"] = {**raw, "session": session, "video": video, "g": g, "i": g, "t": t}
        if isinstance(overlay, dict) and _number(overlay.get("prev_g")):
            out["overlay"] = {**overlay, "prev_g": overlay["prev_g"] + g - int(frame["g"])}
        return out

    def _connecting(
        self, video: str, session: int, revision: int, since: float, playing: bool, changed_by: str
    ) -> dict[str, Any]:
        """The state of a new session of camera `video` that starts at wall time `since`: connecting, from media time 0
        at rate 1.0, held until it goes live."""
        return {
            "session": session,
            "revision": revision,
            "video": video,
            "playing": playing,
            "rate": 1.0,
            "anchor_media": 0.0,
            "anchor_wall": since,
            "hold_until": since + _LIVE_HOLD,
            "duration": None,
            "changed_by": changed_by,
            "focus": None,
            "media_time": 0.0,
            "live": {"phase": "connecting", "warmup": self._warmup(video), "since": since, "lag": 0.0},
        }

    @staticmethod
    def _camera_phases(base: Mapping[str, Any], at: float, live_t: float) -> list[tuple[float, dict[str, Any]]]:
        """The phases a camera session takes once it connects at wall time `at`, from `base` (its connecting state):
        `warming` `CONNECT_S` later, while the interpreter runs on the run's first `hold` seconds, then `live` when the
        frame of time `live_t` is released, `LIVE_LAG_S` behind it (the playhead starts there). Each bumps `revision`,
        as the unit's clock does. As on the unit, warming changes only the phase: its `lag` is `base`'s (0 after a
        select, the live lag after a play of a camera that was live)."""
        live = base["live"]
        warming = {**base, "revision": int(base["revision"]) + 1, "live": {**live, "phase": "warming"}}
        went_live = float(live["since"]) + live_t + LIVE_LAG_S
        on_air = {
            **base,
            "revision": int(base["revision"]) + 2,
            "anchor_media": live_t,
            "anchor_wall": went_live,
            "hold_until": went_live,
            "live": {**live, "phase": "live", "lag": LIVE_LAG_S},
        }
        phases = [(at + CONNECT_S, warming), (went_live, on_air)]
        for when, state in phases:
            state["media_time"] = _playhead(state, when)
        return phases

    def _offer_camera(self) -> None:
        """Show the live camera from the start, as if it had been selected just long enough ago to be live now: the
        playlist is its session's clock, then its first run's frames after the warm-up."""
        released, _, self._cam_g, self._cam_t = self._camera_run(LIVE_CAMERA, int(self.state["session"]), 0, 0.0)
        self._playlist = [self._clock_entry()]
        self._release(released)

    def _restart_camera(self) -> None:
        """Plan the camera session's next run after a play (the state is connecting again): the camera connects now and
        warms up again on its next frames. Once frames were released, the run is numbered after a break of `_LIVE_GAP`
        frames (`g` jumps, as on the unit) and arrives after the frames already sent: the fake's camera has sent its
        whole run by the time it goes live. The play's clock event comes first."""
        now = self.time.time()
        state = self.state
        g0, t0 = 0, now - float(state["live"]["since"])
        if self._session_frames():
            g0, t0 = self._cam_g + _LIVE_GAP, max(t0, self._cam_t)
        released, live_t, self._cam_g, self._cam_t = self._camera_run(
            str(state["video"]), int(state["session"]), g0, t0
        )
        self._playlist.append(self._clock_entry())
        self._phases, self._held = self._camera_phases(state, now, live_t), released
        self.exhausted = False

    def _advance(self) -> None:
        """Take the camera phases whose wall time has come on the fake's clock, while the session plays (nothing runs
        while it is paused: a play plans the phases again)."""
        while self.state.get("playing") is True and self._phases and self._phases[0][0] <= self.time.time():
            self._take_phase(*self._phases.pop(0))

    def _skip_to_phase(self, until: float) -> bool:
        """How a stream or a long-poll waits for the camera's next phase on the fake's clock, which moves only when
        told: while the session plays, it moves the clock on to the phase, if it comes by wall time `until`, and takes
        it. False when there is none to take."""
        if self.state.get("playing") is not True or not self._phases or self._phases[0][0] > until:
            return False
        self.time.advance(max(0.0, self._phases[0][0] - self.time.time()))
        self._advance()
        return True

    def _take_phase(self, at: float, phase: Mapping[str, Any]) -> None:
        """Take a camera phase after any control made since it was planned (a higher revision; the playing state, the
        focus and what changed it kept): its clock event, sent as of its own time. Going live releases the run's frames
        after the warm-up."""
        cur = self.state
        state = {**phase, "revision": max(int(phase["revision"]), int(cur["revision"]) + 1)}
        state.update(playing=cur["playing"], focus=cur.get("focus"), changed_by=cur["changed_by"])
        state["media_time"] = _playhead(state, at)
        self.state = state
        self._playlist.append(_Entry("clock", {**state, "server_now": at}, applied=True))
        if state["live"]["phase"] == "live":
            self._release(self._held)
            self._held = []

    def _release(self, frames: Sequence[dict[str, Any]]) -> None:
        """Release camera frames: each gets the next event cursor (its raw packet's too) as it joins the playlist."""
        for frame in frames:
            self._add_frame(frame)
            if isinstance(frame.get("raw"), dict):
                frame["raw"]["cursor"] = frame["cursor"]

    def live_phase(self) -> str:
        """The phase of the camera session the fake shows (`connecting`, `warming` or `live`; `""` on a recording),
        after the phases whose time has come."""
        self._advance()
        live = self.state.get("live")
        return str(live["phase"]) if isinstance(live, dict) else ""

    def advance(self, seconds: float) -> None:
        """Move the fake's clock on by `seconds` (as `time.advance`) and take the camera phases whose time has come,
        while the session plays: the shared playhead moves on, and `state` is up to date."""
        self.time.advance(seconds)
        self._advance()

    async def _wait_for_change(self) -> None:
        """Wait until a control changes the shared state: a stream on a paused camera waits so for a play."""
        loop = asyncio.get_running_loop()
        if self._change is None or self._change[0] is not loop:
            self._change = (loop, asyncio.Event())
        await self._change[1].wait()

    def _notify(self) -> None:
        """Wake the streams that wait for a change."""
        if self._change is not None:
            self._change[1].set()
            self._change = None

    # ---- playlists -------------------------------------------------------------------------------------------------
    def _seq(self, frame: Mapping[str, Any]) -> int:
        self._next_seq = max(self._next_seq + 1, int(frame.get("cursor", 0)))
        return self._next_seq

    def _add_frame(self, frame: dict[str, Any]) -> None:
        """Add a frame the fake made to the playlist with the next event cursor, which it carries too."""
        frame["cursor"] = self._seq(frame)
        self._playlist.append(_Entry("frame", frame, frame["cursor"]))

    def _first_playlist(self) -> list[_Entry]:
        out = []
        initial = True  # the first clock line is the state the fake starts in
        for line in self._lines:
            if "g" in line and "cursor" in line:
                out.append(_Entry("frame", line, self._seq(line)))
            elif isinstance(line.get("sse"), str) and isinstance(line.get("data"), dict):
                clock = line["sse"] == "clock"
                out.append(_Entry(line["sse"], line["data"], applied=clock and initial))
                initial = initial and not clock
        return out

    @staticmethod
    def _rewrite(frame: Mapping[str, Any], session: int, video: str, g: int, t: float) -> dict[str, Any]:
        out = {**frame, "session": session, "video": video, "g": g, "t": t}
        if isinstance(out.get("world"), dict):
            out["world"] = {**out["world"], "session": session, "video": video, "g": g, "t": t}
        return out

    def _clock_entry(self) -> _Entry:
        """The clock event of the state now (a control made it: the fake's state already is it)."""
        return _Entry("clock", {**self._served(self.state), "server_now": self.time.time()}, applied=True)

    def _apply(self, entry: _Entry) -> None:
        """Take the state of a recorded clock line, once: a stream started after it, or passed it."""
        if entry.kind == "clock" and not entry.applied:
            entry.applied = True
            state = {k: v for k, v in entry.data.items() if k != "server_now"}
            state.setdefault("media_time", state.get("anchor_media", 0.0))
            self.state = state

    def _start_session(self) -> None:
        """The playlist of the session the state has just started (streams move on to it): its clock event, then a
        recording's frames from its time on. A camera's phases are planned from now, and its first run's frames are
        held until it goes live."""
        state = self.state
        video, session = str(state["video"]), int(state["session"])
        self._playlist, self._phases, self._held = [self._clock_entry()], [], []
        self.exhausted = False
        if isinstance(state.get("live"), dict):
            released, live_t, self._cam_g, self._cam_t = self._camera_run(video, session, 0, 0.0)
            self._phases, self._held = self._camera_phases(state, float(state["anchor_wall"]), live_t), released
            return
        at = float(state["anchor_media"])
        for frame in self._frames:  # a frame without a video is the recording's source's
            if frame.get("video", video) == video and float(frame["t"]) >= at - 1e-9:
                self._add_frame(self._rewrite(frame, session, video, frame["g"], frame["t"]))

    def _session_frames(self) -> list[dict[str, Any]]:
        """The current session's frames by grid frame (the newest revision of each), in g order (a camera's from its
        first release on)."""
        by_g: dict[int, dict[str, Any]] = {}
        for e in self._playlist:
            if e.kind == "frame" and e.data.get("session") == self.state["session"]:
                by_g[int(e.data["g"])] = e.data
        return [by_g[g] for g in sorted(by_g)]

    def _worlds(self) -> list[dict[str, Any]]:
        """The current session's worlds, oldest first: from the recording or the camera, else the recorded `getWorld`
        reply."""
        if self.has_recording:
            return [f["world"] for f in self._session_frames() if isinstance(f.get("world"), dict)]
        world = self._recorded("getWorld")
        return [world] if isinstance(world, dict) else []

    # ---- transport -------------------------------------------------------------------------------------------------
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = await request.aread()
        self._advance()
        op, args, routed = self._route(request)
        reply = routed if routed is not None else self._fault(op)
        if isinstance(reply, BaseException):
            raise reply
        if reply is None and op in _STREAMS and self._streams_from_playlist(op):
            return self._open_stream(op, request)
        if reply is None:
            reply = await self._answer(op, args, request, body)
        return self._response(reply, request)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """The sync transport: serves `files` only (for `Replay` over a URL)."""
        self.requests.append(request)
        _, _, reply = self._route(request)
        return self._response(reply or _error(404, "not_found", "the sync transport serves files only"), request)

    def _route(self, request: httpx.Request) -> tuple[str, dict[str, str], Reply | None]:
        """The operation and path arguments of a request, or the reply when it is a file or matches no route."""
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        if unquote(path) in self.files and request.method == "GET":
            return "file", {}, Reply(body=self.files[unquote(path)], content_type="application/octet-stream")
        m = _API_PATH.fullmatch(path)
        rest = m.group(1) if m is not None else ""
        allowed = False
        for method, pattern, op in _ROUTES:
            pm = pattern.fullmatch(rest)
            if pm is not None:
                allowed = True
                if request.method == method:
                    return op, {k: unquote(v) for k, v in pm.groupdict().items()}, None
        if allowed:
            return "", {}, _error(405, "method_not_allowed", f"{request.method} is not allowed here")
        return "", {}, _error(404, "not_found", f"no route {unquote(path)}")

    def _fault(self, op: str) -> Reply | BaseException | None:
        faults = self._faults.get(op)
        if not faults:
            return None
        fault = faults[0]
        fault.times -= 1
        if fault.times <= 0:
            faults.pop(0)
        return fault.reply

    def _response(self, reply: Reply, request: httpx.Request) -> httpx.Response:
        headers = {"Content-Type": reply.content_type, "X-Interp-Build": self._build(), **reply.headers}
        content = b"" if request.method == "HEAD" else reply.content()
        return httpx.Response(reply.status, headers=headers, content=content)

    def _build(self) -> str:
        return str((self.header or {}).get("build") or "fake")

    def _reply(self, key: str, fallback: Callable[[], Reply] | None = None) -> Reply:
        """The recorded reply `key`, else the fallback, else a 404 that says the fake has none."""
        if key in self.replies:
            return self.replies[key]
        if fallback is not None:
            return fallback()
        return _error(404, "not_found", f"this FakeFactory has no reply {key}")

    # ---- answers ---------------------------------------------------------------------------------------------------
    async def _answer(self, op: str, args: dict[str, str], request: httpx.Request, body: bytes) -> Reply:
        if op in _CONTROLS:
            return self._control(op, request.url.params, body)
        handler: Callable[[dict[str, str], httpx.Request], Awaitable[Reply]] = getattr(self, f"_op_{op}")
        return await handler(args, request)

    async def _op_getApiRoot(self, args: dict[str, str], request: httpx.Request) -> Reply:
        root = {
            "api": "2.2.0",
            "build": self._build(),
            "boot": self.boot,
            "backend": "fake",
            "spec": "/api/v2/openapi.yaml",
            "docs": "/api/docs/",
            "layers": {"raw": "sip1", "overlay": "ovl1", "scene": "scn1", "cards": "crd1", "world": "stw1"},
            "render": {"overlay2d": "r2d1", "scene3d": "r3d1"},
            "limits": {"retained_frames": 512, "reply_frames": 8, "max_reply_frames": 16},
        }
        return self._reply("getApiRoot", lambda: Reply(body=root))

    async def _op_getOpenApi(self, args: dict[str, str], request: httpx.Request) -> Reply:
        return self._reply("getOpenApi")

    async def _op_getModel(self, args: dict[str, str], request: httpx.Request) -> Reply:
        header = self.header or {}
        model = {
            "schema": "mdl1",
            "checkpoint": str(header.get("model") or "fake"),
            "backend": str(header.get("backend") or "fake"),
            "engines": [],
        }
        return self._reply("getModel", lambda: Reply(body=model))

    async def _op_getTopology(self, args: dict[str, str], request: httpx.Request) -> Reply:
        return self._reply("getTopology")

    async def _op_getHealth(self, args: dict[str, str], request: httpx.Request) -> Reply:
        health = {"ok": True, "worker": "running", "viewers": self.viewers}  # the fake's worker never stops
        return self._reply("getHealth", lambda: Reply(body=health))

    async def _op_getReady(self, args: dict[str, str], request: httpx.Request) -> Reply:
        return self._reply("getReady", lambda: Reply(body={"ready": True, "waiting": []}))

    async def _op_getStatus(self, args: dict[str, str], request: httpx.Request) -> Reply:
        status = {
            "build": self._build(),
            "boot": self.boot,
            "uptime_s": 0.0,
            "page": "next",
            "worker": self._worker(),
            "stable": self.stable,
            "hosts": {},
            "viewers": self.viewers,
            "long_polls": 0,
            "streams": self.streams_opened - self.streams_closed,
            "retained": self._retained(),
        }
        return self._reply("getStatus", lambda: Reply(body=status))

    def _worker(self) -> Any:
        """The worker status the fake reports (see `worker`): it does not change with a camera's phases or a pause."""
        if self.worker is not None:
            return self.worker
        return {"state": "running", "reason": "", "session": self.state["session"], "video": self.state["video"]}

    async def _op_getMetrics(self, args: dict[str, str], request: httpx.Request) -> Reply:
        return self._reply("getMetrics")

    async def _op_listVideos(self, args: dict[str, str], request: httpx.Request) -> Reply:
        if not self.has_recording and not self.live and "listVideos" in self.replies:
            return self.replies["listVideos"]
        catalog = {
            "schema": "stone-interpreter-catalog-v1",
            "title": "pDigger Factory (FakeFactory)",
            # as on the unit: the catalog's first recording, whatever the session shows (SDK-DESIGN.md section 3)
            "default_video": next((s["id"] for s in self.catalog if not self._is_camera(s["id"])), self.state["video"]),
            "videos": self.catalog,
        }
        etag = '"' + hashlib.sha256(_dumps(catalog).encode()).hexdigest()[:16] + '"'
        if request.headers.get("If-None-Match") == etag:
            return Reply(status=304, headers={"ETag": etag})
        return Reply(body=catalog, headers={"ETag": etag})

    async def _op_getVideo(self, args: dict[str, str], request: httpx.Request) -> Reply:
        src = self._source(args["id"])
        if src is None:
            return _error(404, "unknown_video", f"Unknown video '{args['id']}'", "id")
        return Reply(body=src)

    async def _op_getVideoMedia(self, args: dict[str, str], request: httpx.Request) -> Reply:
        video = args["id"]
        src = self._source(video)
        if src is None:
            return _error(404, "unknown_video", f"Unknown video '{video}'", "id")
        if src.get("live") is True:
            message = f"{video} is a live camera: it has no proxy video; each frame's picture is "
            return _error(404, "not_found", message + "/api/v2/session/frames/{g}/image.jpg", "id")
        return self._reply("getVideoMedia-range")

    async def _op_getSession(self, args: dict[str, str], request: httpx.Request) -> Reply:
        now = self.time.time()
        return Reply(
            body={
                "server_now": now,
                "server_recv": now,
                "state": self._served(self.state),
                "worker": self._worker(),
                "viewers": self.viewers,
                "stable": self.stable,
            }
        )

    async def _op_getFocus(self, args: dict[str, str], request: httpx.Request) -> Reply:
        focus = self.state.get("focus")
        if focus is None:
            return Reply(body={"focus": None, "resolved": None})
        track = self._track(int(focus["id"]))
        resolved = None if track is None else {"id": focus["id"], "path2d": track[1], "path3d": track[2]}
        return Reply(body={"focus": focus, "resolved": resolved})

    async def _op_getWorld(self, args: dict[str, str], request: httpx.Request) -> Reply:
        worlds = self._worlds()
        if not worlds:  # a camera before it is live too: nothing is released yet
            return _error(503, "world_unavailable", "the Stable sim is not running", retry_ms=1000)
        return Reply(body=worlds[-1])

    async def _op_getWorldEvents(self, args: dict[str, str], request: httpx.Request) -> Reply:
        q = request.url.params
        checked = _ints(q, [("after", -1, -1, 2**62), ("limit", 256, 1, 1024)])
        if isinstance(checked, Reply):
            return checked
        after, limit = checked
        types = [x for x in (q.get("type") or "").split(",") if x]
        unknown = [t for t in types if t not in EVENT_TYPES]
        if unknown:
            return _error(400, "invalid_param", f"unknown event type '{unknown[0]}'", "type")
        if not self.has_recording:
            return self._reply("getWorldEvents")
        frames = self._session_frames()
        events: list[dict[str, Any]] = []
        next_after = after
        for f in frames:
            if f["g"] <= after or not isinstance(f.get("world"), dict):
                continue
            mine = [{"g": f["g"], "t": f["t"], **e} for e in f["world"]["events"] if not types or e["type"] in types]
            if events and len(events) + len(mine) > limit:
                break
            events += mine
            next_after = f["g"]
        body: dict[str, Any] = {"session": self.state["session"], "video": self.state["video"], "events": events}
        body["next_after"] = next_after
        if frames:  # no `oldest_g` before a frame is retained
            body["oldest_g"] = frames[0]["g"]
        return Reply(body=body)

    def _track(self, stone: int) -> tuple[dict[str, Any], list[list[float]], list[list[float]], int, int] | None:
        """The stone's newest view, its 2-D and 3-D paths, and its first and last grid frame, or `None`."""
        views: list[tuple[int, dict[str, Any]]] = [
            (w["g"], s) for w in self._worlds() for s in w.get("stones") or [] if s.get("id") == stone
        ]
        if not views:
            return None
        path2d = [
            [
                round(sum(p[0] for p in s["outline"]) / len(s["outline"]), 2),
                round(sum(p[1] for p in s["outline"]) / len(s["outline"]), 2),
            ]
            for _, s in views
        ]
        path3d = [list(s["solid"]["pos"]) for _, s in views]
        return views[-1][1], path2d[-512:], path3d[-512:], views[0][0], views[-1][0]

    async def _op_getStone(self, args: dict[str, str], request: httpx.Request) -> Reply:
        if not args["id"].isdigit():
            return _error(400, "invalid_param", "id must be an integer", "id")
        stone = int(args["id"])
        recorded = self._recorded("getStone")
        if not self.has_recording and isinstance(recorded, dict) and recorded.get("id") == stone:
            return self.replies["getStone"]
        track = self._track(stone)
        if track is None:
            return _error(404, "unknown_stone", f"no stone {stone} in the world", "id")
        newest = self._worlds()[-1]
        view, path2d, path3d, first, last = track
        return Reply(
            body={
                "schema": "sts1",
                "session": self.state["session"],
                "video": self.state["video"],
                "id": stone,
                "g": newest["g"],
                "t": newest["t"],
                "stone": view,
                "first_g": first,
                "last_g": last,
                "path2d": path2d,
                "path3d": path3d,
            }
        )

    def _retained(self) -> dict[str, Any]:
        frames = self._session_frames()
        ends = [frames[0], frames[-1]] if frames else [{}, {}]
        return {
            "session": self.state["session"],
            "g0": ends[0].get("g"),
            "g1": ends[1].get("g"),
            "t0": ends[0].get("t"),
            "t1": ends[1].get("t"),
            "n": len(frames),
        }

    def _check_session(self, request: httpx.Request) -> Reply | None:
        """The 400 of a `session` parameter that is no integer, or the 410 of one that is not the current session."""
        text = request.url.params.get("session")
        if not text:
            return None
        if _INTEGER.fullmatch(text) is None or not -(2**63) <= int(text) < 2**63:
            return _error(400, "invalid_param", "session must be an integer", "session")
        if int(text) != int(self.state["session"]):
            return _error(410, "session_gone", f"session {int(text)} has ended", "session")
        return None

    def _find(self, g_text: str) -> dict[str, Any] | Reply:
        """Frame `g_text` (an index or `latest`, already checked) of the current session, or the server's 404."""
        frames = self._session_frames()
        if g_text == "latest":
            if frames:
                return frames[-1]
            return _error(404, "not_retained", "no frame is retained yet", "g", retained=self._retained())
        found = [f for f in frames if int(f["g"]) == int(g_text)]
        if not found:
            return _error(404, "not_retained", f"frame {g_text} is not retained", "g", retained=self._retained())
        return found[0]

    def _lookup(self, request: httpx.Request, g_text: str) -> dict[str, Any] | Reply:
        """Frame `g_text` (an index or `latest`) of the current session, or the server's error, checked in its order:
        the session, then `g`."""
        refused = self._check_session(request)
        if refused is not None:
            return refused
        if _G.fullmatch(g_text) is None:
            return _error(400, "invalid_param", "g must be an integer or latest", "g")
        return self._find(g_text)

    def _replied_frame(self, op: str, request: httpx.Request, g_text: str) -> Reply:
        """A frame or packet from the recorded `<op>-latest` reply (neither a recording nor a camera)."""
        latest = self._recorded(f"{op}-latest")
        if not isinstance(latest, dict):
            return self._reply(f"{op}-latest")
        guard = request.url.params.get("session")
        if guard is not None and guard != str(latest.get("session")):
            return self._reply(
                f"{op}-410", lambda: _error(410, "session_gone", f"session {guard} has ended", "session")
            )
        if g_text in ("latest", str(latest.get("g"))):
            return self.replies[f"{op}-latest"]
        return self._reply(f"{op}-404", lambda: _error(404, "not_retained", f"frame {g_text} is not retained", "g"))

    async def _op_getFrame(self, args: dict[str, str], request: httpx.Request) -> Reply:
        layers = _layers_param(request.url.params.get("layers"))
        if isinstance(layers, Reply):
            return layers
        if not self.has_recording:
            return self._replied_frame("getFrame", request, args["g"])
        frame = self._lookup(request, args["g"])
        return frame if isinstance(frame, Reply) else Reply(body=_filter_layers(frame, layers))

    async def _op_getFramePicture(self, args: dict[str, str], request: httpx.Request) -> Reply:
        """Checked in the unit's order: `g`, a stale session (gone whatever it showed), a recording, the frame, its
        picture (only the newest `KEEP_PICTURES` released frames have one)."""
        g_text = args["g"]
        if _G.fullmatch(g_text) is None:
            return _error(400, "invalid_param", "g must be an integer or latest", "g")
        refused = self._check_session(request)
        if refused is not None:
            return refused
        if self.state.get("live") is None:
            return _error(
                404, "not_found", f"{self.state['video']} is a recording: its picture is the proxy video", "g"
            )
        frame = self._find(g_text)
        if isinstance(frame, Reply):
            return frame
        g = int(frame["g"])
        gs = [int(f["g"]) for f in self._session_frames()]
        if len(gs) > KEEP_PICTURES and g < gs[-KEEP_PICTURES]:
            message = f"the picture of frame {g} is no longer kept: only the newest {KEEP_PICTURES} pictures are"
            return _error(404, "not_retained", message, "g")  # as on the unit: without a retained window
        return Reply(
            body=_picture(str(frame["video"]), g),
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Interp-G": str(g)},
        )

    async def _op_rawPacket(self, args: dict[str, str], request: httpx.Request) -> Reply:
        if not self.has_recording:
            return self._replied_frame("rawPacket", request, args["g"])
        frame = self._lookup(request, args["g"])
        if isinstance(frame, Reply):
            return frame
        if not isinstance(frame.get("raw"), dict):
            return _error(404, "not_retained", f"frame {args['g']} is not retained", "g", retained=self._retained())
        return Reply(body=_packet(frame, frame["cursor"]))

    async def _op_rawTensors(self, args: dict[str, str], request: httpx.Request) -> Reply:
        return self._reply("rawTensors", lambda: _error(404, "debug_disabled", "tensor capture is not enabled"))

    async def _op_getFrameRaster(self, args: dict[str, str], request: httpx.Request) -> Reply:
        scale = _int_param(request.url.params.get("scale"), 1, "scale", 1, 8)
        if isinstance(scale, Reply):
            return scale
        if args["raster"] not in ("regions", "mask", "depth"):
            return _error(404, "not_found", f"no raster {args['raster']}", "raster")
        return self._reply(f"getFrameRaster-{args['raster']}")

    def _render(self, op: str, request: httpx.Request) -> Reply:
        q = request.url.params
        if ("g" in q) == ("t" in q):
            return _error(400, "invalid_param", "give exactly one of g and t", "t")
        choices: list[tuple[str, tuple[str, ...]]] = [("mode", ("stable", "raw")), ("draw", ("shapes", "primitives"))]
        if op == "renderScene3d":
            choices.append(("surface", ("vertices", "compact", "none")))
        for name, allowed in choices:
            if q.get(name, allowed[0]) not in allowed:
                return _error(400, "invalid_param", f"{name} must be {', '.join(allowed)}", name)
        surface = q.get("surface", "vertices")
        variant = f"-{surface}" if surface != "vertices" else "-primitives" if q.get("draw") == "primitives" else ""
        return self._reply(op + variant, lambda: self._reply(op))

    async def _op_renderOverlay2d(self, args: dict[str, str], request: httpx.Request) -> Reply:
        return self._render("renderOverlay2d", request)

    async def _op_renderScene3d(self, args: dict[str, str], request: httpx.Request) -> Reply:
        return self._render("renderScene3d", request)

    async def _op_streamFrames(self, args: dict[str, str], request: httpx.Request) -> Reply:
        return self._replied_stream("streamFrames", request)

    async def _op_rawStream(self, args: dict[str, str], request: httpx.Request) -> Reply:
        return self._replied_stream("rawStream", request)

    def _streams_from_playlist(self, op: str) -> bool:
        """Streams come from the recording or the camera, or from recorded replies when there is neither and some
        exist."""
        return self.has_recording or not any(key.startswith(op + "-") for key in self.replies)

    def _replied_stream(self, op: str, request: httpx.Request) -> Reply:
        """A recorded stream reply, as it is; a resume after it (`Last-Event-ID`) has nothing more."""
        if self.on_exhausted == "raise" and request.headers.get("Last-Event-ID"):
            raise FakeExhausted(f"{op} asked for more after the recorded stream reply")
        return self._reply(f"{op}-{self._format(op, request)}")

    @staticmethod
    def _format(op: str, request: httpx.Request) -> str:
        fmt = request.url.params.get("format")
        if fmt is not None:
            return str(fmt)
        return "sse" if "text/event-stream" in request.headers.get("Accept", "") or op == "streamFrames" else "ndjson"

    def _polled_frames(self, cursor: int, limit: int) -> list[_Entry]:
        """The frames a long-poll after `cursor` gets (at most `limit`), with the state in force at them: as a stream
        does, it takes the recording's clock lines before its first frame, and its reply ends before the next line not
        taken yet, which the next poll takes before the frames after it (on the unit, a poll that keeps up gets each
        change before the frames released after it)."""
        playlist = self._playlist
        start = next((i for i, e in enumerate(playlist) if e.kind == "frame" and e.seq > cursor), len(playlist))
        for entry in playlist[:start]:
            self._apply(entry)
        new: list[_Entry] = []
        for entry in playlist[start:]:
            if len(new) == limit or (entry.kind == "clock" and not entry.applied):
                break
            if entry.kind == "frame":
                new.append(entry)
        return new

    async def _op_pollFrames(self, args: dict[str, str], request: httpx.Request) -> Reply:
        q = request.url.params
        checked = _ints(
            q, [("cursor", 0, 0, 2**62), ("rev", 0, 0, 2**62), ("limit", 8, 1, 16), ("wait", 1000, 0, 1000)]
        )
        if isinstance(checked, Reply):
            return checked
        cursor, rev, limit, wait = checked
        cid = q.get("cid")
        if cid is not None and _CID.fullmatch(cid) is None:
            return _error(400, "invalid_param", "cid must be 8 to 40 of A-Z a-z 0-9 _ -", "cid")
        if cid is not None:
            self.cids.add(cid)
        layers = _layers_param(q.get("layers"))
        if isinstance(layers, Reply):
            return layers
        if not self.has_recording:
            recorded = self._recorded("pollFrames-world")
            exhausted = isinstance(recorded, dict) and cursor >= int(recorded.get("cursor", 0))
            if exhausted and self.on_exhausted == "raise":
                raise FakeExhausted("the long-poll asked for frames after the recorded reply")
            return self._reply("pollFrames-world")
        asked = self.time.time()
        while self._skip_to_phase(asked + wait / 1000.0):  # a camera's phases within the wait: the clock moves to each
            pass
        new = self._polled_frames(cursor, limit)  # the recording's clock lines it passes are taken
        if not new and rev == self.state["revision"]:
            if self.on_exhausted == "raise" and not self._phases:
                raise FakeExhausted("the long-poll asked for frames after the end of the recording")
            await self.sleep(max(0.0, wait / 1000.0 - (self.time.time() - asked)))  # nothing new: the rest of its wait
            self._advance()
        now = self.time.time()
        return Reply(
            body={
                "server_now": now,
                "server_recv": now,
                "state": self._served(self.state),
                "worker": self._worker(),
                "viewers": self.viewers,
                "stable": self.stable,
                "cursor": new[-1].seq if new else cursor,
                "frames": [_filter_layers(e.data, layers) for e in new],
            }
        )

    # ---- controls --------------------------------------------------------------------------------------------------
    def _control(self, op: str, params: httpx.QueryParams, raw: bytes) -> Reply:
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, RecursionError):
            return _error(400, "invalid_json", "the body is not JSON")
        if not isinstance(body, dict):
            return _error(400, "not_an_object", "the body must be a JSON object")
        flag = params.get("dry_run")
        if flag not in (None, "", "0", "1"):
            return _error(400, "invalid_param", "dry_run must be 0 or 1", "dry_run")
        dry = flag == "1"
        state = dict(self.state)
        apply: Callable[[Mapping[str, Any], dict[str, Any]], Reply | None] = {
            "sessionSelect": self._select,
            "sessionSeek": self._seek,
            "sessionPlay": self._play,
            "sessionPause": self._pause,
            "putFocus": self._focus,
            "deleteFocus": self._unfocus,
        }[op]
        refused = apply(body, state)
        if refused is not None:
            return refused
        if not dry and state["revision"] != self.state["revision"]:  # a dry run, or a repeat, moves nothing
            new_session = state["session"] != self.state["session"]
            self.state = state
            if new_session:
                self._start_session()
            elif op == "sessionPlay" and isinstance(state.get("live"), dict):
                self._restart_camera()  # it connects again: a new warm-up
            else:
                self._playlist.append(self._clock_entry())
            self._notify()
        now = self.time.time()
        return Reply(body={"state": self._served(state), "server_now": now, "server_recv": now, "dry_run": dry})

    def _video_of(self, body: Mapping[str, Any]) -> dict[str, Any] | Reply:
        """The source a control's `video` names, or the unit's 400 (missing, not a string, null too, or unknown)."""
        if "video" not in body:
            return _error(400, "missing_field", "Unknown video None", "video")
        video = body["video"]
        if not isinstance(video, str):
            return _error(400, "invalid_type", f"Unknown video {video!r}", "video")  # as the unit: 5, True, None
        src = self._source(video)
        if src is None:
            return _error(400, "unknown_video", f"Unknown video '{video}'", "video")
        return src

    @staticmethod
    def _time_of(body: Mapping[str, Any], src: Mapping[str, Any]) -> float | Reply | None:
        """A control's media `time` in source `src`, `None` when it gives none, or the unit's 400 (null is a time of
        the wrong type)."""
        if "time" not in body:
            return None
        value = body["time"]
        if not _number(value):
            return _error(400, "invalid_type", _time_range(src), "time")
        duration = src.get("duration")
        if value < 0 or (_number(duration) and value > duration):
            return _error(400, "time_out_of_range", _time_range(src), "time")
        return float(value)

    def _select(self, body: Mapping[str, Any], state: dict[str, Any]) -> Reply | None:
        """Checked in the unit's order: the video, a camera (any time, null too), then the time."""
        src = self._video_of(body)
        if isinstance(src, Reply):
            return src
        video = str(src["id"])
        if "time" in body and self._is_camera(video):
            return _error(400, "not_seekable", f"{video} is a live camera: leave time out", "time")
        at = self._time_of(body, src)
        if isinstance(at, Reply):
            return at
        start = src.get("default_start")
        self._restart(state, video, (float(start) if _number(start) else 0.0) if at is None else at)
        return None

    def _seek(self, body: Mapping[str, Any], state: dict[str, Any]) -> Reply | None:
        """Checked in the unit's order: its video (the one it names, else the one shown), a camera (409: a seek in a
        recording is applied even while the session shows a camera, and leaves it), then the time."""
        src = self._video_of(body) if "video" in body else self._source(str(state["video"]))
        if isinstance(src, Reply):
            return src
        if src is None:
            return _error(409, "not_seekable", "the session shows no video", "video")
        video = str(src["id"])
        if self._is_camera(video):
            return _error(409, "not_seekable", f"{video} is a live camera and cannot seek", "video")
        at = self._time_of(body, src)
        if isinstance(at, Reply):
            return at
        if at is None:
            return _error(400, "missing_field", _time_range(src), "time")
        self._restart(state, video, at)
        return None

    def _restart(self, state: dict[str, Any], video: str, at: float) -> None:
        """A new session on `video` from `at`: its id is one more than the newest the fake has used."""
        sessions = {int(f.get("session") or 0) for f in self._frames} | {int(state["session"])}
        fresh = self._new_state(video, at, max(sessions) + 1, int(state["revision"]) + 1, bool(state["playing"]))
        state.clear()
        state.update(fresh)

    def _set_playing(self, state: dict[str, Any], playing: bool) -> None:
        """Pause or play (a repeat changes nothing). A play of a camera connects again, in its own state: on a camera
        that was live it goes on at the live edge (`now - since - lag`), else where it was."""
        if state["playing"] != playing:
            now = self.time.time()
            at = _playhead(state, now)  # where the playhead is: a pause holds it there, a play goes on from it
            live = state.get("live")
            if playing and isinstance(live, dict):
                if live.get("phase") == "live":
                    at = max(0.0, now - float(live["since"]) - float(live["lag"]))  # a live camera: at the live edge
                state["live"] = {**live, "phase": "connecting"}  # its next frames start a new warm-up
            state.update(playing=playing, revision=state["revision"] + 1, changed_by="viewer")
            state.update(anchor_media=at, anchor_wall=now, media_time=at)

    def _play(self, body: Mapping[str, Any], state: dict[str, Any]) -> Reply | None:
        self._set_playing(state, True)
        return None

    def _pause(self, body: Mapping[str, Any], state: dict[str, Any]) -> Reply | None:
        self._set_playing(state, False)
        return None

    def _focus(self, body: Mapping[str, Any], state: dict[str, Any]) -> Reply | None:
        stone = body.get("stone_id")
        if stone is None:
            return _error(400, "missing_field", "stone_id is required", "stone_id")
        if isinstance(stone, bool) or not isinstance(stone, int):
            return _error(400, "invalid_type", "stone_id must be an integer", "stone_id")
        if body.get("session") is not None and body["session"] != state["session"]:
            return _error(409, "stale_session", "that stone belongs to an earlier session", "session")
        if not self._worlds():  # a camera before it is live: no world yet
            return _error(503, "world_unavailable", "the Stable sim is not running", retry_ms=1000)
        track = self._track(stone)
        if track is None:
            return _error(404, "unknown_stone", f"no stone {stone} in the world", "stone_id")
        view = track[0]
        newest = self._worlds()[-1]
        solid = view["solid"]
        q = solid["q"]
        state.update(revision=state["revision"] + 1, changed_by="viewer")
        state["focus"] = {
            "n": state["revision"],
            "session": state["session"],
            "video": state["video"],
            "id": stone,
            "g": int(body.get("g") or newest["g"]),
            "t": newest["t"],
            "x": solid["pos"][0],
            "y": solid["pos"][1],
            "z": solid["pos"][2],
            "L": solid["dims"][0],
            "W": solid["dims"][1],
            "yaw": round(2 * math.atan2(q[2], q[3]), 4),
        }
        return None

    def _unfocus(self, body: Mapping[str, Any], state: dict[str, Any]) -> Reply | None:
        if state.get("focus") is not None:
            state.update(focus=None, revision=state["revision"] + 1, changed_by="viewer")
        return None

    # ---- streams ---------------------------------------------------------------------------------------------------
    def _start_index(self, request: httpx.Request) -> tuple[int, dict[str, Any] | None] | Reply:
        """Where a stream starts in the playlist, and the `reset` to send first (a `Last-Event-ID` of another boot)."""
        last_id = request.headers.get("Last-Event-ID")
        if last_id:
            boot, _, cursor = last_id.rpartition("-")
            if boot != self.boot or not cursor.isdigit():
                newest = max((e.seq for e in self._playlist if e.kind == "frame"), default=0)
                return 0, {"boot": self.boot, "cursor": newest}
            after: int | None = int(cursor)
        else:
            checked = _int_param(request.url.params.get("after"), -1, "after", 0, 2**62)
            if isinstance(checked, Reply):
                return checked
            after = None if checked < 0 else checked
        if after is None:
            return 0, None
        start = next((i for i, e in enumerate(self._playlist) if e.kind == "frame" and e.seq > after), None)
        if start is None:
            return len(self._playlist), None
        while start > 0 and self._playlist[start - 1].kind != "frame":
            start -= 1  # the side events since the last frame delivered come again
        return start, None

    def _last_sent(self, op: str, playlist: list[_Entry]) -> int:
        """The index of the last frame of `playlist` that a stream of `op` sends (-1 when there is none)."""
        return next((j for j in range(len(playlist) - 1, -1, -1) if self._sends(op, playlist[j])), -1)

    @staticmethod
    def _sends(op: str, entry: _Entry) -> bool:
        """A frame that a stream of `op` sends (a raw stream sends only frames with a packet)."""
        return entry.kind == "frame" and (op != "rawStream" or isinstance(entry.data.get("raw"), dict))

    def _frames_left(self, op: str, index: int) -> bool:
        return any(self._sends(op, e) for e in self._playlist[index:])

    def _open_stream(self, op: str, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        fmt = self._format(op, request)
        layers = _layers_param(q.get("layers"))
        if isinstance(layers, Reply):
            return self._response(layers, request)
        limits = _ints(q, [("limit", 0, 1, 4096), ("max_s", 0, 1, 3600)])
        if isinstance(limits, Reply):
            return self._response(limits, request)
        start = self._start_index(request)
        if isinstance(start, Reply):
            return self._response(start, request)
        index, reset = start
        ended = self.exhausted and reset is None and not self._phases  # and no camera run still to come
        if self.on_exhausted == "raise" and ended and not self._frames_left(op, index):
            raise FakeExhausted(f"{op} asked for more after the end of the recording")
        first = next((j for j in range(index, len(self._playlist)) if self._sends(op, self._playlist[j])), None)
        known = len(self._playlist) if first is None else first
        for entry in self._playlist[:known]:
            self._apply(entry)  # what happened before this connection: its state, not events on it
        drops = self._drops.get(op)
        drop = drops.pop(0) if drops else None
        injections = [i for i in self._injections.get(op, []) if i.times > 0]
        for injection in injections:
            injection.times -= 1
        limit, max_s = limits
        events = self._events(op, fmt, layers, index, known, reset, limit, max_s, injections)
        stream = _FakeStream(self, op, events, drop, q.get("observe") == "1")
        content_type = "text/event-stream" if fmt == "sse" else "application/x-ndjson"
        return httpx.Response(
            200, headers={"Content-Type": content_type, "X-Interp-Build": self._build()}, stream=stream
        )

    def _events(
        self,
        op: str,
        fmt: str,
        layers: list[str],
        index: int,
        known: int,
        reset: dict[str, Any] | None,
        limit: int,
        max_s: int,
        injections: list[_Injection],
    ) -> Iterator[tuple[bytes | None, float | None]]:
        """The stream's chunks, each with the media time of its frame (for pacing); `limit` and `max_s` of 0 mean none.

        The clock lines before playlist index `known` were the state the stream started in. A select or seek during
        the stream switches it to the new session's playlist, as the server does. At the end of the playlist it waits
        for the camera's next phase (the fake's clock moves on to it), or, while the camera is paused, for a control:
        `_WAIT`. A chunk of `None` is the `linger` of a stream that reached its `limit` or `max_s`.
        """
        sent = frames = 0
        elapsed = 0.0
        last_t: float | None = None
        pending = list(injections)

        def side(event: str, data: Mapping[str, Any]) -> Iterator[tuple[bytes, float | None]]:
            nonlocal sent
            if fmt == "sse":
                sent += 1
                yield f"event: {event}\ndata: {_dumps(data)}\n\n".encode(), None
            elif event == "gap":
                sent += 1
                yield f"{_dumps(data)}\n".encode(), None

        def due() -> Iterator[tuple[bytes, float | None]]:
            for injection in [i for i in pending if i.after <= sent]:
                pending.remove(injection)
                yield from side(injection.event, injection.data)

        if reset is not None:
            yield from side("reset", reset)
        playlist, i = self._playlist, index
        seen, last = len(playlist), self._last_sent(op, playlist)
        while True:
            yield from due()
            if playlist is not self._playlist:  # a select or seek: the new session, its clock event first
                playlist, i, last_t, known, seen = self._playlist, 0, None, 0, -1
            if len(playlist) != seen:  # a new playlist, or a camera's phases and frames came
                seen, last = len(playlist), self._last_sent(op, playlist)
            if i >= len(playlist):
                if self._skip_to_phase(math.inf):
                    continue  # the camera's next phase: the fake's clock moved on to it
                if self._phases:
                    yield _WAIT  # a paused camera: nothing comes before a play
                    continue
                self.exhausted = True
                break
            entry = playlist[i]
            i += 1
            if entry.kind != "frame":
                if entry.kind == "clock":
                    if i <= known:
                        continue  # taken as the stream's state when it opened: the unit sends no clock on connect
                    self._apply(entry)
                yield from side(entry.kind, entry.data)
                continue
            if not self._sends(op, entry):
                continue
            t = float(entry.data["t"])
            elapsed += t - last_t if last_t is not None and t > last_t else 0.0
            last_t = t
            if (limit and frames >= limit) or (max_s and elapsed >= max_s):
                if self.linger:
                    yield None, None  # the unit sends nothing more, and closes the stream only later
                return
            data = _packet(entry.data, entry.seq) if op == "rawStream" else _filter_layers(entry.data, layers)
            text = _dumps(data)
            frames += 1
            sent += 1
            if i - 1 == last:
                self.exhausted = True  # the end of the recording is served
            yield (
                (
                    f"id: {self.boot}-{entry.seq}\nevent: frame\ndata: {text}\n\n" if fmt == "sse" else f"{text}\n"
                ).encode(),
                t,
            )
        for injection in pending:
            yield from side(injection.event, injection.data)


class _FakeStream(httpx.AsyncByteStream):
    """A streamed reply of the fake: applies the drop, delay and pacing, and counts open and closed streams."""

    def __init__(
        self,
        fake: FakeFactory,
        op: str,
        events: Iterator[tuple[bytes | None, float | None]],
        drop: int | None,
        observe: bool,
    ) -> None:
        self._fake = fake
        self._op = op
        self._events = events
        self._drop = drop
        self._observe = observe
        self._closed = False
        fake.streams_opened += 1
        fake.viewers += 0 if observe else 1

    async def __aiter__(self) -> AsyncIterator[bytes]:
        fake = self._fake
        last_t: float | None = None
        n = 0
        for chunk, t in self._events:
            if chunk is None and t is not None:  # `_WAIT`: a paused camera, until a control changes the state
                await fake._wait_for_change()
                continue
            if self._drop is not None and n >= self._drop:
                raise httpx.ReadError("connection dropped by FakeFactory")
            if fake.delay is not None:
                await fake.delay(self._op, n)
            n += 1
            if chunk is None:
                await fake.sleep(fake.linger)
                continue
            if fake.realtime and t is not None:
                if last_t is not None and t > last_t:
                    await fake.sleep(t - last_t)
                last_t = t
            yield chunk

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._fake.streams_closed += 1
            self._fake.viewers -= 0 if self._observe else 1

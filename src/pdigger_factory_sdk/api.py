"""Low level: one async method per HTTP operation of the API (SDK-DESIGN.md section 2.2).

Method names are the spec's `operationId` in snake case. Every method returns a typed model (see `models`) or bytes,
and raises `FactoryError` for an error reply. Reach it as `factory.api`:

    async with FactoryClient("http://factory-orin.local:8120") as factory:
        world = await factory.api.get_world()
        print(len(world.stones), "stones at g", world.g)
"""

from __future__ import annotations

import codecs
import contextlib
import re
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import httpx

from ._http import HttpCore, decode_json, http_error
from ._sse import LineSplitter, SseParser
from ._stream import AsyncStream
from .errors import FactoryError
from .models import (
    ApiRoot,
    ControlReply,
    EventLog,
    FocusReply,
    Frame,
    Health,
    LiveReply,
    ModelInfo,
    Overlay2D,
    Ready,
    Scene3D,
    SessionReply,
    Sip1Packet,
    Source,
    Status,
    StoneTrack,
    Topology,
    VideoList,
    WorldLayer,
)

__all__ = [
    "LIVE",
    "Api",
    "EventStream",
    "Layers",
    "Media",
    "MediaStream",
    "Metric",
    "Metrics",
    "Picture",
    "StreamMessage",
    "join_layers",
    "parse_metrics",
]

LIVE = "live"
"""The id the SDK's design gave the one live camera before cameras shipped. A unit lists its cameras by their serials
(`FactoryClient.cameras()`, `Source.is_live`); the SDK still treats this id as a camera."""

Layers = str | Sequence[str] | None
"""Layers to ask for: a comma-separated string or a sequence of `raw`, `overlay`, `scene`, `cards`, `world`."""

GridFrame = int | Literal["latest"]
"""A grid frame index, or `"latest"`."""

_GAP_LINE = re.compile(r'^\{\s*"gap"\s*:')
_CONTENT_RANGE = re.compile(r"^\s*bytes\s+(?:(\d+)-(\d+)|\*)/(\d+|\*)\s*$")


def join_layers(layers: Layers) -> str | None:
    """`("world", "cards")` -> `"world,cards"`; a string is passed as it is; `None` means the server's default."""
    if layers is None or isinstance(layers, str):
        return layers
    return ",".join(layers)


def _flag(value: bool | None) -> int | None:
    return 1 if value else None


# ---- replies that are not plain models ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class Picture:
    """The camera picture a live frame was interpreted from (`GET /session/frames/{g}/image.jpg`)."""

    g: int
    """The frame's grid index (the reply's `X-Interp-G`: the frame `latest` was)."""
    jpeg: bytes
    """The JPEG (640 x 360 on the plant rigs; the layers' native pixels are this picture stretched to `native_wh`)."""


@dataclass(slots=True, frozen=True)
class Media:
    """A reply of `GET` or `HEAD /videos/{id}/media`."""

    status: int
    """200 (the whole file), 206 (a range) or 304 (unchanged since `if_none_match`)."""
    data: bytes
    """The bytes (empty for HEAD and 304)."""
    content_type: str | None = None
    content_range: str | None = None
    """The `Content-Range` header of a 206, for example `bytes 0-1023/184866573`."""
    etag: str | None = None
    accept_ranges: str | None = None
    content_length: int | None = None

    @property
    def not_modified(self) -> bool:
        """True for a 304."""
        return self.status == 304

    @property
    def range(self) -> tuple[int, int] | None:
        """`(first, last)` byte of a 206 (inclusive), from `Content-Range`."""
        m = _CONTENT_RANGE.match(self.content_range or "")
        if m is None or m.group(1) is None:
            return None
        return int(m.group(1)), int(m.group(2))

    @property
    def total(self) -> int | None:
        """The size of the whole file, from `Content-Range` (or `Content-Length` of a 200)."""
        m = _CONTENT_RANGE.match(self.content_range or "")
        if m is not None and m.group(3) != "*":
            return int(m.group(3))
        return self.content_length if self.status == 200 else None


@dataclass(slots=True, frozen=True)
class Metric:
    """One Prometheus sample."""

    name: str
    labels: dict[str, str]
    value: float


@dataclass(slots=True, frozen=True)
class Metrics:
    """`GET /metrics`: the Prometheus text and its parsed samples."""

    text: str
    samples: list[Metric] = field(default_factory=list)

    def find(self, name: str, **labels: str) -> list[Metric]:
        """The samples of `name` whose labels include `labels`."""
        return [m for m in self.samples if m.name == name and all(m.labels.get(k) == v for k, v in labels.items())]

    def value(self, name: str, **labels: str) -> float | None:
        """The value of the first sample of `name` with `labels`, or `None`."""
        found = self.find(name, **labels)
        return found[0].value if found else None


_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)(\s+\S+)?\s*$")
_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*,?')
_UNESCAPE = {"\\\\": "\\", '\\"': '"', "\\n": "\n"}


def parse_metrics(text: str) -> list[Metric]:
    """Parse the Prometheus text exposition format (comments and lines that do not parse are skipped)."""
    out: list[Metric] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if m is None:
            continue
        try:
            value = float(m.group(4))
        except ValueError:
            continue
        labels = {
            k: re.sub(r"\\[\\\"n]", lambda x: _UNESCAPE[x.group(0)], v) for k, v in _LABEL.findall(m.group(3) or "")
        }
        out.append(Metric(name=m.group(1), labels=labels, value=value))
    return out


@dataclass(slots=True, frozen=True)
class StreamMessage:
    """One message of a low-level stream: an SSE event, or an NDJSON line."""

    event: str
    """`frame`, `clock`, `worker`, `gap` or `reset` (NDJSON lines are `frame`, or `gap` for a `{"gap": n}` line)."""
    data: str
    """The event's data: JSON text."""
    id: str | None = None
    """The SSE event id, `<boot>-<cursor>` (NDJSON lines have none)."""

    def json(self) -> Any:
        """The data, decoded."""
        return decode_json(self.data.encode("utf-8"), f"{self.event} event")


class EventStream(AsyncStream[StreamMessage]):
    """An open low-level stream (`stream_frames`, `raw_stream`): iterate it for `StreamMessage`s.

    Closing it (`aclose()`, `async with`, or cancelling the task) closes the connection. A broken connection raises
    `FactoryError` with code `transport`, a body that does not decode (a bad `Content-Encoding`) code `decode`. The
    high level (`factory.streams`) adds resume, backfill and typed items.
    """

    __slots__ = ("response",)

    def __init__(self, response: httpx.Response, fmt: str) -> None:
        self.response = response
        """The HTTP reply (status and headers)."""
        super().__init__(_messages(response, fmt))

    async def aclose(self) -> None:
        """Stop the stream and close its connection (also when it was never iterated)."""
        try:
            await super().aclose()
        finally:
            await self.response.aclose()


class MediaStream(AsyncStream[bytes]):
    """An open download of a source's media (`Api.open_video_media`): iterate it for the body, in chunks as they arrive.

    `media` has the reply's status and headers. Media without an end (a live stream) never ends: read what you need,
    then close the stream (leave the `async with` block, `aclose()`, or cancel the task), which closes the connection.
    A connection that breaks raises `FactoryError` with code `transport`.
    """

    __slots__ = ("media", "response")

    def __init__(self, response: httpx.Response, chunk_size: int | None = None) -> None:
        self.response = response
        """The HTTP reply."""
        self.media = _media(response, b"")
        """The reply's status and headers (its `data` is empty: the body is what the stream gives)."""
        super().__init__(_chunks(response, chunk_size))

    async def aclose(self) -> None:
        """Stop reading and close the connection (also when the body was never read)."""
        try:
            await super().aclose()
        finally:
            await self.response.aclose()


async def _chunks(response: httpx.Response, chunk_size: int | None) -> AsyncGenerator[bytes, None]:
    try:
        async for chunk in response.aiter_bytes(chunk_size):
            yield chunk
    except httpx.HTTPError as e:  # a broken connection (`transport`), or a body that does not decode (`decode`)
        raise http_error("the media download broke", e) from e
    finally:
        await response.aclose()


async def _messages(response: httpx.Response, fmt: str) -> AsyncGenerator[StreamMessage, None]:
    try:
        if fmt == "sse":
            parser = SseParser()
            async for chunk in response.aiter_bytes():
                for ev in parser.feed(chunk):
                    yield StreamMessage(event=ev.event, data=ev.data, id=ev.id)
            parser.close()
        else:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            lines = LineSplitter()
            async for chunk in response.aiter_bytes():
                for line in lines.push(decoder.decode(chunk)):
                    text = line.strip()
                    if text:
                        yield StreamMessage(event="gap" if _GAP_LINE.match(text) else "frame", data=text)
    except httpx.HTTPError as e:  # a broken connection (`transport`), or a body that does not decode (`decode`)
        raise http_error("the connection broke", e) from e
    finally:
        await response.aclose()


def _id(value: str) -> str:
    return quote(value, safe="")


class Api:
    """One method per operation of `spec/openapi.yaml`. Every call may raise `FactoryError`."""

    def __init__(self, http: HttpCore) -> None:
        self._http = http

    def with_options(self, *, retries: int | None = None, timeout: float | None = None) -> Api:
        """The same API with other retries or request timeout (seconds) for the calls made through it."""
        return Api(self._http.with_options(retries=retries, timeout=timeout))

    @property
    def is_closed(self) -> bool:
        """Whether the client's connection pool is closed (calls then raise `stream_closed`)."""
        return self._http.is_closed

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self._http.get_json(path, params)

    async def _control(self, method: str, path: str, dry_run: bool, body: Any) -> ControlReply:
        data = await self._http.send_json(method, path, params={"dry_run": _flag(dry_run)}, body=body)
        return ControlReply.from_json(data)

    async def _bytes(self, path: str, params: Mapping[str, Any] | None, accept: str) -> bytes:
        response = await self._http.send("GET", path, params=params, headers={"Accept": accept})
        return response.content

    # ---- meta and ops ----------------------------------------------------------------------------------------------
    async def get_api_root(self) -> ApiRoot:
        """`GET /api/v2`: links, layer schema ids, limits and the build."""
        return ApiRoot.from_json(await self._get("/api/v2"))

    async def get_openapi(self) -> str:
        """`GET /api/v2/openapi.yaml`: the server's own OpenAPI document (YAML text)."""
        return (await self._bytes("/api/v2/openapi.yaml", None, "application/yaml")).decode("utf-8")

    async def get_model(self) -> ModelInfo:
        """`GET /api/v2/model`: the model that is running, with engine hashes."""
        return ModelInfo.from_json(await self._get("/api/v2/model"))

    async def get_topology(self) -> Topology:
        """`GET /api/v2/topology`: skeleton topology, class names, palette, grid."""
        return Topology.from_json(await self._get("/api/v2/topology"))

    async def get_health(self) -> Health:
        """`GET /api/v2/health`: liveness only."""
        return Health.from_json(await self._get("/api/v2/health"))

    async def get_ready(self) -> Ready:
        """`GET /api/v2/ready`: readiness. Until ready the server answers 503 (`FactoryError.waiting` says why)."""
        return Ready.from_json(await self._get("/api/v2/ready"))

    async def get_status(self) -> Status:
        """`GET /api/v2/status`: everything an operator wants on one page."""
        return Status.from_json(await self._get("/api/v2/status"))

    async def get_metrics(self) -> Metrics:
        """`GET /metrics`: the Prometheus text, with its samples parsed."""
        text = (await self._bytes("/metrics", None, "text/plain")).decode("utf-8", "replace")
        return Metrics(text=text, samples=parse_metrics(text))

    # ---- sources ---------------------------------------------------------------------------------------------------
    async def list_videos(self, *, if_none_match: str | None = None) -> VideoList | None:
        """`GET /api/v2/videos`: the catalog, with its `etag`. With `if_none_match=catalog.etag`, `None` means unchanged
        (304)."""
        headers = {"Accept": "application/json"}
        if if_none_match is not None:
            headers["If-None-Match"] = if_none_match
        response = await self._http.send("GET", "/api/v2/videos", headers=headers)
        if response.status_code == 304:
            return None
        catalog = VideoList.from_json(decode_json(response.content, "/api/v2/videos"))
        catalog.etag = response.headers.get("ETag")
        return catalog

    async def get_video(self, id: str) -> Source:
        """`GET /api/v2/videos/{id}`: one source (a recording or a live camera)."""
        return Source.from_json(await self._get(f"/api/v2/videos/{_id(id)}"))

    async def get_video_media(
        self,
        id: str,
        *,
        range: tuple[int, int | None] | str | None = None,
        if_range: str | None = None,
        if_none_match: str | None = None,
    ) -> Media:
        """`GET /api/v2/videos/{id}/media`: the proxy video, or one byte range of it, read into memory.

        `range=(0, 1023)` asks for the first KiB (inclusive bounds), `(n, None)` from byte `n`, or give the header
        value itself (`"bytes=-500"`). A file is read whole, so for a large one ask for ranges or use
        `open_video_media`. A live camera has no media (404 `not_found`): its pictures come with its frames
        (`get_frame_picture`). A reply that has neither a length nor byte ranges would never end: it raises
        `not_seekable` (field `id`), closing the connection; read such media with `open_video_media`.
        """
        response = await self._http.send(
            "GET",
            f"/api/v2/videos/{_id(id)}/media",
            headers=_media_headers(range, if_range, if_none_match),
            check=lambda reply: _refuse_endless(id, reply),
        )
        return _media(response, response.content)

    @contextlib.asynccontextmanager
    async def open_video_media(
        self,
        id: str,
        *,
        range: tuple[int, int | None] | str | None = None,
        if_range: str | None = None,
        if_none_match: str | None = None,
        chunk_size: int | None = None,
    ) -> AsyncIterator[MediaStream]:
        """`GET /api/v2/videos/{id}/media` as a stream: a large file, or media without an end, read as it arrives. An
        async context manager:

            async with factory.api.open_video_media("dry1") as stream:
                print(stream.media.content_type)
                async for chunk in stream:
                    ...

        The `MediaStream` gives the reply's status and headers (`stream.media`) and the body in chunks (of
        `chunk_size` bytes, or as they arrive). Leaving the block closes the connection. `range`, `if_range` and
        `if_none_match` are those of `get_video_media`. A live camera has no media (404 `not_found`).
        """
        response = await self._http.send(
            "GET",
            f"/api/v2/videos/{_id(id)}/media",
            headers=_media_headers(range, if_range, if_none_match),
            stream=True,
        )
        stream = MediaStream(response, chunk_size)
        try:
            yield stream
        finally:
            await stream.aclose()

    async def head_video_media(self, id: str, *, if_none_match: str | None = None) -> Media:
        """`HEAD /api/v2/videos/{id}/media`: the headers of the proxy video, no body."""
        headers = {} if if_none_match is None else {"If-None-Match": if_none_match}
        response = await self._http.send("HEAD", f"/api/v2/videos/{_id(id)}/media", headers=headers)
        return _media(response, b"")

    # ---- the shared session ----------------------------------------------------------------------------------------
    async def get_session(self) -> SessionReply:
        """`GET /api/v2/session`: the shared clock, the worker and the Stable sim."""
        return SessionReply.from_json(await self._get("/api/v2/session"))

    async def session_select(self, video: str, time: float | None = None, *, dry_run: bool = False) -> ControlReply:
        """`POST /session/select`: switch the shared session to another source, for every viewer.

        Leave `time` out for a live camera (400 `not_seekable`, field `time`): its session starts `connecting`, warms
        up on its first `hold` seconds of frames, then goes `live` (`State.live`). `dry_run` validates without moving
        it.
        """
        body: dict[str, Any] = {"video": video}
        if time is not None:
            body["time"] = time
        return await self._control("POST", "/api/v2/session/select", dry_run, body)

    async def session_seek(self, time: float, *, video: str | None = None, dry_run: bool = False) -> ControlReply:
        """`POST /session/seek`: move the shared playhead for every viewer (a new session). A live camera: 409
        `not_seekable` (field `video`)."""
        body: dict[str, Any] = {"time": time}
        if video is not None:
            body["video"] = video
        return await self._control("POST", "/api/v2/session/seek", dry_run, body)

    async def session_play(self, *, dry_run: bool = False) -> ControlReply:
        """`POST /session/play`: resume the shared playhead for every viewer. Idempotent."""
        return await self._control("POST", "/api/v2/session/play", dry_run, {})

    async def session_pause(self, *, dry_run: bool = False) -> ControlReply:
        """`POST /session/pause`: pause the shared playhead for every viewer. Idempotent."""
        return await self._control("POST", "/api/v2/session/pause", dry_run, {})

    async def get_focus(self) -> FocusReply:
        """`GET /session/focus`: the stone highlighted for every viewer, and its path (nulls when none)."""
        return FocusReply.from_json(await self._get("/api/v2/session/focus"))

    async def put_focus(
        self, stone_id: int, *, g: int | None = None, session: int | None = None, dry_run: bool = False
    ) -> ControlReply:
        """`PUT /session/focus`: highlight a stone of the world for every viewer.

        `g` is the frame the stone was picked on; with `session`, a focus made for another session is refused (409).
        """
        body: dict[str, Any] = {"stone_id": stone_id}
        if g is not None:
            body["g"] = g
        if session is not None:
            body["session"] = session
        return await self._control("PUT", "/api/v2/session/focus", dry_run, body)

    async def delete_focus(self, *, dry_run: bool = False) -> ControlReply:
        """`DELETE /session/focus`: clear the highlight for every viewer."""
        data = await self._http.send_json(
            "DELETE", "/api/v2/session/focus", params={"dry_run": _flag(dry_run)}, body=None
        )
        return ControlReply.from_json(data)

    # ---- the world -------------------------------------------------------------------------------------------------
    async def get_world(self, *, precision: str | None = None) -> WorldLayer:
        """`GET /session/world`: the newest world, at its newest revision (503 `world_unavailable` before one)."""
        return WorldLayer.from_json(await self._get("/api/v2/session/world", {"precision": precision}))

    async def get_world_events(
        self, *, after: int | None = None, limit: int | None = None, types: str | Sequence[str] | None = None
    ) -> EventLog:
        """`GET /session/world/events`: the event log, oldest first; continue with `after=log.next_after`."""
        params = {"after": after, "limit": limit, "type": join_layers(types)}
        return EventLog.from_json(await self._get("/api/v2/session/world/events", params))

    async def get_stone(self, id: int) -> StoneTrack:
        """`GET /session/world/stones/{id}`: one stone with its path since the session began."""
        return StoneTrack.from_json(await self._get(f"/api/v2/session/world/stones/{int(id)}"))

    # ---- streams and long-poll (raw; `factory.streams` adds resume and typed items) ---------------------------------
    async def stream_frames(
        self,
        *,
        layers: Layers = None,
        format: Literal["sse", "ndjson"] = "sse",
        after: int | None = None,
        limit: int | None = None,
        max_s: int | None = None,
        observe: bool = False,
        precision: str | None = None,
        last_event_id: str | None = None,
    ) -> EventStream:
        """`GET /session/frames/stream`: open the frames stream; iterate the returned `EventStream`.

        It counts as a viewer (keeps the worker interpreting) unless `observe` is true.
        """
        params = {
            "layers": join_layers(layers),
            "format": format,
            "after": after,
            "limit": limit,
            "max_s": max_s,
            "observe": _flag(observe),
            "precision": precision,
        }
        return await self._open_stream("/api/v2/session/frames/stream", params, format, last_event_id)

    async def raw_stream(
        self,
        *,
        format: Literal["sse", "ndjson"] = "ndjson",
        after: int | None = None,
        limit: int | None = None,
        max_s: int | None = None,
        observe: bool = False,
        last_event_id: str | None = None,
    ) -> EventStream:
        """`GET /raw/stream`: the raw model codec stream (one `sip1` packet per message)."""
        params = {"format": format, "after": after, "limit": limit, "max_s": max_s, "observe": _flag(observe)}
        return await self._open_stream("/api/v2/raw/stream", params, format, last_event_id)

    async def _open_stream(
        self, path: str, params: Mapping[str, Any], fmt: str, last_event_id: str | None
    ) -> EventStream:
        headers = {"Accept": "text/event-stream" if fmt == "sse" else "application/x-ndjson"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        response = await self._http.send("GET", path, params=params, headers=headers, stream=True)
        return EventStream(response, fmt)

    async def poll_frames(
        self,
        *,
        cursor: int | None = None,
        rev: int | None = None,
        cid: str | None = None,
        layers: Layers = None,
        limit: int | None = None,
        wait: int | None = None,
        precision: str | None = None,
    ) -> LiveReply:
        """`GET /session/frames`: one long-poll; continue from the reply's `cursor` and `state.revision`."""
        params = {
            "cursor": cursor,
            "rev": rev,
            "cid": cid,
            "layers": join_layers(layers),
            "limit": limit,
            "wait": wait,
            "precision": precision,
        }
        return LiveReply.from_json(await self._get("/api/v2/session/frames", params))

    # ---- frames and codec ------------------------------------------------------------------------------------------
    async def get_frame(
        self, g: GridFrame, *, layers: Layers = None, session: int | None = None, precision: str | None = None
    ) -> Frame:
        """`GET /session/frames/{g}`: a retained frame (404 `not_retained`, 410 `session_gone` with `session`)."""
        params = {"layers": join_layers(layers), "session": session, "precision": precision}
        return Frame.from_json(await self._get(f"/api/v2/session/frames/{_grid(g)}", params))

    async def get_frame_picture(self, g: GridFrame, *, session: int | None = None) -> Picture:
        """`GET /session/frames/{g}/image.jpg`: the camera picture live frame `g` was interpreted from, as a JPEG.

        The newest 64 of a camera session are kept: an older frame, or a warm-up frame (never released), is 404
        `not_retained`; with `session`, a frame of an ended session is 410 `session_gone`. A recording has none (404
        `not_found`): its picture is the proxy video.
        """
        response = await self._http.send(
            "GET",
            f"/api/v2/session/frames/{_grid(g)}/image.jpg",
            params={"session": session},
            headers={"Accept": "image/jpeg"},
        )
        shown = response.headers.get("X-Interp-G", "")
        if not shown.isdigit():
            raise FactoryError(
                code="decode", status=response.status_code, message="the picture reply has no X-Interp-G header"
            )
        return Picture(g=int(shown), jpeg=response.content)

    async def get_frame_raster(
        self, g: int, raster: Literal["regions", "mask", "depth"], *, scale: int | None = None
    ) -> bytes:
        """`GET /session/frames/{g}/{raster}.png`: a retained frame's map as a PNG (64 x 40 times `scale`)."""
        return await self._bytes(f"/api/v2/session/frames/{int(g)}/{_id(raster)}.png", {"scale": scale}, "image/png")

    async def raw_packet(self, g: GridFrame, *, session: int | None = None) -> Sip1Packet:
        """`GET /raw/packets/{g}`: one raw `sip1` packet of the retained window."""
        return Sip1Packet.from_json(await self._get(f"/api/v2/raw/packets/{_grid(g)}", {"session": session}))

    async def raw_tensors(self, g: int) -> bytes:
        """`GET /raw/tensors/{g}`: the model's output tensors as `.npz` (debug only; 404 `debug_disabled` when off)."""
        return await self._bytes(f"/api/v2/raw/tensors/{int(g)}", None, "application/octet-stream")

    # ---- render-ready ----------------------------------------------------------------------------------------------
    async def render_overlay2d(
        self,
        *,
        g: int | None = None,
        t: float | None = None,
        mode: Literal["stable", "raw"] | None = None,
        draw: Literal["shapes", "primitives"] | None = None,
        session: int | None = None,
    ) -> Overlay2D:
        """`GET /session/render/overlay2d`: everything the 2-D overlay draws, in native px. Give exactly one of g, t."""
        _one_of(g, t)
        params = {"g": g, "t": t, "mode": mode, "draw": draw, "session": session}
        return Overlay2D.from_json(await self._get("/api/v2/session/render/overlay2d", params))

    async def render_scene3d(
        self,
        *,
        g: int | None = None,
        t: float | None = None,
        mode: Literal["stable", "raw"] | None = None,
        draw: Literal["shapes", "primitives"] | None = None,
        surface: Literal["vertices", "compact", "none"] | None = None,
        session: int | None = None,
    ) -> Scene3D:
        """`GET /session/render/scene3d`: everything the 3-D view draws, in scene world. Give exactly one of g, t."""
        _one_of(g, t)
        params = {"g": g, "t": t, "mode": mode, "draw": draw, "surface": surface, "session": session}
        return Scene3D.from_json(await self._get("/api/v2/session/render/scene3d", params))


def _grid(g: GridFrame) -> str:
    return "latest" if g == "latest" else str(int(g))


def _one_of(g: int | None, t: float | None) -> None:
    if (g is None) == (t is None):
        raise ValueError("give exactly one of g and t")


def _media_headers(
    range: tuple[int, int | None] | str | None, if_range: str | None, if_none_match: str | None
) -> dict[str, str]:
    headers = {"Accept": "video/mp4"}
    if range is not None:
        last = "" if isinstance(range, str) or range[1] is None else str(range[1])
        headers["Range"] = range if isinstance(range, str) else f"bytes={range[0]}-{last}"
    if if_range is not None:
        headers["If-Range"] = if_range
    if if_none_match is not None:
        headers["If-None-Match"] = if_none_match
    return headers


def _endless_media(id: str, status: int | None) -> FactoryError:
    return FactoryError(
        code="not_seekable",
        field="id",
        status=status,
        message=f"the media of {id!r} is a live stream that never ends; read it with open_video_media()",
    )


def _refuse_endless(id: str, response: httpx.Response) -> None:
    """Refuse a whole-file reply that has neither a length nor byte ranges: a live stream, which would never end."""
    h = response.headers
    if response.status_code == 200 and "Content-Length" not in h and h.get("Accept-Ranges") != "bytes":
        raise _endless_media(id, response.status_code)


def _media(response: httpx.Response, data: bytes) -> Media:
    h = response.headers
    length = h.get("Content-Length")
    return Media(
        status=response.status_code,
        data=data,
        content_type=h.get("Content-Type"),
        content_range=h.get("Content-Range"),
        etag=h.get("ETag"),
        accept_ranges=h.get("Accept-Ranges"),
        content_length=int(length) if length is not None and length.isdigit() else None,
    )

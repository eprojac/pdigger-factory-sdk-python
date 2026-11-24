# The low level

Use `factory.api` to request a specific frame, fetch a model output or control the shared session. The 32 typed
methods correspond to the [OpenAPI operations](../spec/openapi.yaml). The codec decodes packed maps, bitsets, depth
and joint data.

```python
frame = await api.get_frame("latest", layers=["overlay", "world"])
if frame.overlay is not None and frame.overlay.regions is not None:
    regions = frame.overlay.regions
    stone_map = codec.decode_u8_map(regions.stone, regions.rows, regions.cols)
    cells = stone_map.count() if stone_map else 0
    print(f"frame g {frame.g}: stone cells {cells} of {regions.rows} x {regions.cols}")
```

## Operations

Paths are under `/api/v2` (`/metrics` is at the root). The 20.20 unit serves all of them.

| Operation | HTTP | Method | Returns |
|---|---|---|---|
| `getApiRoot` | `GET /` | `get_api_root()` | `ApiRoot`: links, layer schemas, limits, the build |
| `getOpenApi` | `GET /openapi.yaml` | `get_openapi()` | the spec, as text |
| `getModel` | `GET /model` | `get_model()` | `ModelInfo`: the model, engine hashes |
| `getTopology` | `GET /topology` | `get_topology()` | `Topology`: skeleton, classes, palette, constants |
| `getHealth` | `GET /health` | `get_health()` | `Health` |
| `getReady` | `GET /ready` | `get_ready()` | `Ready`; `503 not_ready` (with `waiting`) until it is |
| `getStatus` | `GET /status` | `get_status()` | `Status`: everything an operator wants on one page |
| `getMetrics` | `GET /metrics` | `get_metrics()` | `Metrics`: the Prometheus text and its samples |
| `listVideos` | `GET /videos` | `list_videos(if_none_match=)` | `VideoList`, or `None` when unchanged (304) |
| `getVideo` | `GET /videos/{id}` | `get_video(id)` | `Source` |
| `getVideoMedia` | `GET /videos/{id}/media` | `get_video_media(id, range=)`, `head_video_media(id)`; `open_video_media(id, range=)` streams it | `Media`: bytes, status, range, total, ETag; `MediaStream` |
| `getSession` | `GET /session` | `get_session()` | `SessionReply`: the state, the worker, the sim |
| `sessionSelect` | `POST /session/select` | `session_select(video, time=, dry_run=)` | `ControlReply` |
| `sessionSeek` | `POST /session/seek` | `session_seek(time, video=, dry_run=)` | `ControlReply` |
| `sessionPlay` | `POST /session/play` | `session_play(dry_run=)` | `ControlReply` |
| `sessionPause` | `POST /session/pause` | `session_pause(dry_run=)` | `ControlReply` |
| `getFocus` | `GET /session/focus` | `get_focus()` | `FocusReply` |
| `putFocus` | `PUT /session/focus` | `put_focus(stone_id, g=, session=, dry_run=)` | `ControlReply` |
| `deleteFocus` | `DELETE /session/focus` | `delete_focus(dry_run=)` | `ControlReply` |
| `getWorld` | `GET /session/world` | `get_world(precision=)` | `WorldLayer` |
| `getWorldEvents` | `GET /session/world/events` | `get_world_events(after=, limit=, types=)` | `EventLog`; continue with `after=log.next_after` |
| `getStone` | `GET /session/world/stones/{id}` | `get_stone(id)` | `StoneTrack`: the stone and its path |
| `streamFrames` | `GET /session/frames/stream` | `stream_frames(...)` | `EventStream` of raw messages ([streams](streams.md#below-the-streams)) |
| `rawStream` | `GET /raw/stream` | `raw_stream(...)` | `EventStream` |
| `pollFrames` | `GET /session/frames` | `poll_frames(cursor=, rev=, cid=, layers=, limit=, wait=, precision=)` | `LiveReply`: the state, the worker, new frames |
| `getFrame` | `GET /session/frames/{g}` | `get_frame(g, layers=, session=, precision=)` | `Frame`; `g` is an int or `"latest"` |
| `getFramePicture` | `GET /session/frames/{g}/image.jpg` | `get_frame_picture(g, session=)` | `Picture`: `g` and the JPEG a live frame was interpreted from |
| `getFrameRaster` | `GET /session/frames/{g}/{raster}.png` | `get_frame_raster(g, raster, scale=)` | PNG bytes of `regions`, `mask` or `depth` |
| `rawPacket` | `GET /raw/packets/{g}` | `raw_packet(g, session=)` | `Sip1Packet` |
| `rawTensors` | `GET /raw/tensors/{g}` | `raw_tensors(g)` | `.npz` bytes; `404 debug_disabled` unless enabled |
| `renderOverlay2d` | `GET /session/render/overlay2d` | `render_overlay2d(g= or t=, mode=, draw=, session=)` | `Overlay2D`: what the 2-D overlay draws |
| `renderScene3d` | `GET /session/render/scene3d` | `render_scene3d(g= or t=, mode=, draw=, surface=, session=)` | `Scene3D`: what the 3-D view draws |

Every control takes `dry_run=True`, which validates without applying. The controls move the shared session for every
viewer, so prefer the high-level `factory.session`, which also handles the live cameras. The render operations take
exactly one of `g` and `t`; giving neither or both raises `ValueError` without a request.

```python
log = await api.get_world_events(limit=5, types=["lift", "place"])
for entry in log.events:
    print(f"event g {entry.g}: {entry.type} stone {entry.id} ({entry.side})")

overlay = await api.render_overlay2d(g=frame.g, draw="primitives")
polygons = overlay.primitives.polygons if overlay.primitives else None
print(f"overlay2d of g {overlay.g}: {len(polygons or [])} polygons at {overlay.native_wh} px")
```

`draw="primitives"` returns flat draw lists (polygons, segments, circles, labels, images) in native pixels, ready for a
canvas. `draw="shapes"` returns the overlay's own shapes. `render_scene3d(surface="vertices" | "compact" | "none")`
chooses how the observed surface comes: an explicit mesh, the compact form of `scene.surface`
(`SurfaceMesh.compact()`), or not at all.

Media comes by byte range, and the metrics come parsed:

```python
recording = next(s for s in await factory.sources() if not s.is_live)
first_kib = await api.get_video_media(recording.id, range=(0, 1023))
print(f"{recording.id}: {len(first_kib.data)} bytes of {first_kib.total}, etag {first_kib.etag}")

metrics = await api.get_metrics()
print(f"{len(metrics.samples)} samples; v2 requests: {metrics.value('interp_http_requests_total', route='v2')}")
```

`get_video_media` reads the reply into memory; a reply that has neither a length nor byte ranges would never end, so
it raises `not_seekable` (field `id`) and closes the connection. `open_video_media` reads media as a stream instead,
for large files: an async context manager that gives a `MediaStream`, with the reply's headers in `stream.media` and
the body in chunks as they arrive. Leaving the block, `aclose()` or cancelling the task closes the connection.

A live camera has no media (`404 not_found`): its pictures come with its frames. `get_frame_picture(g)` is the JPEG a
live frame was interpreted from (640 x 360 on the plant rigs; the layers' native pixels are that picture stretched to
`native_wh`), with the frame's `g` (`"latest"` gives the newest); the unit keeps the newest 64, and a recording has
none (`not_found`):

```python
frame = await factory.api.get_frame("latest", layers=["overlay"])
picture = await factory.api.get_frame_picture(frame.g, session=frame.session)
print(f"{state.video} frame g {picture.g}: a {len(picture.jpeg)}-byte JPEG, {state.live.phase}")
```

## Models

Every schema of the spec is a dataclass in `pdigger_factory_sdk.models` (slots, keyword-only), parsed by an explicit
`from_json()`:

- Parsing is tolerant: fields the SDK does not know are ignored, and every model keeps the JSON object it came from in
  `raw_json`, unknown fields included.
- A reply that does not fit (a required field missing, a wrong type) raises `FactoryError` with code `decode`.
- Integers in the spec (`g`, `cursor`, `session`, ids) are Python ints; fields a public recording redacts (face
  keypoints, appearance masks, depth textures) are optional.
- The models are generated from the spec by `tools/gen_models.py`; a test fails if they are out of date.

## Errors

One type, `FactoryError`, with `status` (`None` without an HTTP reply), `code`, `message`, `field`, `retry_ms`,
`retained` (the retained window of a `not_retained` error), `waiting` (of `not_ready`), `body` and `headers`. Branch on
`code`; `ErrorCode` lists every code, and its members compare equal to the strings. Besides the API's codes, the SDK has
five of its own: `transport` (no HTTP reply: the connection failed, broke or timed out), `decode` (a reply that is not
the documented JSON, or a body that does not decode, such as a bad `Content-Encoding`), `live_unavailable` (no live
camera in the catalog), `live_timeout` (`session.until_live()` waited in vain) and `stream_closed` (a stream ended or
broke and was not resumed). An error reply whose body
breaks or does not decode keeps its status and gets the status's code. Every httpx failure becomes one of these: no
httpx exception reaches your code. Some errors come from the SDK itself, without a request, and have no `status`: a
base URL that is not `http(s)://host[:port]` is refused when the client is made (`invalid_param`, field `base_url`),
and a seek on a live camera, or a time given for one, raises `not_seekable`. The helpers are `is_retryable`,
`is_not_retained` and `is_session_gone`, and a `FactoryError` pickles (the headers travel as a plain dict).

```python
state = await factory.session.state()
try:
    await factory.api.get_frame("latest", session=state.session - 1)
except FactoryError as e:
    if not e.is_session_gone:
        raise
    print(f"HTTP {e.status} {e.code}: {e.message}")
```

## Retries and timeouts

`FactoryClient(retries=3, timeout=10.0, connect_timeout=5.0, stream_timeout=30.0, backoff=Backoff())`:

- GET and HEAD are retried on connection errors (also when the body breaks while it is read) and on 429, 502, 503 and
  504. POST, PUT and DELETE are retried only on 429, which the unit sends before it applies anything. No other 4xx is
  retried, and neither is a body that does not decode. Retries are logged at debug level to the `pdigger_factory_sdk`
  logger.
- A retry waits as the unit asks (`retry_ms` in the error body, else `Retry-After`, in seconds or as a date, which is
  measured against the reply's `Date`, else the client's `wall_clock`), else the backoff: 0.25 s doubling to 5 s, with
  full jitter. A seeded `rng=random.Random(n)` makes the jitter repeatable. A request that cannot be sent at all (a
  scheme without a transport) is not retried.
- `stream_timeout` is the longest silence on a stream before it counts as broken (and is resumed).
- `api.with_options(retries=0, timeout=2.0)` gives the same API with other settings for a few calls:

```python
try:
    ready = await factory.api.with_options(retries=0).get_ready()
    print(f"ready: {ready.ready}")
except FactoryError as e:
    if e.code != ErrorCode.NOT_READY:
        raise
    print(f"not ready yet, waiting for {e.waiting}")
```

Other client options: `headers`, `token` (sent as `Authorization: Bearer`; 20.20 ignores it, the shipped unit may
require it for controls), `user_agent_suffix` (appended to the user agent, `pdigger-factory-sdk-python/<version>`: `USER_AGENT`), `transport` or
`http_client` (your own httpx client, which the SDK does not close), and `sleep`, `monotonic`, `wall_clock` and `rng`
for tests. The client sends no `Origin` header, so the unit's same-origin check on controls lets it through.

## Codec

`pdigger_factory_sdk.codec` decodes what the layers pack:

| Function | |
|---|---|
| `decode_u8_map(b64, rows, cols)` | an overlay map: base64 of `rows x cols` bytes, as a `Grid` (`at`, `row`, `count`, `to_lists`) |
| `decode_zlib_map(b64, rows, cols)` | a `sip1` map: base64 of zlib |
| `decode_bitset(b64, n)` | `n` booleans, least significant bit first |
| `depth_log_z(q, lo, hi, a, b)`, `depth_z(...)`, `decode_depth(grid, lo, hi, a, b)` | depth: `logZ = a * (lo + q / 255 * (hi - lo)) + b` |
| `decode_f32le(b64)`, `decode_u16le(b64)` | surface positions (xyz triplets) and triangle indices |
| `joints(values)` | `Joint`s from `[u, v, vis, x, y, z]` or null |
| `stone_items(values)` | `StoneItem`s from `[u, v, w, h, score, X, Y, Z, w_m, h_m, yaw]` |
| `uv_to_px`, `px_to_uv`, `scene_to_three`, `three_to_scene`, `quat_to_yaw` | coordinates ([concepts](concepts.md#coordinates-and-units)) |
| `Skeleton(topology)` | bones, wrist and fingertip joints, and the bones a set of joints can draw (`visible_bones`) |

An absent map (redacted in the public recordings) decodes to `None`.

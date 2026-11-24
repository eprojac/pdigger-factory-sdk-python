# Concepts

A factory unit follows one camera or recording at a time. Its **world** is the tracker’s current view of the stones,
hands and events. Use this guide to interpret frames, coordinate systems and changes to the shared session.
[Measurements from the 20.20 unit](BEHAVIOUR-20.20.md).

## The unit and its API

The 20.20 test unit is a Jetson Orin NX on the factory network. Its model detects hands, stones and image regions.
The Stable simulation tracks them across frames. [API v2](../spec/openapi.yaml) serves both the detections and the
tracking results. Set `PDIGGER_FACTORY_URL` to the unit’s base URL, such as `http://factory-orin.local:8120`.

## One shared session

The unit’s own page, the operators’ screens and SDK clients share one source and playhead. Selecting a source or
seeking starts a new session and assigns a new `session` number. Each `factory.session` control accepts
`dry_run=True` to preview its effect on viewers.

```python
shared = await factory.session.clock()
state = shared.state
playing = "playing" if state.playing else "paused"
print(f"session {state.session} shows {state.video}, {playing} at {state.rate}x")
```

The session's state (`State`) carries the source (`video`), `playing`, the `rate`, the playhead as an anchor
(`anchor_media` at server wall time `anchor_wall`), `hold_until` (the worker's warm-up after a select or seek),
`duration`, the `focus` (a stone highlighted for every viewer), a `revision` that counts every change, and `changed_by`.
`factory.session.clock()` samples it together with the round trip and returns a `SessionClock`. The clock maps between
media time, the server's wall clock and yours: `media_time_now()`, `wall_of(t)`, `local_wall_of(t)`, `progress()`.

## Sources and the live cameras

A **source** is what the session can show. The API calls sources videos: `GET /api/v2/videos` is the catalog and
`factory.sources()` returns it as `Source` models. There are two kinds:

- **recordings** (`kind: recording`): a video file with a `duration` and a `default_start`. The session plays it at
  the unit's rate, and you can seek in it. On 20.20, build `claude-20261114-int17` played at 0.8 (its worker keeps up
  at 0.8, not at 1.0, on the full-frame videos); build `claude-20261122-hr4x1` sets the rate itself to what its worker
  keeps up with, each change a `clock` event with `changed_by: auto-rate` (dry1 at 0.75, for about a minute down to
  0.63; its dry2 sessions in the fixtures at 0.75; `dry1-0000-0130` goes from 0.8 to 0.74 and then 0.75 in its first
  23 s). When it ends, the session moves on to the next recording of the catalog (cameras are skipped; after the last
  recording, the first).
- **live cameras** (`kind: camera`, `live: true`, `seekable: false`), listed after the recordings by their serials
  (`CAM00000001`): the camera itself, at rate 1.0, with no duration, no frame count and no media. `hold` is the
  warm-up: the seconds of camera frames the interpreter runs on before the first is shown (3). The 20.20 unit lists the
  six cameras of the plant rigs. On build `claude-20261120-looks1` they streamed at 1.6 to 3.9 frames a
  second; build `claude-20261122-hr4x1` interprets about 3.6 a second on a camera (274 ms a frame, 35 ms of it decoding
  the camera's JPEG), live about 0.5 s behind the camera. `factory.cameras()` returns them, and `factory.live()` the
  first (or `live_unavailable`).

Call `session.watch(camera)` to start a camera session at media time zero and playback rate 1.0. Read `state.live`
(`phase`, `warmup`, `since`, `lag`) to follow the connection.

| Phase | What the unit is doing |
| --- | --- |
| `connecting` | Waiting for the camera’s first frame. The worker status explains a delay. |
| `warming` | Feeding the first frames to the interpreter. The unit discards these frames after warm-up. |
| `live` | Publishing each interpreted frame and its camera image. |

Each transition increments the session revision and reaches clients through long-poll or a `clock` event.
`session.until_live()` polls every 250 ms.

Frame numbers start at zero and include warm-up. At five frames per second, the first visible frame is about 15.
Media time measures seconds since the camera started, and the playhead trails the camera by its reported lag. The
unit keeps the latest 64 camera JPEGs. Read one with `session.picture(g)`.

A camera has no time. A select with a `time` is refused (`400 not_seekable`, field `time`), and so is a seek
(`409 not_seekable`, field `video`): `session.watch(camera, at=...)` raises without sending the select, and
`session.seek()` reads the shared state first (another viewer may have switched the session) and refuses locally when it
shows a camera. `pause` freezes the shared view and stops the interpreter on the camera; `play` continues at the live
edge in the same session, after a new warm-up, so `g` jumps (by 5) over the pause, as it does after a camera stopped
answering. `Source.is_live` tells the kinds apart; an entry without `kind`, `live` or `seekable` (the unit's recordings
omit them) is a seekable recording.

On a camera, media time only counts the seconds since the session started. The useful timestamp of an event is its
**wall time**. The SDK computes the server wall time of every event and activity (`wall`) from the shared state:
`start + (t - anchor_media) / rate`, where `start` is `max(anchor_wall, hold_until)` while the session plays (the unit
holds the clock until `hold_until` after a select or a seek) and `anchor_wall` while it is paused; there is none while
`rate` is 0 or before a state of the frame's session is known. The unit sends a `clock` event only when the state changes, so the
interpreter reads the state with `GET /session` after every connect and takes the stream's clock events after that.

## Frames

The unit interprets every source on the same **grid**: five frames per media second, numbered `g` from 0 at the
start of the video. A **frame** (`Frame`) is one grid frame with its media time `t`, its `session` and `video`, and
the layers you asked for:

| Layer | Model | What it holds |
|---|---|---|
| `raw` | `Sip1Packet` | the model's packet (`sip1`): maps, boxes, the person and its joints, stones, tray, hands |
| `overlay` | `OverlayLayer` | the 2-D overlay: region maps, the hand mask, boxes, wrists, stones in image space |
| `scene` | `SceneLayer` | the 3-D scene: skeleton, stones and the observed surface, in scene units |
| `cards` | `CardsLayer` | the hand cards of the unit's page (`Holding a stone`) |
| `world` | `WorldLayer` | the Stable sim: stones, hands, this frame's events, stats, the board |

The interpreter asks for `world` and `cards`. A stream asks for `overlay,scene,cards,world` unless you choose. A frame
is released at most 300 ms after its packet. A layer that misses that deadline is listed in the frame's `missing`; on
20.20 this happened to the world in 3 to 4 % of the frames of dry1 and dry2 on build `claude-20261114-int17` (never on
aug28 and jul30), and in 507 of dry1's 9,042 frames (5.6 %) on build `claude-20261122-hr4x1` (dry1 is the one
recording of 22 November made on the unit). The unit never pushes such a frame again, so the SDK can **backfill** it:
it holds the frame back and reads the layer from the retained window ([streams.md](streams.md#late-layers-backfill)).

The unit keeps the newest 512 frames, the **retained window**: 102 s of media, which is 128 s of wall time at rate 0.8
and about 137 s at 0.75. `GET /session/frames/{g}` reads any of them. A frame outside the window is `404 not_retained`,
and the error says which window is retained (`FactoryError.retained`). A read that names its session (`session=`)
fails with `410 session_gone` once that session has ended, instead of returning a frame of another session.

## Cursors, event ids and resuming

Every frame the unit releases has a **cursor**, a number that only grows during one boot of the server. In a stream,
each frame event has the id `<boot>-<cursor>` (`a2011b037b-10391`). A reconnect sends the last id as `Last-Event-ID`,
and the unit continues after it, so a stream that resumes loses nothing that is still retained. After a restart the
boot differs: the unit sends a `reset` event and continues from its newest frame. The long-poll uses the cursor
directly (`cursor=`), together with the state's `revision` (`rev=`).

## Revisions

While the session is paused, the world of the paused frame is refined, and the unit releases it again with a higher
`world.rev`. The SDK passes such a frame on marked as a revision (`FrameItem.is_revision`, `Update.is_revision`). The
interpreter emits only the events of a revision that it has not emitted for that frame already. A frame that comes
again with the same or a lower `rev` is a duplicate and is dropped.

## Viewers and `observe`

The worker interprets only while somebody watches. A stream counts as a viewer unless it asks with `observe=1`. With
the recordings an idle worker loses nothing, because the video is still there. With the camera, the time nobody
watched is gone. So the interpreter counts as a viewer by default (`observe=False`). An integration that counts
moves should keep one running all the time. Pass `observe=True` to watch without keeping the worker busy: dashboards,
and checks against a production unit.

## Coordinates and units

- **Image space**: `u`, `v` in 0..1 across the model crop, the part of the source image the model sees;
  `codec.uv_to_px(u, v, width, height)` gives pixels (`px = u * width - 0.5`) and `codec.px_to_uv` goes back.
  `native_wh` is the crop's size in pixels (native px), and the source's `crop` (`x, y, w, h` in source pixels) places
  it in the source image of size `source_wh`.
- **Scene space**: `x`, `y`, `z` in **scene units**, the template gauge of the work area, not metres. Positions are
  `solid.pos`, orientations quaternions `solid.q` (`codec.quat_to_yaw`), sizes `solid.dims`. A move's `distance` is in
  these units. `codec.scene_to_three([x, y, z])` gives three.js axes (`[x, z, -y]`).
- **Time**: `t` is media seconds, `g` the grid frame, and `wall` the server's wall clock (epoch seconds). The text
  prints media time as `M:SS.s` (`21:17.8`), or `H:MM:SS.s` from one hour.

## Precision

Streams and reads take `precision="rounded"` (the default: outlines to 0.01 px, positions to 1e-4, quaternions to
1e-5) or `"full"` (unrounded, for parity runs). The interpreter's events, activities and text are the same with
either.

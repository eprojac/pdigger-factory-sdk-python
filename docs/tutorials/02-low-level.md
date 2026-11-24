# Low level: one call per operation

Under the interpreter, `factory.api` has one async method for each of the unit's 32 operations, each returning a
typed model of its reply (fields the SDK does not know are kept in `raw_json`), and `pdigger_factory_sdk.codec`
decodes what the replies pack: maps, bitsets, depth, joints and stone tuples. Use this level to build what the
interpreter does not: your own drawing, a dashboard of the unit, a bridge to your MES or ERP, a check of the model.

The code is [examples/tutorial_low_level.py](../../examples/tutorial_low_level.py), one function per step; run them
all with `python examples/tutorial_low_level.py`, or some by name (`... tutorial_low_level.py hands image`). Every
step only reads, or sends a dry run. The full list of operations is in [low-level.md](../low-level.md#operations).

## 1. Is it ready, and how fast?

```python
api = factory.api
ready = await api.get_ready()  # while it starts: FactoryError not_ready, and what it waits for
worker = (await api.get_status()).worker
yes = str(ready.ready).lower()  # true or false, as the other languages print it
print(f"ready {yes}, worker {worker.state}: {worker.fps} frames a second, {worker.frame_ms} ms each")
```

The status also has the time of each stage of the model (`worker.stage_ms`), the frames the unit keeps (`retained`),
and how many viewers, long-polls and streams it serves. `get_health()` is the short answer for a load balancer, and
`get_metrics()` the Prometheus text.

## 2. The model, and what to draw with

```python
model = await api.get_model()
print(f"checkpoint {model.checkpoint} on {model.backend}: {len(model.engines)} engines")
topology = await api.get_topology()
print(f"{topology.joints} joints, {len(topology.bones)} bones, {len(topology.colors)} colours")
```

The model is the unit's cascade, engine by engine with its hash; the topology is what every drawing is made with: the
skeleton's bones, which joints are the wrists and the fingertips, and the colours the unit's own page uses.

## 3. A stone's track, and the focus

```python
world = await api.get_world()
if not world.stones:  # between trays, or a camera with nothing in view
    print("no stone in the newest world")
    return
track = await api.get_stone(world.stones[0].id)  # its path, from frame first_g to last_g
print(f"stone {track.id}: {len(track.path2d)} points, g {track.first_g} to {track.last_g}")
reply = await api.put_focus(track.id, dry_run=True)  # the unit checks it; nothing changes
print(f"focus on stone {track.id}: dry run {str(reply.dry_run).lower()}")
```

The focus highlights one stone for every viewer of the unit, so here it is a dry run: the unit checks the request and
answers with the state it would have, without changing it. The same holds for every control of the shared session
(select, seek, play, pause): the session's own methods are in [examples/session_control.py](../../examples/session_control.py),

```python
shared = await factory.session.clock()
state = shared.state
playing = "playing" if state.playing else "paused"
print(f"session {state.session} shows {state.video}, {playing} at {state.rate}x")
print(f"playhead at {clock(shared.media_time_now())}; the server's clock is {shared.offset:+.3f} s from ours")
```

and a control the unit refuses raises a `FactoryError` with the field it refused
([examples/error_handling.py](../../examples/error_handling.py)):

```python
try:
    await factory.session.seek(-1.0, dry_run=True)
except FactoryError as e:
    print(f"{e.code} (field {e.field}): {e.message}")
```

## 4. A frame, and the model's maps of it

A frame carries the layers you ask for: `world` (the stones, hands and events of the world model), `cards` (the hand
cards' titles), `overlay` (the model's 2-D view: regions, boxes, joints), `scene` (the 3-D scene) and `raw`.

```python
frame = await api.get_frame(g, layers=["world", "cards"])
families = ", ".join(sorted({s.family for s in frame.world.stones})) if frame.world else ""
print(f"frame g {frame.g} at {clock(frame.t)}: {len(frame.world.stones) if frame.world else 0} stones, {families}")
packet = await api.raw_packet(frame.g)  # what the model saw, before the world made sense of it
rows, cols = packet.maps.grid
tray = codec.decode_zlib_map(packet.maps.tray, rows, cols)  # one byte a cell
stones = codec.decode_zlib_map(packet.maps.stone, rows, cols)
```

`g` is a frame of the grid (`"latest"` for the newest); the unit keeps the newest 512 of the session.

## 5. Draw lists, ready to paint

The unit also sends what its own views draw: the 2-D overlay in video pixels, as shapes with their style, and the 3-D
scene in scene units. Paint them with any library; no SDK code is needed between the reply and your canvas.

```python
at = (await api.get_frame(g, layers=["world"])).g
overlay = await api.render_overlay2d(g=at, draw="primitives")  # shapes, styled, in video pixels
p = overlay.primitives
```

Each polygon has its `points` and `stroke`, each segment its `from`, `to` and `color`, each circle its `at`, `r` and
`fill`. The scene:

```python
scene = await api.render_scene3d(g=at, surface="compact")  # stones, hands and the work plane, in scene units
print(f"scene of g {scene.g}: {len(scene.stones)} stones, {len(scene.skeleton.bones or [])} bones")
```

## 6. Stone shapes, drawn by hand

To draw it your own way, start from the world layer: each stone's outline is a polygon in video pixels, and the
topology gives the colours.

```python
colors = (await api.get_topology()).colors
frame = await api.get_frame(g, layers=["world"])
shapes = []
for stone in frame.world.stones if frame.world else []:
    fill = colors["stones"] if stone.detected else colors["stoneBox"]
    shapes.append((fill, [(x, y) for x, y in stone.outline]))  # hand these to any drawing library
```

## 7. Hands and bones, drawn by hand

The overlay layer has the 70 joints of the body and both hands, in content units; the topology names the bones and
the wrists, and `uv_to_px` turns a joint into video pixels.

```python
topology = await api.get_topology()
skeleton = Skeleton(topology)
frame = await api.get_frame(g, layers=["overlay", "cards"])
width, height = (await api.get_video(frame.video)).native_wh
js = joints(frame.overlay.joints if frame.overlay and frame.overlay.joints else [])  # u, v, visibility, x, y, z
px = {i: uv_to_px(j.u, j.v, width, height) for i, j in enumerate(js) if j}
bones = [(px[a], px[b]) for a, b in skeleton.visible_bones(js)]  # lines to draw, in video pixels
```

In the fixture dry1-0296-0306-full, at frame 1510 (5:01.2), that is 50 of the 62 bones, with the left hand on a stone
and the right one holding one.

## 8. A frame's image data

```python
frame = await api.get_frame(g, layers=["world"])
png = await api.get_frame_raster(frame.g, "regions")  # also "mask" and "depth"
print(f"regions of g {frame.g}: a {len(png)}-byte PNG")
video = await api.get_video(frame.video)
if video.is_live:  # a camera has no video: each frame comes with the JPEG it was interpreted from
    picture = await api.get_frame_picture(frame.g, session=frame.session)
    print(f"picture: a {len(picture.jpeg)}-byte JPEG of g {picture.g}")
else:  # a recording's picture is its video at frame.t
    print(f"picture: {video.proxy} at {clock(frame.t)}, {video.proxy_wh[0]} x {video.proxy_wh[1]}")
```

A recording's video is served by byte ranges, so any player can seek in it
([examples/low_level_api.py](../../examples/low_level_api.py)):

```python
recording = next(s for s in await factory.sources() if not s.is_live)
first_kib = await api.get_video_media(recording.id, range=(0, 1023))
print(f"{recording.id}: {len(first_kib.data)} bytes of {first_kib.total}, etag {first_kib.etag}")
```

On a live camera there is no video: every frame comes with the camera's own picture, the JPEG it was interpreted from:

```python
frame = await factory.api.get_frame("latest", layers=["overlay"])
picture = await factory.api.get_frame_picture(frame.g, session=frame.session)
print(f"{state.video} frame g {picture.g}: a {len(picture.jpeg)}-byte JPEG, {state.live.phase}")
```

## 9. Frames as they are made

The streams are the level between the operations and the interpreter: the frames themselves, resumed with
`Last-Event-ID` when the connection drops, in order, with the side events between them (clock, worker, gap, reset).
From [examples/mid_level_streams.py](../../examples/mid_level_streams.py):

```python
async with factory.streams.frames(["world"], backfill=True, clock_on_connect=True) as stream:
    async for item in stream:
        match item:
            case FrameItem(frame=frame, is_revision=revised, is_backfill=late):
                world = "no world" if frame.world is None else f"{len(frame.world.stones)} stones"
                note = " (revised)" if revised else " (late world read back)" if late else ""
                print(f"frame g {frame.g} at {clock(frame.t)}: {world}{note}")
```

Where a stream cannot stay open, the long-poll gives the same frames (`factory.streams.poll`), and the raw packets are
the model's output before the world (`factory.streams.raw_packets`). More in [streams.md](../streams.md).

## Next

- [Live cameras](03-live-cameras.md): select a plant camera and follow it.
- [Python](04-python.md): errors, retries, logging and testing in this SDK.

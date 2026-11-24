# Live cameras: one source at a time

A unit shows one source at a time, to everybody: a recording it plays, or a live camera of a plant rig. How many of
each it offers depends on the distribution of the unit (a unit can list several recordings and several cameras, or
only one of them), but there is always exactly one selected, on the one shared session that the unit's page, the
operators' screens and every SDK client follow. Selecting a camera changes what every viewer sees.

The code is [examples/live_camera.py](../../examples/live_camera.py) and
[examples/session_control.py](../../examples/session_control.py).

## 1. The sources

```python
sources = await factory.sources()
for source in sources:
    length = "live" if source.duration is None else clock(source.duration)
    print(f"{source.id}: {source.title} ({source.kind}, {length})")
```

A recording has a duration and can be sought in; a camera (`kind` camera, `is_live`) has neither: it is the camera
itself, at rate 1.0. The cameras are listed after the recordings, by their serials, and `factory.cameras()` returns
only them.

## 2. Watch a camera

```python
cameras = await factory.cameras()  # the plant cameras, by serial
camera = await factory.source(serial) if serial else (cameras[0] if cameras else None)  # that one, or the first
if camera is None:
    print("this unit lists no camera")
    return
await factory.session.watch(camera)  # connecting, then warming up on the camera's first 3 s
state = await factory.session.until_live()
lag = state.live.lag if state.live else 0.0
print(f"{camera.id} ({camera.title}) is live: session {state.session}, {lag:.2f} s behind the camera")
```

`watch` starts a new session on the camera. The unit connects to it, runs the interpreter on its first seconds of frames
to warm the world up (those frames are never shown or stored), and then shows it live: the session's `state.live.phase`
goes from `connecting` to `warming` to `live`, and `until_live()` waits for the last. The unit interprets only while
someone watches: the watch counts as a viewer for 15 s, and the interpreter's stream opened next keeps the camera live.
To check a watch without changing anything, send it as a dry run:

```python
cameras = [s for s in sources if s.is_live]
target = cameras[0].id if cameras else sources[0].id
would = await factory.session.watch(target, dry_run=True)
print(f"watching {target} would start session {would.session} at {clock(would.anchor_media)}")
```

## 3. The camera's pictures

Every frame of a camera comes with the camera's own picture, the JPEG the model saw; the unit keeps the newest 64.

```python
picture = await factory.session.picture()  # the JPEG the newest frame was interpreted from
print(f"frame g {picture.g}: a {len(picture.jpeg)}-byte JPEG")
```

## 4. Follow it

The interpreter follows a camera exactly as it follows a recording. On a camera, media time only counts the seconds
since the session started, so the time that matters is each event's wall time:

```python
seen = 0
async for event in factory.interpreter().events():
    at = time.strftime("%H:%M:%S", time.gmtime(event.wall)) if event.wall else "--:--:--"
    print(f"{at} UTC  {event.describe()}")  # on a camera, the wall time is the timestamp that matters
    seen += 1
    if events is not None and seen >= events:
        break
```

## What differs on a camera

- A camera cannot be sought: a seek raises `not_seekable`, and `session.seek()` refuses locally when the shared state
  shows a camera. `pause` freezes the shared view and `play` goes on at the live edge, after a new warm-up.
- A camera's frames come at the camera's own rate, as far as the worker keeps up (on the 20.20 unit 1.6 to 3.9 a
  second on build `claude-20261120-looks1`, and about 3.6 a second on build `claude-20261122-hr4x1`, whose worker takes
  274 ms a camera frame), where a recording gives 5 a media second; `g` counts them from the start of the session.
- If no camera is offered, `factory.live()` raises `live_unavailable`; `until_live()` raises `live_timeout` when the
  camera does not come up in time.
- For tests, `FakeFactory(recording=..., live=True)` offers a camera, `cam-live`, made from the recording, with its
  phases and pictures ([testing.md](../testing.md)).

The details are in [concepts.md](../concepts.md#sources-and-the-live-cameras).

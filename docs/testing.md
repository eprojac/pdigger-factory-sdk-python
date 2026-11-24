# Testing your code

`pdigger_factory_sdk.testing` lets you unit-test code that uses the SDK with no unit, no network and no real clock.
The SDK's own tests use it for everything.

## `FakeFactory`

`FakeFactory` is an httpx transport that answers API v2 from a recording (the root, model, health, status, sources,
session, focus, world, events, stones, frames, raw packets, streams and controls), from recorded replies (any
operation), or both. Pass it to the client as its transport:

```python
async def test_count_moves() -> None:
    fake = FakeFactory(recording=RECORDING)
    async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep) as factory:
        assert await count_moves(factory, limit=44) == {"carried_away": 2, "dropped": 1, "placed": 4}
```

Here `count_moves` is the code under test. It follows `factory.interpreter().activities()` and counts the moves of
each outcome:

```python
async def count_moves(factory: FactoryClient, limit: int) -> dict[str, int]:
    """Your code: count the moves of each outcome among the first `limit` activities (moves and push bursts)."""
    counts: dict[str, int] = {}
    seen = 0
    async with factory.interpreter().activities() as activities:
        async for activity in activities:
            if isinstance(activity, Move) and activity.outcome is not None:
                counts[activity.outcome] = counts.get(activity.outcome, 0) + 1
            seen += 1
            if seen == limit:
                break
    return counts
```

`FakeFactory(recording=..., replies=..., *, live=False, boot="fakeb00t01", time=None, delay=None, realtime=False,
linger=0.0, sleep=None, on_exhausted="raise", files=None)`:

- **`recording`**: a `pfr1` file (path, `.ndjson` or `.ndjson.gz`) or its lines as dicts. The fake serves its frames
  as the session: the catalog (from the header's `source`), the state (from its clock lines), `frames/{g}`, `world`,
  `world/events`, `world/stones/{id}`, `raw/packets/{g}`, the long-poll, and both streams (SSE and NDJSON). The streams
  honour `layers`, `after`, `Last-Event-ID`, `limit` and `max_s`, and carry the recording's clock and worker lines as
  side events. As on the unit, a stream gets no `clock` event when it connects: the clock lines before its first frame
  are the state it starts in (`GET /session` answers with it), and the later ones come as the changes they were, as
  do a camera's phases taken after it connected. The long-poll takes the clock lines as it passes them too: a reply
  ends before the next one, and the next reply carries its state with the frames after it, so every reply has the
  state in force at its frames; `GET /session` answers the state of the last line a stream or a long-poll passed.
- **`linger`**: seconds a stream that reached its `limit` or `max_s` stays open, sending nothing, before the fake
  closes it, as the 20.20 unit holds a finished stream about 10 s (waited on the fake clock; the default 0 closes it
  at once). The SDK closes such a stream itself, and a test with `linger=10.0` shows that it never waited.
- **`replies`**: recorded replies, as a folder in the layout of `fixtures/api` (`INDEX.json` and files) or a map of
  `Reply` objects. They answer what a recording cannot (the OpenAPI document, topology, metrics, renders, rasters, a
  recording's media), and everything when there is no recording (with `live=True` the camera still answers the frames,
  the world, the long-poll and the streams, also without a recording). Without them those operations reply 404
  `not_found` ("this FakeFactory has no reply ..."), and `raw_tensors` replies `debug_disabled`, as the unit does.
- **controls** are validated like the unit's, with its error codes, fields and messages, and applied to the fake's
  state. The checks come in the unit's order: the video, a camera, then the time (`"time": null` is a time of the wrong
  type). `dry_run` is 0 or 1 (else `400 invalid_param`, field `dry_run`); a dry run answers the state it would give and
  moves nothing. A select or seek starts a new session whose frames are the recording's from that time on, its id one
  higher than the newest the fake has used. Every change, a control or a phase of the camera, is a new `revision` and
  a `clock` event; a repeat (a pause while paused) changes nothing.
- **`live=True`** adds a live camera, `cam-live` (`testing.LIVE_CAMERA`), shaped as the 20.20 unit's plant cameras
  (`kind: camera`, `hold` 3, no media, the size of the recording's video) and listed after the recording; the session
  shows it at the start, already live. The camera sends the recording's frames as a camera session does: `g` from 0
  (`i` the same, and the raw packet's `g`, `i`, `t` and `cursor` the frame's), `t` the arrival in seconds since the
  session started (from `CONNECT_S`, 0.5), its sessions end to end. A select of it starts a session as the unit (build
  live3) does: `connecting` for `CONNECT_S`, then `warming` while the interpreter runs on the first `hold` (3) seconds
  of frames, which are never released, then `live` (`LIVE_LAG_S`, 0.25 s, behind the camera), each phase a new
  `revision` and a `clock` event sent as of its own time. The phases follow the fake's clock while the session plays:
  `session.until_live()` waits for them by sleeping on it, `fake.advance(seconds)` moves it on, and
  `fake.live_phase()` is the phase now. Nothing runs while the session is paused (a pause keeps the phase). A play goes
  back to `connecting` in its own reply and plans the phases again from then (warming changes only the phase, as on
  the unit, so its `lag` is the last one: 0 after a select, the live one after the play of a live camera). On a camera
  that was live a play continues at the live edge (`now - since - lag`), and the camera's next frames come after a
  break (`g` jumps by 5) and after the frames it sent already (the fake's camera sends its whole run at once when it
  goes live); their warm-up is never released either. A stream that comes to a phase before its time moves the fake's
  clock on to it, a stream on a paused camera waits for a control, and a long-poll takes the phases due within its
  `wait` (moving the clock on to them) and otherwise waits the rest of its `wait` on the fake's clock. The newest
  `KEEP_PICTURES` (64) released frames have a picture (`frames/{g}/image.jpg`, a small grey JPEG with
  `FakeFactory <video> g <g>` in a comment segment); an older frame's is `404 not_retained`, without a `retained`
  window, as on the unit. Before the camera's first frame is released, `frames/latest` and `raw/packets/latest` are
  `404 not_retained` ("no frame is retained yet", with an empty `retained` window), the world and a focus are
  `503 world_unavailable`, and the world events are `[]`, without `oldest_g`; a fake with only its camera
  (`FakeFactory(live=True)`, no recording) stays so: it is live, with no frames. The camera has no media
  (`404 not_found`). A seek whose video is a camera (the one it names, else the one shown) is `409 not_seekable` (field
  `video`); a seek naming a recording is applied, and leaves the camera, as on the unit; a select of the camera with
  any time, null too, is `400 not_seekable` (field `time`). A recording selected after the camera plays at 0.8. Unlike
  the unit, the fake interprets whether anyone watches or not, and its camera never stops: a session goes back to
  `connecting` only on a play.
- **`files`**: paths served as plain files (for example a recording for `Replay.open(url)`); the sync transport serves
  only these.

It records what it was asked: `requests`, `streams_opened`, `streams_closed`, `viewers` (the open streams that count
as viewers, those without `observe=1`; unlike the unit, the fake counts neither a long-poll with a `cid` nor a control
as one), `cids` (the client ids of the long-polls), `exhausted` and the `state`.

## Ending an endless loop

The interpreter resumes its stream forever, as it should against a unit. Against a fake, once a stream has served the
end of the recording, the next request that would get no new frames raises `FakeExhausted` (with
`on_exhausted="raise"`, the default; not while a camera's next run is still to come, after a play). That ends the
test's loop:

```python
async def test_the_whole_recording() -> None:
    fake = FakeFactory(recording=RECORDING)  # the follow never ends by itself: the fake ends it when it runs dry
    async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep) as factory:
        with pytest.raises(FakeExhausted):
            await count_moves(factory, limit=10_000)
```

With `on_exhausted="end"` the fake behaves like an idle unit instead: streams end, and long-polls wait their `wait`
on the fake clock and return nothing. Use it with `resume=False`.

## Faults

- `fake.fail(operation, status=503, code="busy", times=1, retry_ms=None, retry_after=None, body=None, exc=None)`: the
  next `times` requests of an operation (`"listVideos"`, `"getFrame"`, ...) get that error reply, or raise `exc`
  (for example `httpx.ConnectError`) as a broken connection would.
- `fake.drop_after(events, operation="streamFrames", times=1)`: the next stream breaks after that many events.
- `fake.inject(event, data, operation="streamFrames", after=0, times=1)`: a `gap`, `reset`, `clock` or `worker` event
  after `after` events of the next stream.
- `delay=`: an async hook awaited before every streamed event, `delay(operation, index)`, to order races in a test.

```python
async def test_a_server_that_fails() -> None:
    fake = FakeFactory(recording=RECORDING)
    fake.fail("listVideos", status=503, code="busy", times=4)  # more failures than the client retries
    async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep, retries=3) as factory:
        with pytest.raises(FactoryError) as error:
            await factory.sources()
    assert error.value.code == "busy"
    assert fake.time.slept  # the retries waited, on the fake clock
```

## Time

`fake.time` is a `FakeTime`: a wall clock and a monotonic clock that move only when slept on. `sleep(s)` advances
both by `s` at once and lets other tasks run. Its `slept` list records every sleep asked for. Unless you give the fake
your own `time=FakeTime(wall=...)`, its wall clock starts where the recording's first clock line has the shared
playhead at the recording's first frame (at that line's time when it is paused), so the state agrees with the frames
it serves: `session.clock().media_time_now()` starts at the first frame's media time (at the line's `anchor_media`
instead when that line is paused before the first frame, as in `dry2-0582-0600`, or when the first frame comes during
its hold, before the anchor, as in `dry2-pause-seek`), and the `media_time` the fake sends is the playhead on its clock.
A recording without clock lines (and without a recorded `getSession` state) plays from its first frame at once, at rate
0.8, without a select's hold. Give it to the client (`sleep=fake.time.sleep`, and `monotonic=` and `wall_clock=` when
your code reads them), and retries, backfill and pacing run without waiting. With `realtime=True` the fake paces its
streams at the frames' media times, on the same fake clock.

## Code that makes its own client

When the code under test creates its own `FactoryClient` or `Replay`, as an application or an example script does,
wrap it in `use_fake(fake)`. Inside the block, every client and replay made without a transport uses the fake and its
time. The tests of this repository run every program in `examples/` this way.

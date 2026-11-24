# Streams

`factory.streams` is the middle level: the session's frames as typed items, with the stream's bookkeeping done for
you. It has three streams:

| | Operation | For |
|---|---|---|
| `frames()` | `GET /session/frames/stream` (server-sent events) | frames and side events; what the interpreter uses |
| `poll()` | `GET /session/frames` (long-poll) | the same where a connection cannot stay open |
| `raw_packets()` | `GET /raw/stream` | the model's raw packets (`sip1`) |

Every stream is an `AsyncStream`, an async iterator and an async context manager. Leaving the `async with` block (by
`break` too), `aclose()` or cancelling the task closes its HTTP connection; a stream abandoned otherwise is closed only
when it is garbage-collected.

## Frames and side events

```python
async with factory.streams.frames(["world"], backfill=True, clock_on_connect=True) as stream:
    async for item in stream:
        match item:
            case FrameItem(frame=frame, is_revision=revised, is_backfill=late):
                world = "no world" if frame.world is None else f"{len(frame.world.stones)} stones"
                note = " (revised)" if revised else " (late world read back)" if late else ""
                print(f"frame g {frame.g} at {clock(frame.t)}: {world}{note}")
                seen += 1
                if seen == frames:
                    break
            case ClockItem(state=state):
                print(f"clock: session {state.session} shows {state.video}, rate {state.rate}")
            case WorkerItem(worker=worker):
                print(f"worker: {worker.state} {worker.reason}".rstrip())
            case GapItem(count=count):
                print(f"gap: the server skipped {count} frames")
            case ResetItem(boot=boot):
                print(f"reset: the server restarted (boot {boot})")
```

The items (`StreamItem`):

| Item | Event | |
|---|---|---|
| `FrameItem(frame, id, is_revision, is_backfill)` | `frame` | a `Frame` with the layers asked for; `id` is `<boot>-<cursor>` |
| `ClockItem(state, server_now)` | `clock` | the shared state changed: play, pause, seek, select, rate; or the state read after connecting (`clock_on_connect=True`, no `id`) |
| `WorkerItem(worker)` | `worker` | the worker's status (`state`, `reason`, fps, timings) changed |
| `GapItem(count)` | `gap` | the unit skipped `count` items because this consumer fell behind |
| `ResetItem(boot, cursor)` | `reset` | the unit restarted; the stream goes on from its newest frame |

Events of a type the SDK does not know are skipped, so a newer unit does not break an older SDK.

The unit sends a `clock` event only when the shared state changes, and none when a stream connects. With
`clock_on_connect=True` the SDK reads the state (`GET /session`) after every connect and reconnect and passes it on
first, as a `ClockItem`, so a consumer always knows the state its frames belong to (the interpreter does this for its
wall times). When the state cannot be read, the stream goes on and logs a warning.

`frames()` takes `layers` (default: the unit's `overlay,scene,cards,world`), `after` (start after this cursor; default:
the newest frames), `observe`, `precision`, `resume`, `backfill`, `clock_on_connect`, `limit`, `max_s`,
`last_event_id` and `max_reconnects`.

## Resume

With `resume=True` (the default) a stream never ends while the unit runs. `limit` and `max_s` bound each connection:
the SDK asks the unit for them and also counts the frames and times the seconds itself (with the client's `monotonic`
clock), and closes the connection when either is reached, because the 20.20 unit sends nothing more by then but closes
a finished stream only about 10 s later. A connection that brought events and ended (the unit ended it, or it reached
its `limit` or `max_s`) is reopened at once with `Last-Event-ID` set to the last event id, and the unit continues after
that frame. `after` is sent until the stream has an event id, so a first connection that ends before its first event
does not lose the start you asked for. A connection that failed, broke, or ended without an event is reopened after the
backoff (0.25 s doubling to 5 s, full jitter), and every such wait is logged as a warning. A `reset` (the unit
restarted) is passed on and the stream continues from the newest frame. `max_reconnects` gives up with `stream_closed`
after that many reconnects in a row that brought no event. With `resume=False` the stream ends when its connection
ends: the unit ended it, it reached its `limit` or `max_s`, or it broke (`stream_closed`).

A reply that does not decode (a bad `Content-Encoding`) raises `decode` and is not resumed; neither is an error reply
other than a retryable one (429, 502, 503, 504). An event that does not decode (its data is not JSON, `NaN` and
`Infinity` included, or not the shape of its type: a required field missing or null, a field of the wrong type) is
skipped with a warning in the log, and the stream goes on (its id still counts when the stream reconnects).
`interpreter.wait_for()` waits on past it. The other languages check the same fields at the top of each event (SDK-DESIGN.md
2.3 rule 9); Python's models also check the fields further inside, and skip an event when one of those is missing or of
the wrong type. `fixtures/damaged` holds an event of each kind the rule names; `tests/test_damaged.py` streams them.

## Logging

The SDK logs to the `pdigger_factory_sdk` logger and installs no handler: a warning for every backoff wait before a
reconnect, for a frame released without its late layers and for an event skipped because it does not decode, info for
a `reset`, debug for retries and for reconnects
after a normal end. Without logging set up, Python prints the warnings to stderr, so a wrong URL does not fail
silently. To see everything ([`examples/reconnects.py`](../examples/reconnects.py), which also gives up after three
failed reconnects in a row with `factory.interpreter(max_reconnects=3)`):

```python
def show_the_sdk_log() -> None:
    handler = logging.StreamHandler()  # stderr
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    sdk = logging.getLogger("pdigger_factory_sdk")
    sdk.addHandler(handler)
    sdk.setLevel(logging.DEBUG)
```

## Revisions and duplicates

While the session is paused, the unit refines the paused frame's world and releases the frame again with a higher
`world.rev`. The stream passes it on with `is_revision=True`. A frame that arrives again for the same session and `g`
with the same or a lower `rev` (after a reconnect, for example) is dropped. Otherwise frames come in cursor order.

## Late layers (backfill)

A frame is released at most 300 ms after its packet. A layer that is not ready by then is listed in `frame.missing`,
and the unit never sends that frame again. On 20.20, on build `claude-20261114-int17`, the world missed the deadline in
3 to 4 % of the frames of dry1 and dry2 (never on aug28 and jul30, the 1088 profile), and every such world was there
when read 1.5 s later. On build `claude-20261122-hr4x1` it missed it in 507 of dry1's 9,042 frames (5.6 %), and every
one was read back (dry2, aug28 and jul30 of 22 November were replayed on a Mac, so they say nothing about the unit).
With `backfill=True` (the interpreter's default; `frames()` defaults to `False`):

1. a frame with missing layers is held back, and so is every frame after it;
2. 1.5 s after it arrived, the SDK reads the missing layers with `GET /session/frames/{g}?layers=...&session=...` (and
   the stream's `precision`), up to 3 times, 1 s apart;
3. it merges them in (they leave `missing`), marks the frame `is_backfill=True` and releases the held frames in
   order. A frame that cannot be completed (not retained, session gone, tries used up) is released with the layers that
   did come merged in (`is_backfill=True` when one did; the others stay in `missing`), with a warning in the log.

Side events are never held. So the interpreter sees every frame in order, at the cost of up to about 4 s of latency
when a world is late.

## Long-poll

`poll()` loops `GET /session/frames`, continuing from each reply's `cursor` and `state.revision`, and yields the same
items: a `ClockItem` when the state's revision changed, a `WorkerItem` when the worker's state or reason changed, then
the frames. Like `frames()` it counts as a viewer unless `observe` is true (default: the client's). The unit counts a
long-poll as a viewer only when it carries a client id (`cid`, 8 to 40 letters, digits, `-` or `_`), so the SDK sends a
random one, the same for the whole stream; pass your own `cid` to choose it.

```python
async with factory.streams.poll(["world"], wait=1000) as polls:
    async for item in polls:
        if isinstance(item, FrameItem):
            print(f"polled frame g {item.frame.g} (cursor {item.frame.cursor})")
```

## Raw packets

`raw_packets()` streams the model's raw packets (`Sip1Packet`: maps, boxes, the person and its joints, stones, tray,
hands) over NDJSON or SSE, with the same resume rules. Over NDJSON, which has no event ids, a reconnect asks for
`after` = the cursor of the last packet.

```python
async with factory.streams.raw_packets(format="ndjson") as stream:
    async for item in stream:
        if isinstance(item, PacketItem):
            packet = item.packet
            print(f"packet g {packet.g}: {packet.stones.count} stones, person {packet.person.present}")
```

The packed buffers inside a packet (maps, masks, depth) are decoded with `codec`; see [low-level.md](low-level.md#codec).

## Below the streams

`factory.api.stream_frames()` and `factory.api.raw_stream()` open one connection and return an `EventStream` of raw
`StreamMessage`s (`event`, `data`, `id`, `json()`), with no resume, typing, backfill or counting of `limit` and
`max_s` (the unit's own end of a stream applies, about 10 s after it reached either). Close it with `aclose()` or
`async with`, also when you never iterate it: that closes the connection. The SSE parser follows the specification:
`event`, `data` (several lines joined), `id`, `retry`, comments, any line ending, and a BOM at the start.

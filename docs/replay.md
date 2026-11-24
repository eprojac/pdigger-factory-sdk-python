# Replay

Use `Replay.open` to read a local file or a recording URL. You can repeat a shift, inspect a stone’s history and
compare the summary against the published result. A local replay runs offline.

```python
import sys

from pdigger_factory_sdk import InterpreterCore, Replay

DRY1 = "https://factory.pdigger.com/developers/data/dry1.world.ndjson.gz"


def main(source: str = DRY1) -> None:
    core = InterpreterCore()  # one pass: the events as they are read, then the summary of the same pass
    for event in Replay.open(source).events(core=core):
        print(event.describe())  # 5:02.8 Right hand places stone 643
    print(core.summary().describe(), end="")


if __name__ == "__main__":
    main(*sys.argv[1:2])
```

`Replay.open(source)` takes a local path or an `http(s)://` URL, of `.ndjson` or `.ndjson.gz` (a file of several gzip
members too). A URL is streamed and never loaded whole. Options: `transport` (an httpx transport, as tests pass a
`FakeFactory`), `headers`, and `sleep` and `monotonic` for the pacing.

## Iterating

| Sync | Async | |
|---|---|---|
| `frames()` | `aframes()` | every frame, in file order, as recorded |
| `updates()` | `aupdates()` | the interpreter's update of every frame, then a final one with what was still open |
| `events()` | `aevents()` | the events |
| `activities()` | `aactivities()` | the activities; the ones still open at the end come as `interrupted` |
| `tray_facts()` | `atray_facts()` | the tray facts ([interpreter](interpreter.md#trays-and-tray-stones)) |
| `trays()` | `atrays()` | every tray of the recording, with its arrival and leaving points |
| `summary()` | `asummary()` | the summary of the whole recording |
| `interpret()` | | one pass: header, events, activities, tray facts, trays and summary together |
| `header` | `aheader()` | the recording's header line (`RecordingHeader`), read from the first line on first use |

Each iteration reads the file again from its start, with a fresh `InterpreterCore` unless you pass one: `updates()`,
`events()`, `activities()` and `tray_facts()` (and their async variants) take `core=`, and after the pass `core.summary()` is the
summary of the recording, as in the quickstart above. `frames()` gives the frames only, without the side lines (the
clock, and the `gap` and `reset` lines whose dropped frames end the trays' presence questions), so to feed a core of
your own, pass it to `updates(core=...)` rather than feeding it `frames()`. `interpret()` gets everything in one pass:

```python
result = Replay.open(source).interpret()
header = result.header
if header is not None:
    print(f"{header.name}: {header.video}, frames g {header.g_first} to {header.g_last}, build {header.build}")

moves = [a for a in result.activities if isinstance(a, Move)]
print(f"{len(result.events)} events, {len(result.activities)} activities, {len(moves)} moves")
for move in moves:
    print(f"{move.outcome}: {move.describe()}")
```

The sync iterators suit scripts and notebooks. The async ones never block the event loop: a URL is read with the async
client, a local file is read and decompressed in a worker thread, and the loop gets a turn after every frame. They can
also pace the frames at their media times, divided by `speed`, to replay a recording as the unit played it:

```python
replay = Replay.open(source)
async with replay.aactivities(pace="realtime", speed=speed) as activities:
    async for activity in activities:
        print(activity.describe())
```

The clock lines of a recording give the events and activities their `wall` times, as on the unit.

## Trays

`trays()` reads the full recording and returns its trays in order. Each tray includes its stones, facts and the
points when it settled and left. Use these points to seek to the same tray in a player.
[Tray identity and events](interpreter.md#trays-and-tray-stones).

```python
trays = await Replay.open(source).atrays()
for tray in trays:
    window = tray.window()
    if window is None:
        continue  # a stop that never settled
    arrived, leaving = window
    print(f"tray {tray.n}: arrived {clock(arrived)}, leaving {clock(leaving)}")
    for stone in tray.stones:
        if stone.status == "removed" and stone.certain:
            print(f"  removed: {stone.id}")
```

`window()` is the media span to replay or to show. To show a point on the unit, seek the shared session there; a seek
changes what every viewer sees, so the example sends dry runs
([`examples/tray_points.py`](../examples/tray_points.py)):

```python
for point in (tray.arrived, tray.leaving):
    if point is not None:
        would = await factory.session.seek(point.t, video=tray.video, dry_run=True)
        print(f"seek to {point.clock} would show {would.video} from {clock(would.anchor_media)}")
```

A recording that cannot be read raises a `FactoryError`: `transport` when a URL cannot be read (connection refused,
reset, timed out), `decode` for gzip data that is corrupt or ends early (a truncated file or download), and the API's
codes, with the status, for an HTTP error reply. Data without the gzip header is read as plain NDJSON. Bytes that are
not UTF-8 read as U+FFFD, and lines a reader does not understand are skipped (SDK-DESIGN.md section 6): blank lines,
lines that are not JSON (`NaN` and `Infinity` are not, nor is JSON nested deeper than Python reads), JSON that is not an
object, frame and side lines that do not decode (a `g` that is not a number, a clock line whose `recv` is not a number,
say), and any other line. The header is the first line that is not blank, when it has `recording` and decodes by its
table (a `source` that is no catalog entry is left out of it, the other members are kept as they are); otherwise the
recording has none (a header that does not decode is skipped, and a header line further down is a line like any other).
So a file that is no recording at all replays as nothing (no frames, no error). A local file that cannot be opened
raises the usual `OSError` (`FileNotFoundError`). `fixtures/damaged` holds a line of each kind; `tests/test_damaged.py`
replays them.

## The published recordings

https://factory.pdigger.com/developers/data/ has four complete recordings of build `claude-20261122-hr4x1`: every
frame the server interpreted, from the start of each video to its end. The 20.20 unit recorded dry1; aug28, jul30
and dry2 are the same model's outputs, computed on an RTX 4090 and streamed through the same interpreter on a Mac
(each header's `recorded_from` says how its recording was made).

| Video | Length | Source |
|---|---|---|
| `aug28` | 1:01:51.8 | 60 fps, a 1088 x 1026 crop of 1920 x 1080 |
| `jul30` | 1:18:38.1 | 60 fps, the same crop |
| `dry1` | 30:07.2 | 10 fps, full frame |
| `dry2` | 29:52.2 | 10 fps, full frame |

Each comes as `<video>.world.ndjson.gz` (the world and cards layers, all the interpreter needs) and
`<video>.full.ndjson.gz` (all five layers), with `<video>.summary.json` and `.summary.txt`, the maintainers' reference
interpreter's summary of the world file. `recordings.json` lists the files with their sizes and sha256. Compare your
replay with the published summary:

```python
if published is not None:
    expected = Summary.from_dict(json.loads(Path(published).read_text(encoding="utf-8")))
    print("the summary matches" if result.summary == expected else "the summary differs")
```

The SDK's opt-in recording tests do exactly this for a downloaded folder ([TESTING.md](../TESTING.md)). The fixtures
in `fixtures/recordings/` are short excerpts in the same format, and their golden outputs are in `fixtures/golden/`.

## The format (`pfr1`)

A recording is NDJSON, optionally gzip. Line 1 is the header: `{"recording": "pfr1", "video": ..., "source": {the
catalog entry}, "build": ..., "model": ..., "layers": [...], "redacted": [...], "recorded_from": ..., "recorded_at":
..., "frames": N, "g_first": ..., "g_last": ..., "sessions": [...], "notes": ...}`. Every other line is either a frame
exactly as the API sent it (it has `cursor` and `g`; layers that were backfilled are merged in, and `missing` lists only
those still missing), or a side event of the stream, in the order received:
`{"sse": "clock" | "worker" | "gap" | "reset", "data": {...}, "recv": epoch}`. Readers skip lines they do not
understand.

The public recordings are redacted: face keypoints (joints 0 to 4 and 69) are null, appearance masks are removed
(`raw.maps.human`, `raw.person.mask`, `raw.person.alpha`, `overlay.regions.human`, `overlay.mask.label`,
`overlay.mask.alpha`), and so are depth textures (`raw.depth.q`, `scene.surface.q`). The header's `redacted` lists
them; the models make those fields optional, and the codec returns `None` for an absent map.

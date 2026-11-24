# High level: what the hands did

The interpreter turns the unit's frames into what a person would say happened at the work area: events
(`1:19.2 Left hand lifts stone 155`), activities (a hand moving one stone, or a burst of pushes), the state of both
hands, the stones in view, and the trays that pass through. It is the same interpreter for a recording replayed
offline and for a unit followed live, and all four languages give the same sentences for the same frames.

This tutorial replays a recording first, so it needs no unit, and then follows one. Its code is
[examples/tutorial_high_level.py](../../examples/tutorial_high_level.py) and, for the live steps, the examples they
name.

## 1. Replay a recording: the activities

Install the SDK (see [the tutorials' start](README.md#before-you-start)) and take a recording: the fixture
`fixtures/recordings/dry1-0018-0153.ndjson.gz` is dry1 from 0:18 to 2:33, where a tray arrives with 42 stones, the
hands take some off and push the others into place. Any published recording works the same, as a path or a URL.

```python
def activities(source: str) -> None:
    """Step 1: what the hands did, one sentence per activity (a move, or a burst of pushes)."""
    for activity in Replay.open(source).activities():
        print(activity.describe())  # 1:19.2 Left hand takes stone 155 away (1.0 s)
```

`Replay.open` streams the file (gzip or not); nothing is held in memory but the interpreter's state. The first
activities of dry1-0018-0153:

```text
0:20.5 Both hands push stones 86, 90 and 94 (6 pushes in 0.6 s)
0:20.3 Right hand pushes stones 89, 90, 94, 95 and 109 (13 pushes in 1.6 s)
0:21.9 Left hand pushes stone 86 (1 push)
```

Run it yourself with `python examples/tutorial_high_level.py fixtures/recordings/dry1-0018-0153.ndjson.gz`.

## 2. The same activities as data

A sentence is for people; your program reads the fields. A `Move` is one stone moved by one hand, with where it went
(`outcome`: `placed`, `dropped`, `carried_away`, `removed`, or `interrupted` when the recording or the session ended first);
the other activity is a `PushBurst`.

```python
def moves(source: str) -> None:
    """Step 2: the same activities as data: each move's stone, hand, outcome and length."""
    for activity in Replay.open(source).activities():
        if isinstance(activity, Move):  # a hand moving one stone; the other activities are bursts of pushes
            took = f"{activity.duration:.1f} s" if activity.duration is not None else "still open"
            at = clock(activity.start.t)
            print(f"{at} stone {activity.stone_id}: {activity.hand} hand, {activity.outcome}, {took}")
```

In dry1-0018-0153 that is nine moves: stone 155 carried away by the left hand at 1:19.2 (in 1.0 s) and stone 106 by
the right hand at 1:25.6 (in 2.6 s), five placed (stone 102 twice) and two dropped.

## 3. Trays, and the stones taken off them

The world's stone ids belong to the engine's tracker, which renames stones and reuses ids. For trays the SDK keeps
ids of its own, one per stone and never reused, so that "this stone was removed" stays true over time. Its tray facts
say what happened to each tray and its stones:

```python
def tray_facts(source: str) -> None:
    """Step 3: the trays, as facts about the SDK's own stone ids (one per stone, never reused)."""
    for fact in Replay.open(source).tray_facts():
        print(fact.describe())  # 0:22.5 tray 1 arrived: 42 stones
```

The first removals from dry1-0018-0153's tray:

```text
0:45.5 tray 1: stone 20261122T023847.0Z-1-117 was removed
0:45.5 tray 1: stone 20261122T023847.0Z-1-127 was removed
```

and its last fact, as it moves out: `2:24.0 tray 1 leaving: 44 stones (30 sure), 0 added, 6 removed, 18 unclear`.

A tray also has two points to jump to: when it had arrived and settled, and its last calm frame before it moved out.
`window()` gives both, and each tray stone says whether it was removed, and whether that is certain:

```python
def trays(source: str) -> None:
    """Step 4: each tray's arrival and leaving points, and the stones removed from it for certain."""
    for tray in Replay.open(source).trays():
        window = tray.window()
        if window is None:
            continue  # a stop that never settled
        arrived, leaving = window
        gone = [stone.id for stone in tray.stones if stone.status == "removed" and stone.certain]
        print(f"tray {tray.n}: {clock(arrived)} to {clock(leaving)}, {len(gone)} stones removed for certain")
```

For dry1-0018-0153: tray 1 from 0:22.5 to 2:24.0, with 6 stones removed for certain.

## 4. One pass, with a core of your own

Each loop above is a pass of its own over the file. To read several things from one pass, give the replay an
`InterpreterCore` and ask it afterwards: its summary, its trays, or the path of any stone of the world.

```python
def summary_and_track(source: str, stone: int) -> None:
    """Step 5: one pass with a core of your own: its summary, and the path of one stone of the world."""
    core = InterpreterCore()
    for _ in Replay.open(source).updates(core=core):
        pass  # every update of the pass: the events, activities and tray facts of each frame
    print(core.summary().describe(), end="")
    path = core.track(stone)  # where the stone was, frame by frame (scene units)
    if path:
        print(f"stone {stone}: {len(path)} points from {clock(path[0].t)} to {clock(path[-1].t)}")
```

The summary of dry1-0018-0153 begins:

```text
frames: 676 (with world 676, without 0, revisions 0)
media: 2:14.6, sessions 1, gaps 0
stones: last 42, max 49, confirmed 42
moves: 9 (placed 5, dropped 2, carried away 2, removed 0, interrupted 0, partial 0, carried 6), mean 1.4 s, 0.18 units
```

and stone 155's path has 5 points, from 1:15.6 to 1:19.8.

## 5. Follow a unit, live

The same interpreter follows the unit's shared session, whatever it shows: a recording it plays, or a live camera.
[examples/quickstart_live.py](../../examples/quickstart_live.py) prints every event as it happens:

```python
async with FactoryClient() as factory:  # PDIGGER_FACTORY_URL, else http://factory-orin.local:8120
    async for event in factory.interpreter().events():
        print(event.describe())  # 21:17.8 Right hand lifts stone 2660
```

The stream resumes by itself when the connection drops, holds a frame whose world came late until it is read back,
and gives every event its wall time from the session's clock. To wait for something first, and then follow the
activities, the tray facts and the hands, [examples/high_level_interpreter.py](../../examples/high_level_interpreter.py)
does this:

```python
interpreter = factory.interpreter()
lift = await interpreter.wait_for("lift", timeout=600)
print(f"First lift: {lift.describe()}")

async with interpreter.updates() as updates:  # goes on right after that lift
    async for update in updates:
        for activity in update.activities:
            kind = "move" if isinstance(activity, Move) else "push"
            print(f"[{kind}] {activity.describe()}")
        for fact in update.tray_facts:  # the SDK's own stone ids: one per stone, never reused
            print(f"[tray] {fact.describe()}")
        if update.state.frames >= frames:
            break
```

After the loop, `interpreter.state` still holds the hands, the stones in view and the moves still open, and the
summary covers every frame followed:

```python
left, right = interpreter.state.hands.left, interpreter.state.hands.right
print(f"Left hand: {left.card or left.state}; right hand: {right.card or right.state}")
print(interpreter.summary().describe(), end="")
```

## 6. Show a tray on the shared screen

Every viewer of the unit sees the same session. To show a tray there, seek to one of its points.
[examples/tray_points.py](../../examples/tray_points.py) asks the unit with dry runs, which it checks and does not
apply; leave out `dry_run=True` to really move the session for everybody.

```python
tray = next(t for t in trays if t.n == show)
async with FactoryClient() as factory:
    for point in (tray.arrived, tray.leaving):
        if point is not None:
            would = await factory.session.seek(point.t, video=tray.video, dry_run=True)
            print(f"seek to {point.clock} would show {would.video} from {clock(would.anchor_media)}")
```

## 7. Test your program without a unit

`FakeFactory` answers the whole API from a recording, with no server and no real clock, so a test of your own code is
a few lines ([examples/testing_with_fake.py](../../examples/testing_with_fake.py)):

```python
async def test_count_moves() -> None:
    fake = FakeFactory(recording=RECORDING)
    async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep) as factory:
        assert await count_moves(factory, limit=44) == {"carried_away": 2, "dropped": 1, "placed": 4}
```

## Next

- [Low level](02-low-level.md): the operations under the interpreter, one call each.
- [Live cameras](03-live-cameras.md): select a plant camera and follow it.
- The reference: [interpreter.md](../interpreter.md) (every event, activity, tray fact and the summary),
  [replay.md](../replay.md) (paced replays, the recording format) and [testing.md](../testing.md).

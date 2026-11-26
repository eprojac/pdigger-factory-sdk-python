# The Interpreter

Use the interpreter to follow a hand movement, trace a stone or explain a tray report. Events describe what the unit
saw in a frame. Activities group events into moves and pushes. A recording produces the same results as the live
frame sequence. [Interpreter rules](../spec/SDK-DESIGN.md#4-interpreter-rules-golden-tested).

## Following the session

```python
interpreter = factory.interpreter()
lift = await interpreter.wait_for("lift", timeout=600)
print(f"First lift: {lift.describe()}")
```

`factory.interpreter()` takes:

| Option | Default | |
|---|---|---|
| `layers` | `("world", "cards")` | `world` drives it; `cards` adds the titles of the hand cards |
| `observe` | the client's (`False`) | `False` counts as a viewer and keeps the worker interpreting ([concepts](concepts.md#viewers-and-observe)) |
| `backfill` | `True` | hold a frame whose world came late until it is read back ([streams](streams.md#late-layers-backfill)) |
| `resume` | `True` | follow the session endlessly, reconnecting with `Last-Event-ID` |
| `max_reconnects` | `None` (never give up) | raise `stream_closed` after this many failed reconnects in a row |
| `precision` | `"rounded"` | or `"full"` |
| `core` | a new `InterpreterCore` | continue from another one's state |

After every connect and reconnect the interpreter reads the shared state (`GET /session`), because the unit sends a
`clock` event only when the state changes, never on connect; the state gives every event its wall time. Reconnects,
backoff waits and frames released without their late world are logged to the `pdigger_factory_sdk` logger
([streams](streams.md#logging)).

It offers four async streams (`updates()`, `events()`, `activities()`, `tray_facts()`), `wait_for(match, timeout)` and
`trays()`. You iterate one at a time, and a later loop goes on where the last one stopped, missing nothing in between: it first yields the
rest of the frame the last loop stopped in (what that loop did not yield; after `wait_for`, the events after the match
and all of that frame's activities and tray facts), then the frames after it. `wait_for` above returns at the
first lift, and the loop below goes on right after it. Leave a loop early inside `async with` (as below) or with
`aclose()`: Python closes an abandoned `async for` only later, and the next loop would find the interpreter busy.

```python
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

An `Update` has the `frame`, its new `events`, the `activities` it completed, the `tray_facts` it made, the `state`
after it, and `is_revision`. Its `lines()` method gives all of it as text in output order. When a stream ends (with
`resume=False`), the last update has `frame=None` and `final=True`. It carries the activities that were still open,
closed as `interrupted`, and the facts of the open tray leaving. `interpreter.end()` makes the same final update when
you stop following yourself, and `finish()` returns just its activities. A stream that fails raises its error, with no
final update. `wait_for` raises `TimeoutError` after `timeout` seconds, and `FactoryError` (`stream_closed`)
if the stream ends first.

## Events

An `Event` has `type` (the wire name), `g`, `t`, `wall` (server wall time, from the shared state), `session`,
`video`, `stone_id`, `hand` (`left`, `right`, `both` or `None`), `why`, `into`, `source`, `stone` (the stone's
snapshot in that frame's world), and `tray_stone`: the id of the [tray stone](#trays-and-tray-stones) that world id
stood for before this frame (also for an event of a revision), or `None`. `stone_id` is the world's id, which the engine's tracker can rename and reuse. `describe()` gives the sentence; `{Hand}` is `Left hand`, `Right hand`, `Both hands`,
or `A hand` when the event names no side, and the verb agrees with it.

| `type` | Sentence |
|---|---|
| `new` | `stone 12 appears`; when the image found it: `stone 12 appears (found in the image)` |
| `confirm` | `stone 12 is confirmed` |
| `freeze` | `stone 12 gets its final shape` |
| `push` | `Right hand pushes stone 12`; a row a hand pushed: `stone 12 is pushed along its row`; the engine: `stone 12 is reseated`; no side: `stone 12 is pushed` |
| `lift` | `Right hand lifts stone 12` |
| `hinge` | `Right hand tips stone 12 up on its edge` |
| `carry` | `Right hand carries stone 12` |
| `place` | `Right hand places stone 12` |
| `drop` | `Right hand drops stone 12` |
| `gone` | `Right hand takes stone 12 out of view` |
| `delete` | `stone 12 is removed`, with the reason: `(no longer seen)`, `(overlapped another stone)`, `(outside the board)`, `(merged with a neighbour)`, `(its place is empty)`, `(a hand emptied its place)`, `(carried away)` |
| `split` | `stone 12 splits into 40 and 41`; into one stone: `stone 12 is trimmed` |
| anything newer | `stone 12: <type>` |

The clock in front is the media time: `M:SS.s` (`21:17.8`), or `H:MM:SS.s` from one hour. On a live camera, media
time counts from the start of the session, so use `wall` for timestamps.

## Activities

**A move** (`Move`) is one stone taken and put somewhere. It opens on a `lift` or a `hinge`. A `carry` marks it
`carried` and a `hinge` marks it `tipped`. It closes with an `outcome`:

| `outcome` | closed by | sentence |
|---|---|---|
| `placed` | `place` | `21:17.8 Right hand moves stone 2660 0.84 units in 4.0 s` (carried), `0:36.3 Right hand tips stone 117 and sets it down (0.2 s)` (tipped), `Right hand lifts stone 7 and sets it down (0.4 s)` |
| `dropped` | `drop` | `0:50.5 Left hand drops stone 128 after 0.2 s` |
| `carried_away` | `gone` | `1:19.2 Left hand takes stone 155 away (1.0 s)` |
| `removed` | `delete` of the stone | `Left hand lifts stone 7; it is removed after 1.2 s` |
| `interrupted` | a new session, or the end | `1:19.2 Left hand is still holding stone 155 (0.6 s so far)` |

A move has `stone_id`, `hand` (it becomes `both` when the other hand takes part), `start` and `end` (a `FrameRef` with
`g`, `t` and `wall`), `duration`, `from_` and `to` (the stone's position at the lift and at the put-down), and
`distance` between them in x and y. The distance is in **scene units**, the template gauge of the work area, not
metres. A put-down with no lift before it (the stream joined mid-move) makes a `partial` move, printed with
`(joined mid-move)` at its end: `1:20.2 Left hand takes stone 155 away (0.0 s) (joined mid-move)`.

**A push burst** (`PushBurst`) is one hand pushing stones about: the `push` events of one side (`left`, `right` or
`both`), each at most 1.0 s after the one before. Pushes along a row (`why: course`, which name no side) and by the engine
(`why: reseat`) do not count. It has `stones` (ascending, each once), `pushes`, `start`, `last`
and `end`: `0:20.5 Both hands push stones 86, 90 and 94 (6 pushes in 0.6 s)`. Beyond six stones the list is
shortened: `1:06.4 Left hand pushes stones 98, 102, 106, 108, 115, 116 and 5 more (37 pushes in 5.8 s)`.

## Trays and tray stones

The tracker assigns a **world ID** to a detection. When a hand pushes a row of similar stones, the tracker can
assign a new ID to a familiar stone or keep an ID at a position now occupied by a different stone. Use the **tray
stone ID** to follow one physical stone across frames. The SDK never reuses it.

For example, `20261122T023847.0Z-1-117` identifies a stone first seen at 02:38:47 UTC on tray 1 under world ID 117.
Without a wall clock the prefix is `s{session}t{media time}`. If two stones receive the same world ID on the same
tray within the same tenth of a second, the later IDs end in `-2`, `-3` and so on.

The SDK tracks two kinds of uncertainty. **Sure** describes a stone’s identity. **Certain** describes a fact, such
as a removal. The SDK keeps every fact it has emitted. When the departure speckle check resolves an earlier `unsure`
fact, it adds an `identified` fact to the history.

### When the SDK records a change

- **Arrival and departure.** A tray must stop for at least two seconds. Arrival is the first calm frame with all
  stones confirmed and no new stone for five frames. A calm frame requires both hands to be free for three
  consecutive frames, with no hand event or stone creation, deletion or split. Departure uses the last calm frame
  unless a hand worked after it, in which case it uses the last frame.
- **Identity.** A push, an overlap, a missing detection or a similar stone near a held stone can put an identity in
  doubt. A stone missing at two calm moments may regain its identity if it reappears in place and nothing could have
  swapped it. The SDK also checks tracking gaps, hand activity, nearby stones and appearance. A stone with an
  appearance history follows the separate matching rules.
- **Removal and addition.** A certain removal requires recent hand contact. For a stone that disappears on the tray,
  the SDK also checks that a calm frame shows its position empty and that no tracked stone could be the missing
  stone. An addition is certain when a new stone stays in place across two calm moments and no similar-sized stone
  is missing or uncertain.
- **Matching at departure.** The SDK links a missing stone to a nearby detection when the two match by speckle or
  appearance, or when no hand could have swapped them. Otherwise it records the possible removal and addition as
  unclear.

The [tray rules](../spec/SDK-DESIGN.md#461-the-tray-rules) give the exact conditions, distance limits and
exceptions.

Some of the facts of `dry1-0018-0153`, a busy tray of dry1, from 0:38.7 to 0:59.4 (`interpreter.tray_facts()`,
`describe()`):

```text
0:38.7 tray 1: stone 20261122T023847.0Z-1-105 is not sure any more (a stone next to it was pushed)
0:38.7 tray 1: stone 20261122T023847.0Z-1-126 is not sure any more (a stone next to it was pushed)
0:45.5 tray 1: stone 20261122T023847.0Z-1-97 is not sure any more (it was pushed)
0:45.5 tray 1: stone 20261122T023847.0Z-1-117 was removed
0:45.5 tray 1: stone 20261122T023847.0Z-1-127 was removed
0:59.4 tray 1: stone 20261122T023847.0Z-1-126 was removed
```

and its end, where the speckle check has settled all but one of the stones the worker pushed, and the SDK reports
the unresolved additions:

```text
2:24.0 tray 1: stone 20261122T024126.9Z-1-284 not recognised (unclear)
2:24.0 tray 1 leaving: 44 stones (30 sure), 0 added, 6 removed, 18 unclear
```

| Fact (`kind`) | Sentence |
|---|---|
| `arrived` | `{clock} tray {n} arrived: {stones} stones` |
| `unsure` | `{clock} tray {n}: stone {id} is not sure any more ({why})` |
| `removed`, certain | `{clock} tray {n}: stone {id} was removed`, with ` (carried away)` when a hand took it out of view |
| `removed`, unclear | `{clock} tray {n}: stone {id} not found (unclear)` |
| `added`, certain | `{clock} tray {n}: stone {id} was set down` |
| `added`, unclear | `{clock} tray {n}: stone {id} not recognised (unclear)` |
| `identified` | `{clock} tray {n}: stone {id} confirmed by its speckle on world stone {world_id} ({score})`; `recognised` or `found again` in place of `confirmed` when the stone was on another world stone or lost; `recognised by its look` for a look's link |
| `leaving` | `{clock} tray {n} leaving: {stones} stones ({sure} sure), {added} added, {removed} removed, {unclear} unclear` |

A `TrayFact` has `kind`, `tray`, `session`, `video`, `stone` (the tray stone's id), `world_id` (the world id it
stands for now), `certain`, `why`, `g`, `t`, `wall`, and the counts of `arrived` and `leaving`. `interpreter.trays()`
lists the trays so far (those that have left, the newest 256, and the open one) and `Replay.trays()` every tray of a
recording. A `Tray` has `session`, `video`, `n`, the points `arrived` and `leaving` (`TrayPoint`: `g`, `t`, `wall`),
`stones` (`TrayStone`: `id`, `world_id`, `status` `on`, `lost` or `removed`, `sure`, `added`, `certain`, `born`,
`place`, `size`), its `facts`, `window()` (the media span from arrival to leaving) and `stone(id)`. The two points are
the ones to jump between: on the shared session, `factory.session.seek(point.t, video=tray.video)` shows one
([replay](replay.md#trays)).

### Report fields

Call `tray.report()` after a tray leaves. Use `interpreter.tray_reports()` for a live stream of reports or
`replay.tray_reports()` for a recording.

| Fields | Meaning |
| --- | --- |
| `session`, `video`, `tray` | Source session, video and tray number. |
| `arrived`, `left`, `stones` | Arrival and departure points, and the number of stones at arrival. |
| `removed`, `added`, `moved` | Changes in each category. Each carries a stone ID, time, certainty, reason code, position, size, distance and confidence as applicable. |
| `unclear`, `unsure`, `complete` | Counts of unclear changes and other retained stones with uncertain identities. A report is complete when both counts are zero. |

A move requires a known stone identity, evidence that the stone was touched, and a detection at departure. After
compensating for the motion of the tray itself, the displacement must exceed 0.75 times the stone’s shorter side.
Use `report.to_dict()` for the report data.

An `added` change with `certain=False` records an occupant of unknown origin. It can be a renamed arrival or a
stone set down by a worker. Only `certain=True` records a confirmed addition. If a previously retained stone is
positively detected on the quiet frame immediately after the leaving point while other IDs blink, its actual sight
can support uncertain presence. Its `left_place` stays `None` when the leaving frame did not detect it.

`Tray.open` is the open account of the tray's leaving (`TrayOpen`, SDK-DESIGN 4.6.8: for each stone left open, the
readings the evidence still allows), and `TrayFact.code` gives each fact's reason as a code (`carried_away`,
`pushed`, `not_recognised`, ...). A tray stone also has `first_place`, `left_place` and `touched`, which the report
reads, and `question` while it is held in question; with the stones' looks, a fact may be `identified` and a tray stone
carries `looks`, `by_look`, `look_lost` and `open`; with the unit's speckle check, `speckle`, `by_speckle` and
`evidence` (below).

### Confidence

Confidence estimates how often a reported change of this kind is correct. The maintainers checked 150 trays across
four recordings against the video. Each table entry uses `(right + 1) / (checked + 2)` for that group of changes.
[Calibration method](../spec/SDK-DESIGN.md#4613-the-leavings-speckle-answer-and-the-confidence-of-a-change).

For a removal, the evidence says whether the surface pattern of the stone was found at **departure**. For an
addition, it says whether that stone was already visible at **arrival**.

| Change | Certain | not found | undecided | found |
|---|---|---|---|---|
| removed | yes | 0.98 | 0.50 | 0.25 |
| removed | no | 0.71 | 0.58 | 0.09 |
| added | yes or no | 0.96 | 0.52 | 0.02 |

A move is `MOVED_CONFIDENCE`, 0.87. The numbers are constants (`CONFIDENCE`), the same in every language, so a client
keeps the changes at or above a threshold of its own: at 0.90, dry1's tray 2 (the fixture's tray 1) keeps its six
certain removals and nine of its thirteen additions, all fifteen right by the camera check.

### The unit's speckle check

The unit compares the grain on each stone’s surface at arrival with its appearance in a later image. Since build
`claude-20261123-speckle3` it sends each GPU comparison once in `world.speckle`. The published recordings include
comparisons computed after capture. [Camera measurements](BEHAVIOUR-20.20.md#8-the-speckle-answers).

The SDK uses the last calm frame to reconcile identities at departure. Clean matches can confirm a stone in place or
recover it under a new world ID. If a hand worked after the last calm frame, the departure comparison is unavailable
and the report uses undecided evidence.

Reading the answers yourself is advanced mode: to look into an unclear change, to check a tray by hand, or for rules of
your own. The interpreter reads each answer as a tray's (`TraySpeckle`): `update.speckle` holds the answers a frame
gave, `interpreter.speckle()` the answers so far (the newest 1024) and `Replay.interpret().speckle` every answer of a
recording, and while the tray is on, each of its stones in the answer takes its part as `speckle` (`StoneSpeckle`). As
the tray leaves, `TrayStone.by_speckle` says the leaving's answer paired the stone, and `TrayStone.evidence` what it
found of it at the other moment (`found`, `not_found` or `undecided`: a look-alike, or a stone it did not check). An
answer at another moment, or a score below the clean bar, is for you to weigh: the tray rules use only the leaving's
clean finds. The last answer for the first tray of the fixture recording `dry1-0018-0153` (tray 2 of the published
dry1), as `describe_speckle_txt()` writes it, begins:

```text
732 2:26.0 tray 1: speckle of 0:22.5 and 2:24.2: 42 stones, 30 clean, 26 paired, 24 the same
  20261122T023847.0Z-1-84 arrival 0.632 0.112 clean found 20261122T023847.0Z-1-84 pair 20261122T023847.0Z-1-84
```

and, further down:

```text
  20261122T023847.0Z-1-104 arrival 0.481 0.090 clean found 20261122T023847.0Z-1-104 pair 20261122T023847.0Z-1-104
  20261122T023847.0Z-1-105 arrival 0.102 0.079 found world 193
  20261122T023847.0Z-1-106 arrival 0.706 0.102 clean found world 266
  20261122T023847.0Z-1-107 arrival 0.720 0.115 clean found 20261122T023847.0Z-1-106 pair 20261122T023847.0Z-1-106
```

Stones 84, in question since 2:18.8, and 104, pushed at 2:09.6, are the same stones at the leaving, so they stay sure;
105, removed at 1:05.4, is found nowhere clean; 107, which the world had lost, is found and paired on world stone 191,
where the SDK had linked tray stone 106, and 106 on world stone 266, which no tray stone stood for. As the tray leaves,
the SDK links them so: 107 found again on 191, 106 recognised on 266 (a camera check of this tray: 39 of its stones
right, none wrong). A `StoneSpeckle` has `stone`, `side` (`"arrival"`: the stone as it was at the arrival, checked at the later
moment; `"later"`: the other way), `score` and `second` (None: not checked), `clean` (a score of at least 0.300 and at
least twice the second), `found` (the tray stone at the other moment that its best place lies on, None when none;
`found_world` the world stone), `pair` (the tray stone the answer pairs it with, or None) and `same` (paired with
itself). A `TraySpeckle` has `tray`, `session`, `video`, `g`, `t` and `wall` (the frame it came in), `unit_tray` (the
answer's own tray number, the unit's count), `ref` and `at` (the two moments: `g` and `t`), `device`, `gpu_ms`,
`wall_ms`, `stones`, `stone(id)`, `describe()` and `to_dict()`.

### Reading the dry1 report

The fixture `dry1-0018-0153` contains tray 2 of the full dry1 recording. The excerpt numbers it tray 1. A worker
straightened the rows and several stones briefly lost their tracker IDs. The SDK counted 42 stones at arrival and 44
at departure, matching the video count at departure.

| Change | What the report recorded | What the maintainers found in the video |
| --- | --- | --- |
| Removed | Six certain and five unclear | All six certain removals were correct. Of the five unclear removals, 87 and 129 left. Stones 121, 128 and 130 stayed. |
| Added | Thirteen unclear | Ten were set down and three (151, 246 and 249) arrived on the tray. At a 0.90 threshold, nine additions and six removals remain. All fifteen were correct. The filter also drops a genuine addition (278). |
| Moved | Eight | The SDK measured displacements of 0.33 to 0.43 scene units after accounting for the tray’s own movement. Each move has a confidence of 0.87. |

The unit’s departure comparison confirmed eight uncertain identities, found four missing stones and matched two to
different world IDs. Stone 98 remained uncertain. In the report, `18 unclear` counts changes. The separate
`1 not sure` counts that remaining identity.

A removal gives the last observed position. An addition or move gives the departure position when observed. For a
stone the world model retained without a detection at departure, the report can use its position in that model.
Stone 281 is an example. The worker set it down about six seconds before departure.

### Limits of the camera evidence

A complete report accounts for the stones visible to the model. If the model detects a stone in an empty slot at
arrival, a later occupant can inherit that ID. If the model loses sight of a stone permanently, the SDK can report a
removal even though the stone stayed. In a separate video check, three of 52 certain removals across the four
recordings were stones a hand had moved on the tray.

At departure the SDK excludes an unseen stone that is held, or whose box overlaps a free stone’s box by at least 30%
of its own area. On a busy tray, similar stones can also remain unresolved if hands worked after the final calm
frame. For the evidence behind any individual change, inspect its tray facts and the departure comparison.

## Output order

Within a frame: activities that the frame closed before its events (a new session; push bursts more than 1.0 s old),
then the frame's events in world order, then the activities its events closed, then the tray facts it made (a new
session's first frame first has the facts of the old session's tray leaving). Activities closed together are sorted by
end frame, start frame, moves before pushes, stone id, and hand. That is why a push burst that started earlier can be
printed after a later one: it ended later.

## State

`interpreter.state` (an `InterpreterState`) is the view after the newest frame: `session`, `video`, `g`, `t`, `wall`,
`stones` (id to the stone's view in the newest world), `hands.left` and `hands.right` (a `HandView` with `state`:
`free`, `touch`, `grasp`, `hinge` or `carry`; the `stone` held; the `card` title), `board`, `stats`, `open_moves`,
`open_bursts`, `tray` (the open tray with its tray stones, or `None`), and the counters `frames`, `revisions` and
`gaps`.

```python
left, right = interpreter.state.hands.left, interpreter.state.hands.right
print(f"Left hand: {left.card or left.state}; right hand: {right.card or right.state}")
print(interpreter.summary().describe(), end="")
```

## Summary

`interpreter.summary()` (so far) and `Replay.summary()` (a whole recording) return a `Summary`. `to_dict()` is its JSON
form, the one the golden files and the published `<video>.summary.json` hold. `Summary.from_dict()` reads it back, and
`describe()` prints it. This is the summary of `dry1-0000-0130`, the first 130 s of dry1:

```text
frames: 632 (with world 632, without 0, revisions 0)
media: 2:09.6, sessions 1, gaps 1
stones: last 40, max 49, confirmed 40
moves: 9 (placed 5, dropped 2, carried away 2, removed 0, interrupted 0, partial 0, carried 6), mean 1.4 s, 0.18 units
push bursts: 51 (left 25, right 16, both 10), pushes 456
events: new 276, confirm 119, freeze 99, push 533, lift 9, hinge 4, carry 6, place 5, drop 2, gone 2, delete 76, split 3, other 0
trays: 2 (arrived 2), stones at arrival 83, added 0 (+14 unclear), removed 6 (+9 unclear), not sure 37
left hand: free 0:53.7, touch 0:52.1, grasp 0:17.1, hinge 0:00.4, carry 0:02.4
right hand: free 1:07.7, touch 0:09.4, grasp 0:38.6, hinge 0:00.6, carry 0:09.6
```

- `frames` counts frames fed (a revision counts in `revisions` instead); `missing_world` counts frames whose world was
  missing (after backfill, for a stream).
- `gaps` counts the times `g` jumped by more than one within a session (frames the unit dropped, a paused camera).
- `media` is the media time covered, summed over sessions.
- `stones`: the number in the last world, the most in any frame, and the last `stats.confirmed`.
- `trays`: trays opened and arrived, tray stones at the arrivals, certain and unclear additions and removals, and tray
  stones that stopped being sure.
- The hand times add up each frame with a world in which the hand was in that state by its step: the media time
  since the session's previous frame when that frame is the grid step before it, and after a gap the previous step
  (0.2 s on a recorded video, 0.199 s on dry1 and dry2, about 0.3 s on a plant camera, whose worker releases about
  three frames a second; SDK-DESIGN.md 4.5).
- `mean` is the mean duration of the moves that are not partial or interrupted, and the mean distance of the moves
  that have one.

## The core

All of the rules live in `InterpreterCore`, a synchronous state machine with no I/O: `feed(frame)` returns an
`Update`, `clock(state)` gives it the clock for wall times, `gap()` tells it that the stream dropped frames (a `gap`
or `reset` event), `end()` closes what is still open (the final update; `finish()` gives its activities), and `state`,
`summary()` and `trays()` read it. The interpreter feeds it from the frames stream and `Replay` from a file. You can feed it from
anywhere, for example from the long-poll:

```python
core = InterpreterCore()
async with (
    FactoryClient() as factory,
    factory.streams.poll(["world", "cards"], cid="example-core-01", backfill=True) as polls,
):
    async for item in polls:
        if isinstance(item, ClockItem):
            core.clock(item.state)  # gives the events their wall time
        elif isinstance(item, FrameItem):
            for line in core.feed(item.frame).lines():
                print(line)
            if core.state.frames >= frames:
                break
```

The frame handling, in order: a frame of a new session resets the state and closes what was open. A frame that is not
newer than the newest is a revision (a higher `world.rev`) or a duplicate (dropped). A revision's events are emitted
only once per frame. A new frame's events are emitted, fed to the activities, and its world replaces the state. A
frame without a world changes only `g`, `t` and the cards. The full rules are in
[spec/SDK-DESIGN.md](../spec/SDK-DESIGN.md#4-interpreter-rules-golden-tested), section 4.

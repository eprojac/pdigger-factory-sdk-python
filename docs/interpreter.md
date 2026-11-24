# The Interpreter

The interpreter turns frames into what happened: typed **events** (a stone appears, a hand lifts it, places it), the
**activities** they add up to (a move of one stone, a hand pushing stones about), the **trays** with stone ids of the
SDK's own, never reused, and facts that are never revised, the state of the hands and stones, a **summary**, and a
sentence for each of these. The same rules run in all four languages, and the same recording gives the same text in all of
them, byte for byte.

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

The world's stone ids are the engine tracker's. It renames a stone it lost and found again, and it can keep an id on
another stone: a stone set down where a removed one lay, a neighbour pushed into its place, a row of equal stones pushed
one place along while the ids stay on the places. For anything that must hold over time ("stone X was removed"), use the
**tray stones**: the interpreter follows each tray from the moment it has arrived and settled to the moment it is about
to move out, and gives every stone on it an id that is never reused, no other tray stone is ever given it:
`{first seen}-{tray}-{world id}`, for example `20261122T023847.0Z-1-117` (the server's wall time in UTC when the stone
was first seen on tray 1, under world id 117; without a clock `s{session}t{media time}-{tray}-{world id}`; a second
stone first seen on the tray in the same tenth of a second under the same world id takes `-2` at the end, a third
`-3`).

A tray stone says whether its identity is still **sure**, and a fact about it is **certain** or **unclear**. Tray facts
are final: a fact, once given, is never revised or withdrawn, and a stone that is not sure (fact `unsure`) is sure
again only through the unit's speckle check as its tray leaves, once, with an `identified` fact
([below](#the-units-speckle-check)). On a worked tray the world stone a tray stone stands for can turn out to be another stone; the rules mark a
stone not sure whenever what the world shows leaves another reading, but they do not tell which of the not-sure links
are wrong. The rules prefer saying "unclear" to guessing:

- A **tray** is a stop of at least 2 s. It has **arrived** at the first calm frame with every stone confirmed and no new
  stone for five frames (a calm frame: both hands free for three frames in a row, no hand event, no stone new, deleted
  or split). It is **leaving** just before it moves out: at its last calm frame, when no hand worked after it (else at
  its last frame).
- A stone stays **sure** while nothing could have swapped it: it stops being sure when it was pushed, when a stone of
  its size next to it was pushed, when its box overlaps another stone, when it is not seen at two calm moments, or when
  a stone of its size appeared next to it while a hand held it. A stone without a look chain that is not seen at two
  calm moments while nothing could have changed it since it was last seen is first held in question (not sure,
  `question` set, no fact yet): it is sure again once the world sees it again on its place, with no hand at work, no
  held stone, no stone a hand took away, no push on the tray and no frame the SDK could not read (a frame without a
  world, a jump in `g`, a `gap` or `reset` of the stream, a revision, the moving frames of a tray that goes on) from its
  last sight until then, no hand on it or near it, no other stone of its size within 0.12 of its place, and no look of
  its world stone that matches another stone in question or lost; otherwise it stops being sure. When the question is
  settled on a calm moment that does not show the stone (at once at its doubt, or by a sight before that calm moment),
  the next calm moment that shows it, or the tray's last frame when the tray leaves first and that frame shows it, must
  find nothing at work since either. A stone with a look chain is never held in question: its doubt is a question only
  its looks settle. When the world renames a stone, the tray stone follows only if exactly one new world stone fits,
  close by and within 3 s.
- A **removal** is certain when the stone's place is empty at a calm moment, as far as the world shows, and no world
  stone could be it. An **addition** is certain when a new stone stays at one place over two calm moments while no stone
  of its size is lost or not sure. When the tray leaves, a lost stone is matched to a new world stone near it only
  with evidence that they are the same stone (the unit's speckle check pairs them, their looks agree, or no hand worked
  near either since the stone was last seen); everything else is reported as unclear, so a stone's id never passes to
  a stone that may be another.

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

A tray's report is the SDK's simple mode (SDK-DESIGN 4.7): `tray.report()` once the tray has left,
`interpreter.tray_reports()` from a unit as each tray leaves, and `Replay.tray_reports()` for every tray of a recording.
A `TrayReport` has the tray's `session`, `video` and number (`tray`), its points `arrived` and `left`, the `stones` it
arrived with, its changes `removed`, `added` and `moved` (`TrayChange`: `kind`, `stone`, `at`, `certain`, `code`,
`place`, `size`, `distance`, `confidence`), the counts `unclear` and `unsure` (the stones still on that are not sure), and `complete`:
no unclear change and no stone that is not sure. `describe()` writes it as text and `to_dict()` as data. A stone is
reported moved only when the SDK is sure it is the same stone, saw it at the leaving point and something touched it on
the tray, and it moved more than `MOVED` (0.75) times its short side once the tray's own shift is taken out.

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

Every change of a report has a `confidence`, the probability that it is right: the share of changes of its kind that
were right on 150 trays of the four complete recordings checked against their video (the maintainers' offline checks,
not published: each stone followed by its look both ways, and a camera check of the certain removals), as
`(right + 1) / (checked + 2)` (SDK-DESIGN 4.6.13). Its kind is the change, whether it is certain, and what the unit's
speckle check found of the stone at the other moment (`TrayStone.evidence`): for a removal, whether the stone is on the
tray as it left; for an addition, whether it was on the tray as it arrived.

| Change | Certain | not found | undecided | found |
|---|---|---|---|---|
| removed | yes | 0.98 | 0.50 | 0.25 |
| removed | no | 0.71 | 0.58 | 0.09 |
| added | yes or no | 0.96 | 0.52 | 0.02 |

A move is `MOVED_CONFIDENCE`, 0.87. The numbers are constants (`CONFIDENCE`), the same in every language, so a client
keeps the changes at or above a threshold of its own: at 0.90, dry1's tray 2 (the fixture's tray 1) keeps its six
certain removals and nine of its thirteen additions, all fifteen right by the camera check.

### The unit's speckle check

Every report uses it; nothing needs to be called. Since build `claude-20261123-speckle3` the unit compares each tray's
stones between its arrival and a later moment by their speckle (the grain of each stone's face), on its GPU, and sends
each answer once, in the world (`world.speckle`; [how the unit does it](BEHAVIOUR-20.20.md#8-the-speckle-answers)); the
four published recordings carry answers made afterwards. The answer for the tray's last calm frame is the leaving's
(SDK-DESIGN 4.6.13): as the tray leaves, the stones it pairs are sure, confirmed on their world stone or linked again
where the world had lost them or put them on other world stones, and each change takes its confidence from what it
found. A tray whose hands worked after its last calm frame has no leaving answer: its report is made without one, its
changes `undecided`.

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

What the SDK can not see, since it sees only the world: a box the model sees on an empty slot when the tray arrives
becomes a tray stone, and a stone later set into that slot keeps its id; and a stone the model stops seeing for good
while it stays on the tray looks removed: unclear when the tray leaves, or for certain when a hand touched it before it
was lost and no world stone could be it (3 of the 52 certain removals in a camera check of the four complete recordings
were such stones, each one a stone a hand had moved). On busy trays, where hands push rows of equal stones, the
speckle check as the tray leaves settles most pushed stones; where hands worked after the tray's last calm frame there
is no leaving answer, and most pushed stones end up not sure. Stones set down are mostly unclear: their confidence
says how far to rely on each.

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
left hand: free 0:54.0, touch 0:52.4, grasp 0:17.2, hinge 0:00.4, carry 0:02.4
right hand: free 1:08.0, touch 0:09.4, grasp 0:38.8, hinge 0:00.6, carry 0:09.6
```

- `frames` counts frames fed (a revision counts in `revisions` instead); `missing_world` counts frames whose world was
  missing (after backfill, for a stream).
- `gaps` counts the times `g` jumped by more than one within a session (frames the unit dropped, a paused camera).
- `media` is the media time covered, summed over sessions.
- `stones`: the number in the last world, the most in any frame, and the last `stats.confirmed`.
- `trays`: trays opened and arrived, tray stones at the arrivals, certain and unclear additions and removals, and tray
  stones that stopped being sure.
- The hand times are 0.2 s for each frame with a world in which the hand was in that state.
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

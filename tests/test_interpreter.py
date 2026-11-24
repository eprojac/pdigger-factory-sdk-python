"""The Interpreter's rules (SDK-DESIGN.md 4.2-4.4) one by one, and its async API over the fake server."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from pdigger_factory_sdk import (
    Event,
    FactoryClient,
    FactoryError,
    InterpreterCore,
    InterpreterState,
    Move,
    PushBurst,
    State,
    Summary,
    TrackPoint,
    Update,
)
from pdigger_factory_sdk import interpreter as interpreter_module
from pdigger_factory_sdk import streams as streams_module
from pdigger_factory_sdk.interpreter import FrameRef, _sort_key
from pdigger_factory_sdk.models import Frame
from pdigger_factory_sdk.testing import FakeExhausted, FakeFactory

from .support import client, gated_client, golden, recording, stream_requests, text_of
from .synth import HEADER, cards, clock_line, ev, frame, stone, worker_line

LIFT = "5:01.6 Right hand lifts stone 643"


def run(*frames: dict[str, Any], core: InterpreterCore | None = None) -> tuple[InterpreterCore, list[Update]]:
    core = core or InterpreterCore()
    return core, [core.feed(Frame.from_json(f)) for f in frames]


def acts(updates: list[Update], core: InterpreterCore | None = None) -> list[str]:
    out = [a.describe() for u in updates for a in u.activities]
    if core is not None:
        out += [a.describe() for a in core.finish()]
    return out


def test_a_carried_move() -> None:
    core, ups = run(
        frame(10, stones=[stone(7, 0.0, 0.0)], events=[ev("lift", 7, "left")], left="grasp", left_stone=7),
        frame(11, stones=[stone(7, 0.1, 0.0)], events=[ev("carry", 7, "left")], left="carry", left_stone=7),
        frame(12, stones=[stone(7, 0.3, 0.4)], events=[ev("place", 7, "left")]),
    )
    assert [e.describe() for u in ups for e in u.events] == [
        "0:02.0 Left hand lifts stone 7",
        "0:02.2 Left hand carries stone 7",
        "0:02.4 Left hand places stone 7",
    ]
    (move,) = ups[2].activities
    assert isinstance(move, Move)
    assert (move.outcome, move.carried, move.distance, move.from_, move.to) == (
        "placed",
        True,
        0.5,
        [0.0, 0.0, 0.046],
        [0.3, 0.4, 0.046],
    )
    assert move.describe() == "0:02.0 Left hand moves stone 7 0.50 units in 0.4 s"
    assert ups[0].events[0].stone is not None
    s = core.summary()
    assert (s.moves.total, s.moves.placed, s.moves.carried, s.moves.mean_duration, s.moves.mean_distance) == (
        1,
        1,
        1,
        0.4,
        0.5,
    )
    assert s.hands["left"]["grasp"] == 0.2
    assert s.hands["left"]["carry"] == 0.2
    assert s.hands["left"]["free"] == 0.2


def test_hinge_lift_and_hand_merging() -> None:
    _, ups = run(
        frame(1, stones=[stone(3)], events=[ev("hinge", 3, "right")]),
        frame(2, stones=[stone(3)], events=[ev("lift", 3, "right"), ev("hinge", 3, "left")]),
        frame(3, stones=[stone(3)], events=[ev("carry", 3, None), ev("drop", 3, None)]),
    )
    (move,) = ups[2].activities
    assert isinstance(move, Move)
    assert (move.hand, move.tipped, move.carried, move.outcome) == ("both", True, True, "dropped")
    assert move.describe() == "0:00.2 Both hands drop stone 3 after 0.4 s"


def test_a_side_joins_a_move_that_had_none() -> None:
    _, ups = run(
        frame(1, stones=[stone(3)], events=[ev("lift", 3)]),
        frame(2, stones=[stone(3)], events=[ev("carry", 3, "left"), ev("place", 3, "left")]),
    )
    (move,) = ups[1].activities
    assert isinstance(move, Move)
    assert move.hand == "left"


def test_outcomes_and_skipped_deletes() -> None:
    core, ups = run(
        frame(1, stones=[stone(1), stone(2), stone(3)], events=[ev("lift", 1, "left"), ev("lift", 2, "right")]),
        frame(2, stones=[stone(3)], events=[ev("gone", 1, "left"), ev("delete", 1, why="carried-away")]),
        frame(3, stones=[stone(3)], events=[ev("delete", 2, why="absent"), ev("delete", 9), ev("carry", 8, "left")]),
        frame(4, stones=[stone(3)], events=[ev("lift", 3, "left"), ev("freeze", 3), ev("teleport", 3)]),
    )
    assert acts(ups, core) == [
        "0:00.2 Left hand takes stone 1 away (0.2 s)",
        "0:00.2 Right hand lifts stone 2; it is removed after 0.4 s",
        "0:00.8 Left hand is still holding stone 3 (0.0 s so far)",
    ]
    s = core.summary()
    assert s.events["other"] == 1
    assert s.events["delete"] == 3
    assert (s.moves.carried_away, s.moves.removed, s.moves.interrupted) == (1, 1, 1)
    assert s.moves.mean_distance is None
    assert core.finish() == []


def test_partial_moves() -> None:
    core, ups = run(frame(50, stones=[stone(4)], events=[ev("place", 4, "right")]))
    (move,) = ups[0].activities
    assert isinstance(move, Move)
    assert move.partial
    assert move.describe() == "0:10.0 Right hand lifts stone 4 and sets it down (0.0 s) (joined mid-move)"
    s = core.summary()
    assert (s.moves.partial, s.moves.mean_duration) == (1, None)


def test_push_bursts() -> None:
    core, ups = run(
        frame(0, events=[ev("push", 5, "left"), ev("push", 6, "right")]),
        frame(2, events=[ev("push", 5, "left"), ev("push", None, "left"), ev("push", 9, "left", why="course")]),
        frame(6, events=[ev("push", 3, "left")]),
        frame(12, events=[ev("push", 3, "left")]),
        frame(13, with_world=False),
        frame(20, events=[ev("push", 1, "both"), ev("push", 2, None), ev("push", 2, "odd")]),
    )
    out = acts(ups, core)
    assert out == [
        "0:00.0 Right hand pushes stone 6 (1 push)",
        "0:00.0 Left hand pushes stones 3 and 5 (4 pushes in 1.2 s)",
        "0:02.4 Left hand pushes stone 3 (1 push)",
        "0:04.0 Both hands push stone 1 (1 push)",
    ]
    assert ups[2].events[0].describe() == "0:01.2 Left hand pushes stone 3"
    assert ups[3].before == 1  # the stale right burst closed before the frame's events... and the left one in _push
    s = core.summary()
    assert (s.push_bursts.total, s.push_bursts.pushes, s.push_bursts.left, s.push_bursts.right) == (4, 7, 2, 1)
    assert s.push_bursts.both == 1


def test_session_change_interrupts_and_counts_media() -> None:
    core, ups = run(
        frame(100, session=1, stones=[stone(1)], events=[ev("lift", 1, "left"), ev("push", 2, "right")]),
        frame(101, session=1, stones=[stone(1)]),
        frame(102, session=1, with_world=False),
        frame(5, session=2, stones=[stone(9)], events=[ev("new", 9)]),
    )
    change = ups[3]
    assert change.before == 2
    assert [a.describe() for a in change.activities] == [
        "0:20.0 Right hand pushes stone 2 (1 push)",  # ends at g 100, the move at g 101: by end g first
        "0:20.0 Left hand is still holding stone 1 (0.2 s so far)",
    ]
    assert change.lines()[2] == "0:01.0 stone 9 appears"
    s = core.summary()
    assert (s.sessions, s.media_seconds, s.frames, s.missing_world, s.first_g, s.last_g) == (2, 0.4, 4, 1, 100, 5)


def test_revisions_duplicates_and_gaps() -> None:
    core, ups = run(
        frame(1, stones=[stone(1)], events=[ev("new", 1)]),
        frame(3, stones=[stone(1)]),
        frame(1, rev=1, stones=[stone(1), stone(2)], events=[ev("new", 1), ev("new", 2), ev("lift", 2, "left")]),
        frame(1, rev=1, events=[ev("new", 3)]),
        frame(2, with_world=False),
        frame(3, rev=2, stones=[stone(1), stone(2)], events=[ev("place", 2, "left")]),
    )
    revised = ups[2]
    assert revised.is_revision
    assert [e.describe() for e in revised.events] == ["0:00.2 stone 2 appears", "0:00.2 Left hand lifts stone 2"]
    assert len(core.state.stones) == 2  # the newest frame's revision takes the state
    assert ups[3].dropped
    assert ups[4].dropped
    assert [a.describe() for a in ups[5].activities] == ["0:00.2 Left hand lifts stone 2 and sets it down (0.4 s)"]
    s = core.summary()
    assert (s.revisions, s.gaps, s.frames) == (2, 1, 2)


def test_a_revision_of_an_older_frame_keeps_the_newest_state() -> None:
    core, _ = run(frame(1, stones=[stone(1)]), frame(2, stones=[stone(1)]), frame(1, rev=1, stones=[]))
    assert list(core.state.stones) == [1]


def test_revision_window_is_bounded() -> None:
    _, ups = run(
        *[frame(g) for g in range(10)], frame(1, rev=1, events=[ev("new", 1)]), core=InterpreterCore(revision_window=2)
    )
    assert ups[-1].dropped  # its revision bookkeeping was pruned


def test_a_revision_inside_the_window_keeps_its_tray_stone() -> None:
    """2.4: the tray links are pruned with the revisions: the oldest frame kept still gives a revision the tray stone
    from before it, a frame older than that is a duplicate, and the links kept stay bounded."""
    window = 8
    on = [stone(1, -0.6), stone(2), stone(3, 0.6)]
    core, _ = run(*[frame(g, stones=on) for g in range(25)], core=InterpreterCore(revision_window=window))
    # the tray arrives at g4, taken at g10 (its visit), so g11 is the first frame its links come before; more than
    # 2 x 8 frames kept prune at g16 (keeping g8 on) and at g24 (keeping g16 on)
    assert core.state.tray is not None
    one = core.state.tray.stones[0]
    assert (one.id, one.world_id) == ("s1t0.8-1-1", 1)
    assert sorted(core._links) == list(range(16, 25))  # bounded like the revisions
    _, (edge, older) = run(
        frame(16, stones=on, rev=1, events=[ev("confirm", 1)]),
        frame(15, stones=on, rev=1, events=[ev("confirm", 1)]),
        core=core,
    )
    assert edge.is_revision
    assert [e.tray_stone for e in edge.events] == ["s1t0.8-1-1"]
    assert older.dropped
    assert older.events == []


def test_wall_times_from_the_clock() -> None:
    core = InterpreterCore()
    data = clock_line(session=1, rate=0.5, anchor_media=1.0, anchor_wall=500.0, hold_until=500.0)["data"]
    core.clock(State.from_json(data))
    _, ups = run(frame(10, events=[ev("new", 1)]), core=core)
    assert ups[0].events[0].wall == 502.0
    assert core.state.wall == 502.0
    _, ups = run(frame(11, session=2, events=[ev("new", 1)]), core=core)
    assert ups[0].events[0].wall is None
    core.clock(State.from_json({**data, "session": 2, "rate": 0.0}))
    _, ups = run(frame(12, session=2, events=[ev("new", 1)]), core=core)
    assert ups[0].events[0].wall is None


def test_an_older_clock_state_is_ignored() -> None:
    """A `GET /session` read after connecting can be newer than a clock event the stream sent before it."""
    core = InterpreterCore()
    data = clock_line(session=1, rate=0.5, anchor_media=1.0, anchor_wall=600.0, hold_until=600.0, revision=5)["data"]
    core.clock(State.from_json(data))
    core.clock(State.from_json({**data, "revision": 4, "anchor_wall": 500.0}))
    _, ups = run(frame(10, events=[ev("new", 1)]), core=core)
    assert ups[0].events[0].wall == 602.0
    core.clock(State.from_json({**data, "revision": 6, "anchor_wall": 700.0}))
    _, ups = run(frame(11, events=[ev("new", 2)]), core=core)
    assert ups[0].events[0].wall == 702.4


def test_the_clock_states_of_the_last_8_sessions_are_kept() -> None:
    core = InterpreterCore()
    data = clock_line(rate=1.0, anchor_wall=500.0, hold_until=500.0)["data"]
    for session in range(1, 11):
        core.clock(State.from_json({**data, "session": session}))
    _, ups = run(frame(0, session=1, events=[ev("new", 1)]), frame(1, session=10, events=[ev("new", 2)]), core=core)
    assert ups[0].events[0].wall is None  # session 1's state was forgotten
    assert ups[1].events[0].wall == pytest.approx(500.2)


def test_a_paused_clock_and_one_held_until_later() -> None:
    core = InterpreterCore()
    data = clock_line(session=1, rate=1.0, anchor_media=1.0, anchor_wall=500.0, hold_until=503.0)["data"]
    core.clock(State.from_json(data))
    _, ups = run(frame(10, events=[ev("new", 1)]), core=core)
    assert ups[0].events[0].wall == 504.0  # playing: the clock starts when the hold ends
    core.clock(State.from_json({**data, "revision": 2, "playing": False}))
    _, ups = run(frame(11, events=[ev("new", 2)]), core=core)
    assert ups[0].events[0].wall == pytest.approx(501.2)  # paused: from the anchor


def test_stone_tracks() -> None:
    def at(g: int, *xs: float, session: int = 1) -> dict[str, Any]:
        return frame(g, session=session, stones=[stone(i + 1, x) for i, x in enumerate(xs)])

    flat = {**stone(9), "solid": {**stone(9)["solid"], "pos": [1.0, 2.0]}}  # no z: no track point
    core, _ = run(at(0, 0.0, 5.0), at(1, 0.0, 5.0), at(2, 1.0, 5.0), frame(3, stones=[stone(1, 1.0), flat]))
    assert core.track(1) == [
        TrackPoint(g=0, t=0.0, pos=(0.0, 0.0, 0.046)),
        TrackPoint(g=2, t=0.4, pos=(1.0, 0.0, 0.046)),
    ]
    assert [p.g for p in core.track(2)] == [0]
    assert core.track(9) == []
    assert core.track(77) == []
    run(at(4, 2.0, session=2), core=core)
    assert core.track(2) == []  # a new session starts new tracks
    assert [p.g for p in core.track(1)] == [4]


def test_stone_tracks_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interpreter_module, "TRACK_POINTS", 2)
    monkeypatch.setattr(interpreter_module, "TRACK_STONES", 2)
    core, _ = run(*[frame(g, stones=[stone(1, float(g))]) for g in range(5)])
    assert [p.g for p in core.track(1)] == [3, 4]  # the newest points
    run(
        frame(5, stones=[stone(1, 4.0), stone(2, 0.0)]),
        frame(6, stones=[stone(1, 4.0), stone(2, 1.0), stone(3)]),
        core=core,
    )
    assert core.track(1) == []  # the least recently moved stone is forgotten
    assert [p.g for p in core.track(2)] == [5, 6]
    assert [p.g for p in core.track(3)] == [6]


def test_state_hands_cards_and_open_activities() -> None:
    core, _ = run(
        frame(
            1,
            stones=[stone(1)],
            events=[ev("lift", 1, "right"), ev("push", 1, "left")],
            right="grasp",
            right_stone=1,
            left="weird",
            cards=cards("Hand on a stone", "Holding a stone"),
        )
    )
    st = core.state
    assert (st.hands.right.state, st.hands.right.stone, st.hands.right.card) == ("grasp", 1, "Holding a stone")
    assert st.hands.left.card == "Hand on a stone"
    assert list(st.open_moves) == [1]
    assert list(st.open_bursts) == ["left"]
    assert core.summary().hands["left"]["free"] == 0.0  # an unknown hand state is not counted
    empty = InterpreterCore().state
    assert (empty.hands.left.state, empty.hands.left.card, empty.wall) == (None, None, None)


def test_cards_belong_to_their_session_and_come_from_new_frames() -> None:
    """A new session starts with no card titles until its own cards come; a revision never changes the cards."""
    _, ups = run(
        frame(1, session=1, stones=[stone(1)], cards=cards("Hand on a stone", "Holding a stone")),
        frame(0, session=2, stones=[stone(1)]),  # the next video's first frame, without a cards layer
        frame(0, session=2, rev=1, stones=[stone(1)], cards=cards("Free", "Pushing a stone")),
        frame(1, session=2, stones=[stone(1)], cards=cards("Free", "Pushing a stone")),
    )
    assert ups[0].state.hands.right.card == "Holding a stone"
    assert (ups[1].state.hands.left.card, ups[1].state.hands.right.card) == (None, None)
    assert ups[2].is_revision
    assert ups[2].state.hands.right.card is None
    assert ups[3].state.hands.right.card == "Pushing a stone"


def test_the_values_are_made_by_keyword() -> None:
    """Event, InterpreterState and Summary take their fields by keyword only, so a new field never shifts one."""
    with pytest.raises(TypeError):
        Event("lift", 1, 0.2, None, 1, "dry1")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Summary(1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        InterpreterState(1)  # type: ignore[call-arg]
    assert Event(type="lift", g=1, t=0.2, wall=None, session=1, video="dry1").tray_stone is None


def test_frames_without_hands() -> None:
    f = frame(1)
    f["world"]["hands"] = {}
    core, _ = run(f)
    assert core.state.hands.left.state is None


def test_sort_key_edge_cases() -> None:
    ref = FrameRef(g=1, t=0.2)
    m = Move(stone_id=None, hand=None, start=ref, session=1, video="dry1")
    b = PushBurst(hand="left", start=ref, last=ref, session=1, video="dry1")
    assert _sort_key(m) == (1, 1, 0, -1, "")
    assert _sort_key(b) == (1, 1, 1, -1, "left")
    assert ref.clock == "0:00.2"
    assert ref.to_dict() == {"g": 1, "t": 0.2, "wall": None}
    assert m.to_dict()["end"] is None
    assert b.to_dict()["end"] is None
    m.end = ref
    b.end = ref
    assert m.to_dict()["end"] == ref.to_dict()
    assert b.to_dict()["end"] == ref.to_dict()


# ---- the async Interpreter ---------------------------------------------------------------------------------------
async def test_events_activities_and_the_final_update() -> None:
    fake = FakeFactory(recording=recording("joined-mid-move"))
    async with client(fake) as factory:
        events = [e async for e in factory.interpreter(resume=False).events()]
        activities = [a async for a in factory.interpreter(resume=False).activities()]
        updates = [u async for u in factory.interpreter(resume=False).updates()]
    assert text_of(events) == golden("joined-mid-move", "events.txt")
    assert text_of(activities) == golden("joined-mid-move", "activities.txt")
    assert updates[-1].final
    assert updates[-1].frame is None
    assert all(e.wall is not None for e in events)  # from GET /session: the unit sends no clock event on connect


async def test_the_interpreter_goes_on_past_an_event_that_does_not_decode(caplog: pytest.LogCaptureFixture) -> None:
    """2.3 rule 9: an event whose data is not JSON is skipped, so a stream that ends after it still ends with the final
    update, and the next loop, which resumes after the newest frame, skips it again instead of stopping on it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(404, json={"error": "no session", "code": "not_found"})
        good = {g: json.dumps(frame(g)) for g in range(2)}
        sent = [("1", good[0]), ("2", "{nope")]
        if request.headers.get("Last-Event-ID") == "b-1":
            sent = [("2", "{nope"), ("3", good[1])]
        body = "".join(f"event: frame\nid: b-{i}\ndata: {data}\n\n" for i, data in sent)
        return httpx.Response(200, content=body.encode(), headers={"Content-Type": "text/event-stream"})

    async with FactoryClient("http://unit.test", transport=httpx.MockTransport(handler)) as factory:
        interpreter = factory.interpreter(resume=False)
        first = [u async for u in interpreter.updates()]
        second = [u async for u in interpreter.updates()]
    assert [(u.frame.g if u.frame else None, u.final) for u in first] == [(0, False), (None, True)]
    assert [(u.frame.g if u.frame else None, u.final) for u in second] == [(1, False), (None, True)]
    assert sum("skipped a frame event that does not decode" in r.getMessage() for r in caplog.records) == 2


async def test_wall_times_come_from_the_session_after_every_connect() -> None:
    lines = [
        HEADER,
        clock_line(rate=1.0, anchor_wall=500.0, hold_until=500.0),
        *[frame(g, events=[ev("new", g)]) for g in range(4)],
    ]
    fake = FakeFactory(recording=lines)
    fake.drop_after(2)
    events: list[Event] = []
    async with client(fake) as factory:
        with pytest.raises(FakeExhausted):
            async for event in factory.interpreter().events():
                events.append(event)
    assert [e.wall for e in events] == pytest.approx([500.0, 500.2, 500.4, 500.6])
    paths = [r.url.path.rsplit("/", 1)[-1] for r in fake.requests]
    assert paths == ["stream", "session", "stream", "session", "stream"]


async def test_without_the_session_wall_times_wait_for_a_clock_event(caplog: pytest.LogCaptureFixture) -> None:
    lines = [
        HEADER,
        clock_line(rate=1.0, anchor_wall=500.0),
        frame(0, events=[ev("new", 0)]),
        clock_line(rate=1.0, anchor_wall=700.0, hold_until=700.0, revision=2),
        frame(1, events=[ev("new", 1)]),
    ]
    fake = FakeFactory(recording=lines)
    fake.fail("getSession", status=404, code="not_found")
    async with client(fake) as factory:
        events = [e async for e in factory.interpreter(resume=False).events()]
    assert [e.wall for e in events] == [None, pytest.approx(700.2)]
    assert "could not read the shared state after connecting" in caplog.text


async def test_the_state_read_after_a_connect_is_bounded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(streams_module, "CLOCK_READ_TIMEOUT", 0.01)
    fake = FakeFactory(recording=[HEADER, frame(0, events=[ev("new", 0)])])
    async with gated_client(fake, "/api/v2/session", asyncio.Event()) as factory:
        events = [e async for e in factory.interpreter(resume=False).events()]
    assert [e.wall for e in events] == [None]
    assert "no reply within 0.01 s" in caplog.text


async def test_the_interpreter_gives_up_after_max_reconnects() -> None:
    fake = FakeFactory(recording=[frame(0)])
    fake.fail("streamFrames", status=503, code="busy", times=10)
    async with client(fake, retries=0) as factory:
        with pytest.raises(FactoryError, match="gave up after 1 reconnects") as info:
            async for _ in factory.interpreter(max_reconnects=1).events():
                pass
    assert info.value.code == "stream_closed"
    assert len(fake.requests) == 2


async def test_one_consumer_at_a_time_and_resume_after_a_break() -> None:
    fake = FakeFactory(recording=recording("joined-mid-move"))
    async with client(fake) as factory:
        interpreter = factory.interpreter()
        first = interpreter.updates()
        got = [await first.__anext__() for _ in range(3)]
        with pytest.raises(RuntimeError, match="already being iterated"):
            await interpreter.updates().__anext__()
        await first.aclose()
        stopped_at = interpreter.last_event_id
        assert stopped_at is not None
        rest: list[Update] = []
        with pytest.raises(FakeExhausted):
            async for update in interpreter.updates():
                rest.append(update)
    assert stream_requests(fake)[1].headers["Last-Event-ID"] == stopped_at
    assert [u.frame.g for u in got + rest if u.frame] == sorted({u.frame.g for u in got + rest if u.frame})
    assert interpreter.summary().frames == 22
    assert interpreter.state.frames == 22
    assert isinstance(interpreter.finish(), list)


async def test_wait_for() -> None:
    fake = FakeFactory(recording=recording("dry1-0000-0130"))
    async with client(fake) as factory:
        lift = await factory.interpreter().wait_for("lift")
        assert lift.type == "lift"
        placed = await factory.interpreter().wait_for(lambda e: e.type == "place" and e.hand == "right", timeout=30)
        assert placed.hand == "right"
        with pytest.raises(FactoryError, match="stream_closed"):
            await factory.interpreter(resume=False).wait_for("teleport")


async def test_a_later_loop_goes_on_where_the_last_one_stopped() -> None:
    """The rest of the frame the last loop stopped in first (what it did not yield), then the frames after it."""
    events = golden("dry1-0296-0306-full", "events.txt").splitlines()
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"))
    async with client(fake) as factory:
        interpreter = factory.interpreter(resume=False)
        lift = await interpreter.wait_for("lift")
        assert lift.describe() == LIFT
        rest = [e.describe() async for e in interpreter.events()]
        track = interpreter.track(643)  # lifted, carried and placed: it moved
        assert len(track) > 1
        assert [p.g for p in track] == sorted({p.g for p in track})
        whole = [a.describe() async for a in factory.interpreter(resume=False).activities()]
        split = factory.interpreter(resume=False)
        async with split.activities() as first:
            one = [(await first.__anext__()).describe()]
        later = [a.describe() async for a in split.activities()]
    assert rest == events[events.index(LIFT) + 1 :]
    assert len(whole) > 1
    assert one + later == whole


async def test_a_later_loop_prints_the_rest_of_a_frame_in_order() -> None:
    """A frame closes a stale push burst before its events and ends a move with its place event: an activities loop
    that stops after the burst leaves the rest, which an updates loop prints place first, then the move."""
    fake = FakeFactory(
        recording=[
            HEADER,
            frame(0, stones=[stone(5), stone(7)], events=[ev("push", 5, "right")]),
            frame(1, stones=[stone(5), stone(7)], events=[ev("lift", 7, "left")], left="grasp", left_stone=7),
            frame(10, stones=[stone(5), stone(7, 0.3, 0.4)], events=[ev("place", 7, "left")]),
        ]
    )
    async with client(fake) as factory:
        interpreter = factory.interpreter(resume=False)
        async with interpreter.activities() as acts:
            burst = await acts.__anext__()
        assert burst.describe() == "0:00.0 Right hand pushes stone 5 (1 push)"
        async with interpreter.updates() as updates:
            rest = await updates.__anext__()
    assert rest.before == 0
    assert rest.lines() == [
        "0:02.0 Left hand places stone 7",
        "0:00.2 Left hand lifts stone 7 and sets it down (1.8 s)",
    ]


async def test_a_loop_that_stops_at_the_end_of_a_frame_leaves_nothing() -> None:
    fake = FakeFactory(recording=[HEADER, frame(0, events=[ev("new", 1)]), frame(1, events=[ev("new", 2)])])
    async with client(fake) as factory:
        interpreter = factory.interpreter(resume=False)
        async with interpreter.events() as first:
            assert (await first.__anext__()).stone_id == 1
        assert [e.stone_id async for e in interpreter.events()] == [2]


async def test_wait_for_times_out() -> None:
    async def delay(op: str, n: int) -> None:
        if n == 1:
            await asyncio.Event().wait()

    fake = FakeFactory(recording=[HEADER, frame(0), frame(1)], delay=delay)
    async with client(fake) as factory:
        with pytest.raises(TimeoutError, match=r"no matching event within 0\.01 s"):
            await factory.interpreter().wait_for("lift", timeout=0.01)
    assert fake.streams_opened == fake.streams_closed


async def test_a_frame_event_without_an_id() -> None:
    fake = FakeFactory(recording=[HEADER, frame(1), worker_line(), frame(2)])
    fake.inject("frame", frame(0), after=0)
    async with client(fake) as factory:
        interpreter = factory.interpreter(resume=False)
        updates = [u async for u in interpreter.updates()]
    assert [u.frame.g for u in updates if u.frame] == [0, 1, 2]
    assert interpreter.last_event_id == "fakeb00t01-3"


async def test_a_late_frame_is_skipped() -> None:
    """A frame that arrives after a newer one (and is no revision) changes nothing; the iterators skip it."""
    fake = FakeFactory(recording=[HEADER, frame(1), frame(3), frame(2)])
    async with client(fake) as factory:
        updates = [u async for u in factory.interpreter(resume=False).updates()]
    assert [u.frame.g for u in updates if u.frame] == [1, 3]

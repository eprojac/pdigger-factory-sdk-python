"""Numbers and text (SDK-DESIGN.md 4.1, 4.2, 4.4, 4.5), checked against the contract's own examples."""

from __future__ import annotations

import pytest

from pdigger_factory_sdk import Event, FrameRef, Move, PushBurst, Summary
from pdigger_factory_sdk.text import (
    clock,
    describe_summary,
    event_sentence,
    fmt,
    hand_word,
    join_ids,
    round3,
    verb,
)


@pytest.mark.parametrize(
    ("x", "d", "want"),
    [(0.05, 1, "0.1"), (0.5, 0, "1"), (1.23, 2, "1.23"), (0.0, 2, "0.00"), (-1.25, 1, "-1.3"), (0.125, 2, "0.13")],
)
def test_fmt(x: float, d: int, want: str) -> None:
    assert fmt(x, d) == want


def test_round3() -> None:
    assert round3(1.0005) == 1.001
    assert round3(18.1494) == 18.149


@pytest.mark.parametrize(
    ("t", "want"),
    [(5.2, "0:05.2"), (1277.8, "21:17.8"), (3723.4, "1:02:03.4"), (59.96, "1:00.0"), (0.0, "0:00.0")],
)
def test_clock(t: float, want: str) -> None:
    assert clock(t) == want


def test_hands_and_verbs() -> None:
    assert [hand_word(s) for s in ("left", "right", "both", None, "odd")] == [
        "Left hand",
        "Right hand",
        "Both hands",
        "A hand",
        "A hand",
    ]
    assert verb("both", "is") == "are"
    assert verb("left", "is") == "is"


def test_join_ids() -> None:
    assert join_ids([3]) == "3"
    assert join_ids([3, 5]) == "3 and 5"
    assert join_ids([3, 5, 9]) == "3, 5 and 9"
    assert join_ids([None]) == "?"
    assert join_ids([]) == "?"


@pytest.mark.parametrize(
    ("args", "want"),
    [
        (("new", 7), "stone 7 appears"),
        (("new", 7, None, None, None, "image"), "stone 7 appears (found in the image)"),
        (("confirm", 7), "stone 7 is confirmed"),
        (("freeze", 7), "stone 7 gets its final shape"),
        (("push", 7, "left", "course"), "stone 7 is pushed along its row"),
        (("push", 7, None, "reseat"), "stone 7 is reseated"),
        (("push", 53, "both"), "Both hands push stone 53"),
        (("push", 7), "stone 7 is pushed"),
        (("push", 7, None, "nudge"), "stone 7 is pushed (nudge)"),
        (("lift", 72, "right"), "Right hand lifts stone 72"),
        (("hinge", 7, "left"), "Left hand tips stone 7 up on its edge"),
        (("carry", 7, "both"), "Both hands carry stone 7"),
        (("place", 7, None), "A hand places stone 7"),
        (("drop", 7, "right"), "Right hand drops stone 7"),
        (("gone", 7, "left"), "Left hand takes stone 7 out of view"),
        (("delete", 7), "stone 7 is removed"),
        (("delete", 7, None, "absent"), "stone 7 is removed (no longer seen)"),
        (("delete", 7, None, "overlap"), "stone 7 is removed (overlapped another stone)"),
        (("delete", 7, None, "outside"), "stone 7 is removed (outside the board)"),
        (("delete", 7, None, "merged"), "stone 7 is removed (merged with a neighbour)"),
        (("delete", 7, None, "empty"), "stone 7 is removed (its place is empty)"),
        (("delete", 7, None, "hand"), "stone 7 is removed (a hand emptied its place)"),
        (("delete", 7, None, "carried-away"), "stone 7 is removed (carried away)"),
        (("delete", 7, None, "zapped"), "stone 7 is removed (zapped)"),
        (("split", 7, None, None, [8, 9]), "stone 7 splits into 8 and 9"),
        (("split", 7, None, None, [8, 9, 10]), "stone 7 splits into 8, 9 and 10"),
        (("split", 7, None, None, [8]), "stone 7 is trimmed"),
        (("split", 7, None, None, []), "stone 7 splits"),
        (("split", 7), "stone 7 splits"),
        (("teleport", None), "stone ?: teleport"),
    ],
)
def test_event_sentences(args: tuple[object, ...], want: str) -> None:
    assert event_sentence(*args) == want  # type: ignore[arg-type]


def test_event_describe() -> None:
    e = Event(type="lift", g=6389, t=1277.8, wall=None, session=1, video="dry1", stone_id=72, hand="right")
    assert e.describe() == "21:17.8 Right hand lifts stone 72"
    assert e.clock == "21:17.8"
    assert e.key == ("lift", 72, "right", None)
    assert e.to_dict()["stone_id"] == 72


def ref(t: float) -> FrameRef:
    return FrameRef(g=round(t * 5), t=t)


def move(outcome: str | None, **kw: object) -> Move:
    m = Move(stone_id=76, hand="left", start=ref(109.3), session=1, video="dry1", outcome=outcome, duration=0.4)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


@pytest.mark.parametrize(
    ("m", "want"),
    [
        (move("placed", carried=True, distance=0.3849), "1:49.3 Left hand moves stone 76 0.38 units in 0.4 s"),
        (move("placed", carried=True), "1:49.3 Left hand moves stone 76 in 0.4 s"),
        (move("placed", tipped=True), "1:49.3 Left hand tips stone 76 and sets it down (0.4 s)"),
        (move("placed"), "1:49.3 Left hand lifts stone 76 and sets it down (0.4 s)"),
        (move("dropped"), "1:49.3 Left hand drops stone 76 after 0.4 s"),
        (move("carried_away", hand="both"), "1:49.3 Both hands take stone 76 away (0.4 s)"),
        (move("removed"), "1:49.3 Left hand lifts stone 76; it is removed after 0.4 s"),
        (move("interrupted", hand="both"), "1:49.3 Both hands are still holding stone 76 (0.4 s so far)"),
        (move(None, duration=None, stone_id=None), "1:49.3 Left hand is still holding stone ? (0.0 s so far)"),
        (
            move("dropped", partial=True, end=ref(110.0), duration=0.0),
            "1:50.0 Left hand drops stone 76 after 0.0 s (joined mid-move)",
        ),
        (move("dropped", partial=True), "1:49.3 Left hand drops stone 76 after 0.4 s"),
    ],
)
def test_move_text(m: Move, want: str) -> None:
    assert m.describe() == want


def burst(stones: list[int], pushes: int, hand: str = "both", duration: float | None = 0.6) -> PushBurst:
    return PushBurst(
        hand=hand,
        start=ref(20.5),
        last=ref(21.1),
        session=1,
        video="dry1",
        stones=stones,
        pushes=pushes,
        duration=duration,
    )


@pytest.mark.parametrize(
    ("b", "want"),
    [
        (burst([53], 1), "0:20.5 Both hands push stone 53 (1 push)"),
        (burst([49, 53], 5), "0:20.5 Both hands push stones 49 and 53 (5 pushes in 0.6 s)"),
        (burst([3, 5, 9], 3, "left"), "0:20.5 Left hand pushes stones 3, 5 and 9 (3 pushes in 0.6 s)"),
        (
            burst([3, 5, 9, 11, 12, 14, 20, 21, 22, 23], 12, "right", None),
            "0:20.5 Right hand pushes stones 3, 5, 9, 11, 12, 14 and 4 more (12 pushes in 0.0 s)",
        ),
        (burst([], 2), "0:20.5 Both hands push stone ? (2 pushes in 0.6 s)"),
    ],
)
def test_push_text(b: PushBurst, want: str) -> None:
    assert b.describe() == want
    assert b.to_dict()["pushes"] == b.pushes


def test_summary_text_with_nulls() -> None:
    empty = Summary()
    want = (
        "frames: 0 (with world 0, without 0, revisions 0)\n"
        "media: 0:00.0, sessions 0, gaps 0\n"
        "stones: last -, max 0, confirmed -\n"
        "moves: 0 (placed 0, dropped 0, carried away 0, removed 0, interrupted 0, partial 0, carried 0), "
        "mean - s, - units\n"
        "push bursts: 0 (left 0, right 0, both 0), pushes 0\n"
        "events: new 0, confirm 0, freeze 0, push 0, lift 0, hinge 0, carry 0, place 0, drop 0, gone 0, delete 0, "
        "split 0, other 0\n"
        "trays: 0 (arrived 0), stones at arrival 0, added 0 (+0 unclear), removed 0 (+0 unclear), not sure 0\n"
        "left hand: free 0:00.0, touch 0:00.0, grasp 0:00.0, hinge 0:00.0, carry 0:00.0\n"
        "right hand: free 0:00.0, touch 0:00.0, grasp 0:00.0, hinge 0:00.0, carry 0:00.0\n"
    )
    assert empty.describe() == want
    assert describe_summary(empty.to_dict()) == want
    assert empty.to_dict()["first_g"] is None


def test_summary_text_with_means() -> None:
    d = Summary().to_dict()
    d["moves"]["mean_duration"] = 4.05
    d["moves"]["mean_distance"] = 0.375
    d["stones"] = {"last": 41, "max": 52, "confirmed_last": 41}
    out = describe_summary(d)
    assert "mean 4.1 s, 0.38 units" in out
    assert "stones: last 41, max 52, confirmed 41" in out


def test_a_summary_from_before_trays_reads_as_no_trays() -> None:
    d = Summary().to_dict()
    del d["trays"]  # the form published before trays were counted
    out = describe_summary(d)
    assert out == Summary.from_dict(d).describe()
    assert "trays: 0 (arrived 0), stones at arrival 0, added 0 (+0 unclear), removed 0 (+0 unclear), not sure 0" in out

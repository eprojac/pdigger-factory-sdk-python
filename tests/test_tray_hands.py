"""Version 4 of the tray rules (SDK-DESIGN.md 4.6.1): a certain removal needs a hand on the stone, and a visit continues
the open tray only when nine tenths of its first frame's stones were seen on it."""

from __future__ import annotations

from typing import Any

import pytest

from pdigger_factory_sdk import Tray
from pdigger_factory_sdk.models import WorldEvent, WorldLayer
from pdigger_factory_sdk.trays import TrayCounts, TrayLedger, TrayPoint, _hand_touch

from . import synth


def _feed(frames: list[dict[str, Any]]) -> list[Tray]:
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 1, "dry1", closed.append)
    for w in frames:
        ledger.feed(WorldLayer.from_json(w), TrayPoint(w["g"], w["t"], None))
    ledger.close()
    return closed


class _Scene:
    """Frames 0.2 s apart; `on` holds the world's stones by id."""

    def __init__(self) -> None:
        self.g = 0
        self.on: dict[int, dict[str, Any]] = {}
        self.frames: list[dict[str, Any]] = []

    def step(
        self, n: int = 1, events: list[dict[str, Any]] | None = None, left: str = "free", motion: str = "static"
    ) -> None:
        for k in range(n):
            w = synth.world(
                self.g,
                round(self.g * 0.2, 6),
                stones=list(self.on.values()),
                events=(events or []) if k == 0 else [],
                left=left,
            )
            w["motion"] = motion
            self.frames.append(w)
            self.g += 1


def _removal(touch: list[dict[str, Any]] | None, touch_g: int, loss: dict[str, Any], loss_g: int) -> Tray:
    """Stones 1 and 2 arrive; `touch` lands on stone 2 at `touch_g`; stone 2 leaves the world at `loss_g` with `loss`;
    a hand works away from the stones at g40, so a calm moment follows at g43, more than 3 s after the loss."""
    sc = _Scene()
    sc.on = {1: synth.stone(1, -0.6), 2: synth.stone(2, 0.6)}
    while sc.g < 50:
        if sc.g == touch_g and touch:
            sc.step(1, touch, left="touch")
        elif sc.g == loss_g:
            del sc.on[2]
            sc.step(1, [loss])
        elif sc.g in (20, 40):
            sc.step(1, left="touch")
        else:
            sc.step()
    (tray,) = _feed(sc.frames)
    return tray


@pytest.mark.parametrize(
    ("event", "touched"),
    [
        (synth.ev("lift", 2, "right"), True),
        (synth.ev("drop", 2, "right", why="t-release"), True),
        (synth.ev("gone", 2, "right"), True),
        (synth.ev("push", 2, "left"), True),
        (synth.ev("push", 2, why="reseat"), False),
        (synth.ev("delete", 2, why="hand"), True),
        (synth.ev("delete", 2, why="carried-away"), True),
        (synth.ev("delete", 2, why="absent"), False),
        (synth.ev("freeze", 2), False),
    ],
)
def test_a_hand_touch_is_a_hand_event_a_push_with_a_side_or_a_delete_by_a_hand(
    event: dict[str, Any], touched: bool
) -> None:
    assert _hand_touch(WorldEvent.from_json(event)) is touched


def test_a_stone_the_world_lost_with_no_hand_on_it_is_never_removed_for_certain() -> None:
    tray = _removal(None, -1, synth.ev("delete", 2, why="absent"), 30)
    removed = [f for f in tray.facts if f.kind == "removed"]
    assert [(f.certain, f.code) for f in removed] == [(False, "not_found")]
    leaving = tray.facts[-1]
    assert leaving.open is not None
    assert [(o.kind, o.cause) for o in leaving.open] == [("removed", "no_hand")]


def test_a_stone_a_hand_emptied_is_removed_for_certain_at_the_next_calm_moment() -> None:
    tray = _removal(None, -1, synth.ev("delete", 2, why="hand"), 30)
    removed = [f for f in tray.facts if f.kind == "removed"]
    assert [(f.certain, f.why, f.g) for f in removed] == [(True, "missing", 43)]


def test_a_touch_up_to_three_seconds_before_the_last_sight_counts_even_before_the_last_calm_moment() -> None:
    # pushed at g24 (t 4.8); the calm moment at g27 (t 5.4) comes after it; lost silently at g28, last seen at g27:
    # the window starts at min(5.4, 5.4 - 3.0) = 2.4, so the push counts
    tray = _removal([synth.ev("push", 2, "left")], 24, synth.ev("delete", 2, why="absent"), 28)
    assert [(f.certain, f.g) for f in tray.facts if f.kind == "removed"] == [(True, 43)]


def test_a_touch_before_both_the_last_calm_moment_and_the_three_seconds_does_not_count() -> None:
    # pushed at g5 (t 1.0); calm moments at g8 and g23 (t 4.6); lost silently at g30 (t 6.0), last seen at g29:
    # the window starts at min(4.6, 5.8 - 3.0) = 2.8, after the push
    tray = _removal([synth.ev("push", 2, "left")], 5, synth.ev("delete", 2, why="absent"), 30)
    assert [(f.certain, f.code) for f in tray.facts if f.kind == "removed"] == [(False, "not_found")]


def test_a_touch_after_the_last_calm_moment_counts_while_hands_keep_working_until_the_loss() -> None:
    # a calm moment at g23 (t 4.6); pushed at g25 (t 5.0); hands work until stone 2 is lost silently at g44 (t 8.8),
    # last seen at g43: the window starts at min(4.6, 8.6 - 3.0) = 4.6, so the push counts
    sc = _Scene()
    sc.on = {1: synth.stone(1, -0.6), 2: synth.stone(2, 0.6)}
    sc.step(20)
    sc.step(1, left="touch")
    sc.step(4)
    sc.step(1, [synth.ev("push", 2, "left")], left="touch")
    sc.step(18, left="touch")
    del sc.on[2]
    sc.step(1, [synth.ev("delete", 2, why="absent")], left="touch")
    sc.step(10)  # a calm moment at g47, too soon after the loss
    sc.step(1, left="touch")
    sc.step(10)  # the next one, g58, removes it
    (tray,) = _feed(sc.frames)
    assert [(f.certain, f.g) for f in tray.facts if f.kind == "removed"] == [(True, 58)]


def _board(known: int) -> list[Tray]:
    """Ten stones arrive; the conveyor moves; then a board whose first frame shows `known` of them and 10 - `known`
    stones the tray never saw."""
    sc = _Scene()
    sc.on = {i: synth.stone(i, -0.9 + 0.2 * i) for i in range(10)}
    sc.step(12)
    sc.step(3, motion="moving")
    for i in range(known, 10):
        del sc.on[i]
        sc.on[100 + i] = synth.stone(100 + i, -0.9 + 0.2 * i)
    sc.step(1, [synth.ev("new", 100 + i) for i in range(known, 10)])
    sc.step(11)
    return _feed(sc.frames)


def test_a_visit_with_nine_tenths_of_its_stones_seen_goes_on_as_the_same_tray() -> None:
    assert [t.n for t in _board(9)] == [1]


def test_a_visit_with_fewer_than_nine_tenths_of_its_stones_seen_is_a_new_tray() -> None:
    trays = _board(8)
    assert [t.n for t in trays] == [1, 2]
    assert all(t.arrived is not None for t in trays)
    assert [len([s for s in t.stones if s.status == "on"]) for t in trays] == [10, 10]

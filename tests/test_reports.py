"""Tray reports (SDK-DESIGN.md 4.7) beyond the golden files: the trays that have none, the text without a clock, the
tray's own shift, a tray where nothing it arrived with is left, a change without its point or distance, and the
reports as data."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable
from typing import Any

from pdigger_factory_sdk import Replay, Tray, TrayChange, TrayFact, TrayReport, TrayStone
from pdigger_factory_sdk.trays import CONFIDENCE, MOVED, MOVED_CONFIDENCE, TrayPoint, iso

from .support import recording


def at(t: float, wall: float | None = None) -> TrayPoint:
    return TrayPoint(round(t * 5), t, wall)


def fact(kind: str, t: float, **fields: Any) -> TrayFact:
    return dataclasses.replace(TrayFact(kind, 1, 7, "v", 10, t, None), **fields)


def stone(sid: str, place: tuple[float, float], first: tuple[float, float], sure: bool = True) -> TrayStone:
    """A stone the tray arrived with, seen at the leaving point at `place`; nothing touched it."""
    return TrayStone(
        sid, 1, "on", sure, False, None, TrayPoint(5, 1.0), place, (0.4, 0.2), first_place=first, left_place=place
    )


def tray(
    n: int,
    arrived: TrayPoint | None,
    leaving: TrayPoint | None,
    stones: Iterable[TrayStone] = (),
    facts: Iterable[TrayFact] = (),
) -> Tray:
    return Tray(7, "v", n, arrived, leaving, tuple(stones), tuple(facts))


def test_a_tray_that_has_not_left_has_no_report() -> None:
    assert tray(1, None, None).report() is None  # never arrived
    assert tray(1, at(1.0), None).report() is None  # still on
    assert tray(1, at(1.0), at(9.0)).report() is None  # no facts
    assert tray(1, at(1.0), at(9.0), facts=[fact("arrived", 1.0)]).report() is None  # facts not ending in its leaving


def test_a_tray_without_a_clock_and_with_nothing_it_arrived_with_left() -> None:
    gone = stone("s1t1.0-1-4", (0.0, 0.0), (0.0, 0.0))
    gone = dataclasses.replace(gone, status="removed", certain=True, world_id=None)
    removed = fact("removed", 4.0, stone=gone.id, certain=True, code="carried_away")
    other = fact("removed", 5.0, stone="not-a-tray-stone", certain=True)  # a fact of no stone of the tray is left out
    leaving = fact("leaving", 9.0, stones=0, sure=0)  # no unclear count; the arrival has no stone count
    r = tray(1, at(1.0), at(9.0), [gone], [fact("arrived", 1.0), removed, other, leaving]).report()
    assert r is not None
    assert (r.stones, r.unclear, r.unsure, r.complete, r.moved) == (0, 0, 0, True, ())
    assert r.describe() == (
        "tray 1, 0:01.0 to 0:09.0: 1 removed, 0 added, 0 moved\n  0:04.0 removed s1t1.0-1-4 from (0.00, 0.00) (0.50)"
    )
    # no leaving answer: its evidence is undecided, and a certain removal so is 0.50 (CONFIDENCE, 4.6.13)
    assert r.removed[0].confidence == CONFIDENCE[("removed", True, "undecided")] == 0.50
    assert r.removed[0].code == "carried_away"
    # an addition that is not certain: counted unclear by the leaving fact, not as a stone that is not sure
    set_down = dataclasses.replace(stone("s1t1.0-1-5", (1.0, 1.0), (1.0, 1.0), sure=False), added=True, certain=False)
    added = fact("added", 6.0, stone=set_down.id, certain=False, code="not_recognised")
    leaving = fact("leaving", 9.0, stones=1, sure=0, unclear=1)
    r = tray(1, at(1.0), at(9.0), [set_down], [fact("arrived", 1.0), added, leaving]).report()
    assert r is not None
    assert r.unsure == 0
    assert r.describe() == (
        "tray 1, 0:01.0 to 0:09.0: 0 removed, 1 added, 0 moved, not complete: 1 unclear\n"
        "  0:06.0 added s1t1.0-1-5 at (1.00, 1.00) (unclear, 0.52)"
    )
    # what the leaving's answer found of it sets its confidence: not on the tray as it arrived, or on it
    for evidence, confidence in (("not_found", 0.96), ("found", 0.02), ("undecided", 0.52)):
        known = dataclasses.replace(set_down, evidence=evidence)
        r2 = tray(1, at(1.0), at(9.0), [known], [fact("arrived", 1.0), added, leaving]).report()
        assert r2 is not None
        assert r2.added[0].confidence == confidence
    # its final place is its place at the leaving point; not seen there, where it was last seen
    seen = dataclasses.replace(set_down, place=(2.0, -0.001), left_place=(1.5, -0.004))
    r = tray(1, at(1.0), at(9.0), [seen], [fact("arrived", 1.0), added, leaving]).report()
    assert r is not None
    assert (r.added[0].place, r.added[0].describe()) == (
        (1.5, -0.004),
        "0:06.0 added s1t1.0-1-5 at (1.50, 0.00) (unclear, 0.52)",
    )
    unseen = dataclasses.replace(seen, left_place=None)
    r = tray(1, at(1.0), at(9.0), [unseen], [fact("arrived", 1.0), added, leaving]).report()
    assert r is not None
    assert r.added[0].describe() == "0:06.0 added s1t1.0-1-5 at (2.00, 0.00) (unclear, 0.52)"


def test_the_trays_own_shift_is_taken_out_before_a_stone_is_called_moved() -> None:
    # stones the tray carried 0.1 along y as it left, one of them pushed 0.3 along x besides; d is not sure, so it is
    # counted as not sure and not as moved however far its place is
    kept = [
        stone("a", (0.0, 0.1), (0.0, 0.0)),
        stone("b", (1.0, 0.1), (1.0, 0.0)),
        dataclasses.replace(stone("c", (2.3, 0.1), (2.0, 0.0)), touched=40),
        dataclasses.replace(stone("d", (3.9, 0.1), (3.0, 0.0), sure=False), touched=41),
        stone("e", (4.0, 0.1), (4.0, 0.0)),
    ]
    facts = [fact("arrived", 2.0, stones=5, wall=1_794_960_000.25), fact("leaving", 9.0, stones=5, sure=4, unclear=0)]

    def report(*stones: TrayStone) -> TrayReport:
        r = tray(2, at(1.0), at(9.0), stones, facts).report()
        assert r is not None
        return r

    def names(r: TrayReport) -> list[str]:
        return [c.stone for c in r.moved]

    r = report(*kept)
    assert [(c.stone, c.distance, c.certain, c.place, c.at, c.confidence) for c in r.moved] == [
        ("c", 0.3, True, (2.3, 0.1), None, MOVED_CONFIDENCE)
    ]
    assert (r.unsure, r.complete) == (1, False)
    assert r.describe() == (
        "tray 2 (2026-11-18T00:00:00.3Z), 0:02.0 to 0:09.0: 0 removed, 0 added, 1 moved, not complete: 1 not sure\n"
        "  moved c 0.30 to (2.30, 0.10) (0.87)"
    )
    # the median of an odd half: one of two sure stones, three of five
    assert names(report(kept[0], kept[2])) == ["c"]
    assert names(report(*kept[:3], stone("f", (5.0, 0.1), (5.0, 0.0)), stone("g", (6.0, 0.1), (6.0, 0.0)))) == ["c"]
    assert report(kept[3]).moved == ()  # nothing sure: nothing moved
    # the same place, but nothing touched it: an outline the world refined is not a move
    assert report(kept[0], kept[1], stone("c", (2.3, 0.1), (2.0, 0.0))).moved == ()
    # not seen at the leaving point: its place there is not known, and it is left out of the shift as well
    unseen = dataclasses.replace(kept[2], left_place=None)
    assert report(kept[0], kept[1], unseen).moved == ()
    assert (unseen.to_dict()["left_place"], unseen.to_dict()["touched"]) == (None, 40)
    assert MOVED == 0.75


def test_a_change_without_its_point_or_distance() -> None:
    def change(kind: str, certain: bool, confidence: float) -> TrayChange:
        return TrayChange(kind, "x", None, certain, None, (0.0, 0.0), (1.0, 1.0), None, confidence)

    assert change("removed", False, 0.71).describe() == "removed x from (0.00, 0.00) (unclear, 0.71)"
    assert change("moved", True, 0.87).describe() == "moved x 0.00 to (0.00, 0.00) (0.87)"
    # a coordinate that rounds to zero prints without its sign
    far = TrayChange("removed", "x", None, True, None, (-1.236, -0.0049), (1.0, 1.0), None, 0.98)
    assert far.describe() == "removed x from (-1.24, 0.00) (0.98)"
    assert iso(1_794_960_000.04) == "2026-11-18T00:00:00.0Z"


def test_a_report_as_data() -> None:
    """`to_dict()` has every field, with the names the other languages write, and is JSON as it is."""
    removed = TrayChange(
        "removed", "s-1", TrayPoint(20, 4.0, 1.5), True, "carried_away", (1.0, 2.0), (0.4, 0.2), None, 0.98
    )
    moved = TrayChange("moved", "s-2", None, True, None, (3.0, 4.0), (0.4, 0.2), 0.3, 0.87)
    r = TrayReport(7, "v", 2, TrayPoint(5, 1.0), TrayPoint(45, 9.0, 2.5), 3, (removed,), (), (moved,), 0, 1, False)
    assert r.to_dict() == {
        "session": 7,
        "video": "v",
        "tray": 2,
        "arrived": {"g": 5, "t": 1.0, "wall": None},
        "left": {"g": 45, "t": 9.0, "wall": 2.5},
        "stones": 3,
        "removed": [
            {
                "kind": "removed",
                "stone": "s-1",
                "at": {"g": 20, "t": 4.0, "wall": 1.5},
                "certain": True,
                "code": "carried_away",
                "place": [1.0, 2.0],
                "size": [0.4, 0.2],
                "distance": None,
                "confidence": 0.98,
            }
        ],
        "added": [],
        "moved": [
            {
                "kind": "moved",
                "stone": "s-2",
                "at": None,
                "certain": True,
                "code": None,
                "place": [3.0, 4.0],
                "size": [0.4, 0.2],
                "distance": 0.3,
                "confidence": 0.87,
            }
        ],
        "unclear": 0,
        "unsure": 1,
        "complete": False,
    }
    (report,) = Replay.open(str(recording("dry1-0018-0153"))).tray_reports()
    data = json.loads(json.dumps(report.to_dict()))
    keys = ["session", "video", "tray", "arrived", "left", "stones", "removed", "added", "moved", "unclear", "unsure"]
    assert list(data) == [*keys, "complete"]  # the order the other languages write
    assert (len(data["removed"]), len(data["added"]), data["unclear"], data["complete"]) == (11, 13, 18, False)

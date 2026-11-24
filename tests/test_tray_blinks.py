"""A brief silent omission can preserve identity only while no hand could have worked on the stones."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from pdigger_factory_sdk import Tray
from pdigger_factory_sdk.models import WorldLayer
from pdigger_factory_sdk.trays import TrayCounts, TrayLedger, TrayPoint, _Tray

from .support import recording_lines


def _two_stones(
    hand_g: int | None, missing: int, rival_from: int | None = None, *, co_blink: bool = False
) -> tuple[Tray, dict[int, str]]:
    """ID 1 disappears at g13, next to ID 2. End on its recovery frame with an unrelated new event."""
    base = next(row["world"] for row in recording_lines("trays-synthetic") if "world" in row)
    trays: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 1, "example", trays.append)
    links: dict[int, str] = {}
    for g in range(14 + missing if rival_from is None else 23):
        w = deepcopy(base)
        w.update(g=g, t=g * 0.2, motion="static")
        a, b = w["stones"][:2]
        a["solid"]["pos"][0], b["solid"]["pos"][0] = 0.0, 0.35
        if rival_from is not None:
            a["solid"]["dims"] = [0.09, 0.05, 0.046]
        absent = 13 <= g < 13 + missing
        w["stones"] = [b] if absent else [a, b]
        if rival_from is not None and g >= rival_from and not (co_blink and absent):
            rival = deepcopy(a)
            rival["id"], rival["solid"]["pos"][0] = 3, 0.12
            w["stones"].append(rival)
        for key in ("stones", "confirmed", "frozen", "fitted"):
            w["stats"][key] = len(w["stones"])
        events: list[dict[str, Any]] = [{"type": "new", "id": 100}] if g == 13 + missing else []
        if hand_g == g:
            events.append({"type": "place", "id": 2, "side": "left"})
        w["events"] = events
        ledger.feed(WorldLayer.from_json(w), TrayPoint(g, g * 0.2, None))
        if g == 13:
            links = ledger.links()
    ledger.close()
    return trays[0], links


@pytest.mark.parametrize("missing", [1, 2])
def test_silent_omissions_preserve_links_and_the_last_complete_leaving_frame(missing: int) -> None:
    tray, links = _two_stones(None, missing)
    assert tray.leaving is not None
    assert tray.leaving.g == 12
    assert links[1] == tray.stones[0].id
    assert [s.world_id for s in tray.stones if s.sure] == [1, 2]
    assert tray.stones[0].touched is None
    assert [f.kind for f in tray.facts] == ["arrived", "leaving"]


@pytest.mark.parametrize("hand_g", [12, 14])
def test_hand_work_at_either_end_keeps_the_identity_in_doubt(hand_g: int) -> None:
    tray, _ = _two_stones(hand_g, 1)
    assert [s.world_id for s in tray.stones if not s.sure] == [1, 2]
    assert [f.code for f in tray.facts if f.kind == "unsure"] == ["found_again", "appeared"]


def test_new_nearby_stone_competes_with_a_returning_id() -> None:
    tray, _ = _two_stones(None, 1, rival_from=14)
    stone = tray.stones[0]
    assert not stone.sure
    assert stone.touched == 22
    assert any(f.code == "matched_at_leaving" and f.stone == stone.id for f in tray.facts)


def test_new_nearby_stone_on_the_first_missing_frame_gets_ordinary_rename_handling() -> None:
    tray, links = _two_stones(None, 1, rival_from=13)
    assert 1 not in links
    assert links[3] == tray.stones[0].id


def test_neighbour_continuously_seen_through_the_blink_is_not_a_rival() -> None:
    tray, _ = _two_stones(None, 2, rival_from=0)
    assert tray.stones[0].sure
    assert tray.stones[0].touched is None
    assert not any(f.kind in ("removed", "added") for f in tray.facts)


def test_two_adjacent_ids_that_disappear_together_can_compete_on_return() -> None:
    tray, _ = _two_stones(None, 2, rival_from=0, co_blink=True)
    assert {s.world_id for s in tray.stones if not s.sure} == {1, 3}
    assert [f.code for f in tray.facts if f.kind == "unsure"] == ["matched_at_leaving"] * 2


def _ledger_world(positions: dict[int, float], g: int, event: tuple[str, int] | None = None) -> WorldLayer:
    raw = deepcopy(next(row["world"] for row in recording_lines("trays-synthetic") if "world" in row))
    seed = raw["stones"][0]
    stones = []
    for wid, x in positions.items():
        s = deepcopy(seed)
        s["id"], s["solid"]["pos"][0] = wid, x
        s["solid"]["dims"] = [0.05, 0.04, 0.03]
        stones.append(s)
    raw.update(stones=stones, events=[{"type": event[0], "id": event[1]}] if event else [], g=g, t=g / 5)
    for key in ("stones", "confirmed", "frozen", "fitted"):
        raw["stats"][key] = len(stones)
    return WorldLayer.from_json(raw)


def _arrived_ledger(second_x: float = 0.1) -> tuple[TrayLedger, list[Tray]]:
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 42, "example", closed.append)
    for g in range(12):
        ledger.feed(_ledger_world({1: 0, 2: second_x}, g), TrayPoint(g, g / 5, None))
    return ledger, closed


@pytest.mark.parametrize("hand_work", [False, True], ids=["calm", "last-worked-frame"])
def test_departure_position_requires_a_detection_on_the_selected_frame(hand_work: bool) -> None:
    ledger, closed = _arrived_ledger()
    ref = TrayPoint(12, 11 / 5, None)  # same media time as g11
    world = _ledger_world({1: 0, 2: 0.1}, ref.g, ("place", 2) if hand_work else None)
    world = replace(world, t=ref.t, stones=[replace(world.stones[0], detected=False), world.stones[1]])
    ledger.feed(world, ref)
    ledger.close()
    tray = closed[0]
    assert tray.leaving == ref
    assert tray.stones[0].place == (0.0, 0.0)
    assert tray.stones[0].left_place is None
    assert tray.stones[1].left_place == (0.1, 0)


def test_arrival_frame_can_supply_the_departure_positions() -> None:
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 42, "example", closed.append)
    for g in range(13):
        world = _ledger_world({1: 0, 2: 0.1}, g)
        world = replace(
            world,
            stats=replace(world.stats, confirmed=1 if g < 12 else 2),
            stones=[replace(world.stones[0], detected=False), world.stones[1]],
        )
        ledger.feed(world, TrayPoint(g, g / 5, None))
    ledger.close()
    tray = closed[0]
    assert tray.arrived == tray.leaving == TrayPoint(12, 2.4, None)
    assert [s.left_place for s in tray.stones] == [None, (0.1, 0)]


def test_separated_stones_can_disappear_together_and_keep_their_original_links() -> None:
    ledger, closed = _arrived_ledger(second_x=0.35)
    initial = ledger.links()
    for g in (12, 13):
        ledger.feed(_ledger_world({}, g), TrayPoint(g, g / 5, None))
        assert ledger.links() == initial
    ledger.feed(_ledger_world({1: 0, 2: 0.35}, 14, ("new", 100)), TrayPoint(14, 2.8, None))
    ledger.close()
    assert closed[0].leaving == TrayPoint(11, 2.2, None)
    assert {s.world_id for s in closed[0].stones} == {1, 2}
    assert all(s.sure and s.touched is None for s in closed[0].stones)


@pytest.mark.parametrize("return_x", [0.05, 0.1], ids=["tie", "other-source"])
def test_return_must_be_uniquely_nearer_its_original_place(return_x: float) -> None:
    ledger, _ = _arrived_ledger()
    ledger.feed(_ledger_world({2: 0.1}, 12), TrayPoint(12, 2.4, None))
    ledger.feed(_ledger_world({1: return_x, 2: 0.0}, 13), TrayPoint(13, 2.6, None))
    tray = ledger.current()
    assert tray is not None
    # Ordinary rename adjudication records a new link at g13. Silent continuity would leave touched unset.
    assert tray.stones[0].touched == 13


def test_unrecovered_unidentified_and_linked_stones_at_close() -> None:
    ledger, closed = _arrived_ledger()
    ledger.feed(_ledger_world({1: 0, 2: 0.1, 3: 0.8}, 12), TrayPoint(12, 2.4, None))
    ledger.feed(_ledger_world({2: 0.1}, 13), TrayPoint(13, 2.6, None))
    assert 1 in ledger.links()
    ledger.close()
    tray = closed[0]
    assert tray.leaving == TrayPoint(12, 2.4, None)
    assert [s.world_id for s in tray.stones if s.status == "on"] == [2]
    assert [(f.kind, f.certain) for f in tray.facts if f.kind in ("added", "removed")] == [("removed", False)]


@pytest.mark.parametrize("ending", ["expiry", "gap", "delete", "far"])
def test_grace_expires_or_gives_way_to_contrary_evidence(ending: str) -> None:
    ledger, _ = _arrived_ledger()
    ledger.feed(_ledger_world({2: 0.1}, 12), TrayPoint(12, 2.4, None))
    assert 1 in ledger.links()
    if ending == "gap":
        ledger.skipped()
    if ending == "expiry":
        ledger.feed(_ledger_world({2: 0.1}, 13), TrayPoint(13, 2.6, None))
        assert 1 in ledger.links()
    g = 14 if ending == "expiry" else 13
    rows = {2: 0.1, 1: 0.7} if ending == "far" else {2: 0.1}
    ledger.feed(_ledger_world(rows, g, ("delete", 1) if ending == "delete" else None), TrayPoint(g, g / 5, None))
    assert 1 not in ledger.links()


def test_unidentified_return_keeps_birth_and_geometry_at_selected_calm_frame() -> None:
    ledger, closed = _arrived_ledger()
    ledger.feed(_ledger_world({1: 0, 2: 0.1, 3: 0.6}, 12), TrayPoint(12, 2.4, None))
    ledger.feed(_ledger_world({1: 0, 2: 0.1}, 13), TrayPoint(13, 2.6, None))
    ledger.feed(_ledger_world({1: 0, 2: 0.1, 3: 0.65}, 14, ("new", 4)), TrayPoint(14, 2.8, None))
    ledger.close()
    stone = next(s for s in closed[0].stones if s.world_id == 3)
    assert stone.born == TrayPoint(12, 2.4, None)
    assert stone.left_place is not None
    assert stone.left_place[0] == 0.6
    assert stone.place[0] == 0.65


@pytest.mark.parametrize(
    "case",
    [
        "positive",
        "ghost",
        "hand",
        "gap",
        "replacement",
        "move",
        "resize",
        "rival",
        "source_rival",
        "expiry",
        "rewind",
        "same_time",
        "latest",
        "drift",
    ],
)
def test_a_real_sight_beside_the_leaving_point_only_supports_uncertain_presence(case: str) -> None:
    """The extra observation never proves where the stone came from, or fabricates a place in the leaving frame."""
    base = next(row["world"] for row in recording_lines("trays-synthetic") if "world" in row)
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 1, "sample", closed.append)
    for g in range(18):
        w = deepcopy(base)
        t = g * 0.2 + (4 if case == "expiry" and g > 15 else 0)
        if case == "rewind" and g == 16:
            t = 2.8  # Sequence numbers advance, but this frame predates the positive sight at 3.0 s.
        if case == "same_time" and g == 16:
            t = 3.0
        w.update(g=g, t=t, motion="static", events=[])
        a, b = w["stones"][:2]
        a["solid"]["pos"][0], b["solid"]["pos"][0] = -0.6, 0.6
        w["stones"] = [b] if g == 15 else [a, b]
        if g >= 10:
            stone = deepcopy(a)
            stone["id"] = 4 if case == "replacement" and g >= 16 else 3
            stone["solid"]["pos"][0] = 0.3 if case == "move" and g >= 16 else 0.0
            stone["detected"] = (g in (10, 11) and case != "ghost") or g == 15
            stone["mode"] = "seen" if stone["detected"] else "kept"
            if g == 15:
                stone["solid"]["pos"][0] = 0.02
            if g == 16 and case in ("latest", "drift"):
                stone["detected"], stone["mode"] = True, "seen"
                stone["solid"]["pos"][0] = 0.06 if case == "latest" else 0.13
            if g == 17 and case == "drift":
                stone["solid"]["pos"][0] = 0.21
            if case == "resize" and g >= 16:
                stone["solid"]["dims"][0] *= 2
            w["stones"].append(stone)
        if g == 16:
            w["events"] = [{"type": "new", "id": 100}]
            if case == "hand":
                w["events"].append({"type": "place", "side": "left", "id": 2})
            if case == "gap":
                ledger.skipped()
        if case == "rival" and g >= 16:
            rival = deepcopy(a)
            rival["id"], rival["solid"]["pos"][0] = 4, 0.10
            w["stones"].append(rival)
        if case == "source_rival" and g == 15:
            rival = deepcopy(a)
            rival["id"] = 4
            rival["solid"]["pos"][:2] = [0.02, 0.145]
            w["stones"].append(rival)
        for key in ("stones", "confirmed", "frozen", "fitted"):
            w["stats"][key] = len(w["stones"])
        ledger.feed(WorldLayer.from_json(w), TrayPoint(g, t, None))
    ledger.close()
    tray = closed[0]
    sightings = [s for s in tray.stones if s.world_id == 3]
    if case == "replacement":  # world stone 3 is gone by the end: nothing to count
        assert sightings == []
        return
    # where the sight cannot be used, the stone is counted at the leaving point's own place for it, where the world kept
    # it (not held, less than 0.30 of its box under another unheld stone's: version 7), never at the sight's
    place = {"positive": 0.02, "same_time": 0.02, "latest": 0.06}.get(case, 0.0)
    assert len(sightings) == 1
    stone_view = sightings[0]
    assert (stone_view.sure, stone_view.added, stone_view.certain) == (False, True, False)
    assert stone_view.left_place is None
    assert stone_view.place == (place, 0.0)
    assert (tray.facts[-1].stones, tray.facts[-1].added) == (3, 0)
    assert [f.code for f in tray.facts if f.stone == stone_view.id] == ["not_recognised"]


@pytest.mark.parametrize("sight_g", [None, 24, 25], ids=["still-unseen", "intervening-sight", "seen-at-leaving"])
def test_second_stop_uses_its_own_clean_frame_and_its_actual_unseen_evidence(sight_g: int | None) -> None:
    """Motion separates two visits. At the second stop a silent absence interrupts its one three-frame quiet run."""
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 42, "sample", closed.append)
    for g in range(26):
        positions = {1: -0.5 if g <= 22 else -0.48, 2: 0.0, 3: 0.6}
        if g == 23:
            del positions[2]
        event = ("new", 100) if 14 <= g <= 20 or g >= 24 else None
        world = _ledger_world(positions, g, event)
        if g == 13:
            world = replace(world, motion="moving")
        if g >= 21 and g != sight_g:
            world = replace(world, stones=[replace(s, detected=False) if s.id == 3 else s for s in world.stones])
        ledger.feed(world, TrayPoint(g, g / 5, None))
    ledger.close()
    assert len(closed) == 1
    tray = closed[0]
    assert tray.leaving == TrayPoint(22, 4.4, None)
    assert tray.stones[0].left_place == (-0.5, 0.0)
    assert tray.stones[2].left_place is None
    assert tray.stones[2].sure is (sight_g == 25)
    assert [f.code for f in tray.facts if f.kind == "unsure"] == ([] if sight_g == 25 else ["unseen"])


@pytest.mark.parametrize(
    ("case", "counted"),
    [
        ("kept", {5: ((0.32, 0.3), (0.05, 0.04), None)}),
        ("seen", {5: ((0.32, 0.3), (0.05, 0.04), (0.32, 0.3))}),
        ("hidden", {}),
    ],
)
def test_a_second_stop_counts_what_its_clean_frame_kept(case: str, counted: dict[int, Any]) -> None:
    """The second stop of test_second_stop_uses_its_own_clean_frame_and_its_actual_unseen_evidence leaves at its
    nominated clean frame, g 22. An unidentified stone 5, boxed at (0.30, 0.30) in the first visit, kept at
    (0.32, 0.30) through g 22 and at (0.36, 0.30) after, is counted at its place there (version 7); boxed there, with
    that place as its left_place too (version 6); under a kept stone 6 that comes at g 21, it is not, and nor is 6."""
    seed = next(row["world"] for row in recording_lines("trays-synthetic") if "world" in row)
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 1, "sample", closed.append)
    for g in range(26):
        positions = {1: -0.5 if g <= 22 else -0.48, 2: 0.0, 3: 0.6}
        if g == 23:
            del positions[2]
        stones = []
        for wid, x in positions.items():
            seen = wid != 3 or g < 21
            stones.append(_small(seed["stones"][0], wid, x, 0.0, seen))
        if g >= 8:
            seen = g <= 12 or (case == "seen" and g == 22)
            stones.append(_small(seed["stones"][0], 5, 0.30 if g <= 12 else 0.32 if g <= 22 else 0.36, 0.30, seen))
            if case == "hidden" and g >= 21:
                stones.append(_small(seed["stones"][0], 6, 0.33, 0.30, False))
        w = deepcopy(seed)
        events = [{"type": "new", "id": 100}] if 14 <= g <= 20 or g >= 24 else []
        w.update(g=g, t=g / 5, motion="moving" if g == 13 else "static", events=events, stones=stones)
        for key in ("stones", "confirmed", "frozen", "fitted"):
            w["stats"][key] = len(stones)
        ledger.feed(WorldLayer.from_json(w), TrayPoint(g, g / 5, None))
    ledger.close()
    tray = closed[0]
    leaving = tray.facts[-1]
    assert (leaving.g, leaving.stones, leaving.sure, leaving.unclear) == (22, 3 + len(counted), 2, len(counted))
    assert {s.world_id: (s.place, s.size, s.left_place) for s in tray.stones if s.added} == counted


def test_two_stones_first_seen_in_one_tenth_under_one_world_id_get_two_ids() -> None:
    """Tray stone 1 arrives at t 9.96; world 1 is missing at t 10.00 while a hand places stone 2, and is back at t 10.04
    at x 0.3: nothing pairs it with stone 1, so that is removed and world 1 added, both unclear and both first seen in
    the tenth 10.0. The added one takes the id with -2 (4.6)."""
    seed = next(row["world"] for row in recording_lines("trays-synthetic") if "world" in row)
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 1, "sample", closed.append)
    times = [9.0, 9.2, 9.4, 9.6, 9.96, 10.0, 10.04] + [round(10.2 + 0.2 * k, 3) for k in range(12)]
    for g, t in enumerate(times):
        w = deepcopy(seed)
        a, b = w["stones"][:2]
        a["solid"]["pos"][0], b["solid"]["pos"][0] = 0.3 if g >= 6 else -0.6, 0.6
        stones = [b] if g == 5 else [a, b]
        events = [{"type": "place", "id": 2, "side": "left"}] if g == 5 else []
        w.update(g=g, t=t, motion="static", events=events, stones=stones)
        for key in ("stones", "confirmed", "frozen", "fitted"):
            w["stats"][key] = len(stones)
        ledger.feed(WorldLayer.from_json(w), TrayPoint(g, t, None))
    ledger.close()
    tray = closed[0]
    assert [(s.id, s.world_id, s.added) for s in tray.stones] == [
        ("s1t10.0-1-1", None, False),
        ("s1t10.0-1-2", 2, False),
        ("s1t10.0-1-1-2", 1, True),
    ]
    assert [(f.kind, f.code, f.stone) for f in tray.facts if f.stone is not None and f.kind != "unsure"] == [
        ("removed", "not_found", "s1t10.0-1-1"),
        ("added", "not_recognised", "s1t10.0-1-1-2"),
    ]


def test_a_tray_gives_no_id_twice() -> None:
    """A second stone first seen under one world id in one tenth of a second takes -2, a third -3 (4.6)."""
    seed = next(row["world"] for row in recording_lines("trays-synthetic") if "world" in row)
    ws = WorldLayer.from_json(seed).stones[0]
    tray = _Tray(n=1)
    for _ in range(3):
        tray.stones.append(TrayLedger._new(tray, "s7t1.0-1-7", ws, TrayPoint(5, 1.0, None)))
    assert [s.id for s in tray.stones] == ["s7t1.0-1-7", "s7t1.0-1-7-2", "s7t1.0-1-7-3"]


def _small(seed: dict[str, Any], wid: int, x: float, y: float, seen: bool) -> dict[str, Any]:
    """A copy of `seed` as world stone `wid` at (x, y), 0.05 by 0.04: boxed (`seen`) or kept."""
    s = deepcopy(seed)
    s["id"], s["solid"]["pos"][:2], s["solid"]["dims"] = wid, [x, y], [0.05, 0.04, 0.03]
    s["mode"], s["detected"] = ("seen", True) if seen else ("kept", False)
    return s


def test_independent_partial_calm_readings_can_raise_a_doubt_before_leaving() -> None:
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 42, "sample", closed.append)
    facts = []
    for g in range(20):
        positions = {1: -0.6, 2: 0.0, 3: 0.6}
        if g in (15, 19):
            del positions[2]
        world = _ledger_world(positions, g, ("new", 100) if g in (12, 16) else None)
        if g in (15, 19):
            world = replace(world, stones=[replace(s, detected=False) if s.id == 3 else s for s in world.stones])
        facts.extend(ledger.feed(world, TrayPoint(g, g / 5, None)))
    assert [(f.g, f.code) for f in facts if f.kind == "unsure"] == [(19, "unseen")]
    ledger.close()
    assert not closed[0].stones[2].sure

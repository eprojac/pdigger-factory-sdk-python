"""Trays and tray stones (SDK-DESIGN.md 4.6) beyond the golden files: the values, ids without a clock, the live
streams, the state, the events' tray stones and the trays the core keeps. The rules themselves are golden-tested over
every recording, `trays-synthetic` among them (the rules the real excerpts never reach)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from pdigger_factory_sdk import Frame, InterpreterCore, Replay, Summary, TrayFact
from pdigger_factory_sdk.models import ClockEvent, WorldEvent
from pdigger_factory_sdk.testing import FakeFactory
from pdigger_factory_sdk.trays import TrayCounts, stamp

from .support import client, golden, recording, recording_lines, text_of

SYNTH = "trays-synthetic"


def synthetic_frames() -> list[Frame]:
    return [Frame.from_json(x) for x in recording_lines(SYNTH) if "g" in x and "cursor" in x]


def clocked_core(**options: Any) -> InterpreterCore:
    """A core with the recording's clock state (wall = 1794960000 + t)."""
    core = InterpreterCore(**options)
    line = next(x for x in recording_lines(SYNTH) if x.get("sse") == "clock")
    core.clock(ClockEvent.from_json(line["data"]))
    return core


def test_fixed_tray_area_only_excludes_unseen_background_retirements_from_settling() -> None:
    inside = [[10.0, 10.0], [18.0, 10.0], [18.0, 18.0], [10.0, 18.0]]
    outside = [[120.0, 20.0], [128.0, 20.0], [128.0, 28.0], [120.0, 28.0]]
    bounds = (0.0, 0.0, 100.0, 100.0)

    def arrival(
        area: tuple[float, float, float, float] | None,
        polygon: list[list[float]] = outside,
        *,
        reason: str = "stranded",
        prior: bool = True,
        anchor_count: int = 2,
        mode: str = "kept",
        was_seen: bool = False,
    ) -> int:
        core = InterpreterCore(tray_bounds=area)
        deleted_ids: list[int | None] = []
        source = synthetic_frames()
        for frame in source[:12]:
            assert frame.world
            world = frame.world
            source_stone = world.stones[0]
            world.stones = [
                dataclasses.replace(stone, mode="seen", detected=True, outline=inside)
                for stone in world.stones[:anchor_count]
            ]
            if prior and frame.g < 4:
                world.stones.append(
                    dataclasses.replace(source_stone, id=99, mode=mode, detected=was_seen, outline=polygon)
                )
            world.stats = dataclasses.replace(world.stats, stones=len(world.stones), confirmed=len(world.stones))
            world.events = [WorldEvent(type="delete", id=99, why=reason)] if frame.g == 4 else []
            deleted_ids.extend(e.stone_id for e in core.feed(frame).events if e.type == "delete")
        core.end()
        (tray,) = core.trays()
        assert tray.arrived
        assert deleted_ids == [99]
        return tray.arrived.g

    assert arrival(bounds) == 4
    assert arrival(bounds, reason="outside") == 4
    assert arrival(None) == arrival(bounds, inside) == arrival(bounds, reason="hand") == 7
    assert arrival(bounds, prior=False) == arrival(bounds, anchor_count=1) == 7
    assert arrival(bounds, mode="carry") == arrival(bounds, was_seen=True) == 7
    assert arrival(bounds, outside[:2]) == arrival(bounds, [[120.0], *outside[1:]]) == 7
    with pytest.raises(ValueError, match="tray_bounds"):
        InterpreterCore(tray_bounds=(10, 0, 10, 20))


def test_a_tray_as_values() -> None:
    trays = Replay.open(recording(SYNTH)).trays()
    assert [t.n for t in trays] == [1, 2, 3, 4, 5, 6, 7, 8]
    first = trays[0]
    assert first.window() == (0.8, 5.2)
    assert trays[3].window() is None  # a stop that never settled
    assert trays[3].arrived is None
    assert trays[3].leaving is None
    assert trays[3].stones == ()
    d: dict[str, Any] = first.to_dict()
    assert (d["session"], d["video"], d["n"]) == (1794684199, "dry1", 1)
    assert d["arrived"] == {"g": 4, "t": 0.8, "wall": 1794960000.8}
    assert d["leaving"]["g"] == 26
    assert d["stones"][1] == {
        "id": "20261118T000000.8Z-1-2",
        "world_id": None,
        "status": "removed",
        "sure": True,
        "added": False,
        "certain": True,
        "born": {"g": 4, "t": 0.8, "wall": 1794960000.8},
        "place": [0.0, 0.0],
        "first_place": [0.0, 0.0],
        "left_place": None,  # removed: not on the tray when it left
        "touched": 13,  # the frame a hand last touched it (it carried it away)
        "size": [0.3, 0.2],
        "question": None,
        "looks": [],
        "by_look": False,
        "look_lost": False,
        "open": None,
        "speckle": None,  # no speckle answer for it (4.6.12)
        "by_speckle": False,  # no leaving answer paired it (4.6.13)
        "evidence": "undecided",  # set as the tray left: no leaving answer
    }
    assert d["stones"][0]["left_place"] == [-0.6, 0.0]  # seen in the leaving frame, never touched
    assert d["stones"][0]["touched"] is None
    assert d["facts"][1] == {
        "kind": "removed",
        "tray": 1,
        "session": 1794684199,
        "video": "dry1",
        "stone": "20261118T000000.8Z-1-2",
        "world_id": None,
        "certain": True,
        "why": "carried away",
        "code": "carried_away",
        "g": 22,
        "t": 4.4,
        "wall": 1794960004.4,
        "stones": None,
        "sure": None,
        "added": None,
        "removed": None,
        "unclear": None,
        "opened": None,
        "from_world": None,
        "score": None,
        "margin": None,
        "bar": None,
        "t_id": None,
        "open": None,
    }
    assert trays[4 - 1].to_dict()["arrived"] is None
    added = first.stone("20261118T000003.8Z-1-7")
    assert added is not None
    assert (added.added, added.certain, added.born.clock) == (True, True, "0:03.8")
    assert first.stone("no such stone") is None
    leaving = first.facts[-1]
    assert (leaving.kind, leaving.clock, leaving.stones, leaving.sure, leaving.added, leaving.removed) == (
        "leaving",
        "0:05.2",
        3,
        3,
        1,
        1,
    )


def test_a_certain_addition_counts_when_the_stone_is_removed_unclear() -> None:
    """Tray 5: slab 47 is set down for certain, then gone after the last calm moment, so the tray's leaving finds it
    removed unclear; the leaving still counts its certain addition, as its facts and the summary do."""
    replay = Replay.open(recording(SYNTH))
    five = replay.trays()[4]
    slab = five.stone("20261118T000025.8Z-5-47")
    assert slab is not None
    assert (slab.added, slab.status, slab.certain) == (True, "removed", False)  # certain is its removal's
    assert [(x.kind, x.certain) for x in five.facts if x.stone == slab.id] == [("added", True), ("removed", False)]
    leaving = five.facts[-1]
    assert (leaving.stones, leaving.sure, leaving.added, leaving.removed, leaving.unclear) == (2, 2, 1, 0, 1)
    assert five.check() == []

    # the self-check counts the leaving's additions, removals and unclear from the facts: each one off is C5
    def off(wrong: TrayFact) -> list[str]:
        return dataclasses.replace(five, facts=(*five.facts[:-1], wrong)).check()

    added, removed, unclear = (
        dataclasses.replace(leaving, added=0),
        dataclasses.replace(leaving, removed=1),
        dataclasses.replace(leaving, unclear=0),
    )
    assert [off(added), off(removed), off(unclear)] == [["C5"], ["C5"], ["C5"]]
    assert replay.summary().trays.added == 2  # tray 1's slab and this one


def test_a_relinked_stone_keeps_its_id() -> None:
    """The world renames stone 12 to 17: the tray stone keeps its id and stands for 17 (still sure)."""
    tray = Replay.open(recording(SYNTH)).trays()[1]
    stone = tray.stone("20261118T000006.4Z-2-12")
    assert stone is not None
    assert (stone.world_id, stone.status, stone.sure) == (17, "on", True)
    found = next(f for f in tray.facts if f.why == "found again, not for sure")
    assert (found.stone, found.world_id) == ("20261118T000006.4Z-2-13", 18)  # the world id it stands for now


def test_a_new_stone_that_fits_two_lost_stones_stands_for_neither(tmp_path: Path) -> None:
    """Tray 2 with a twin of stone 12 (14: the same size, 0.1 beside it) that the world deletes with 12 at frame 58,
    where it renames 12 to 17. 17 fits both lost stones, so while both are lost it stands for neither: a rename needs
    the one lost stone that fits (above, 12 stands for 17 from frame 58). As the tray leaves, 14 is not found."""
    lines = recording_lines(SYNTH)
    for x in lines[1:]:
        world: dict[str, Any] = x.get("world") or {}
        for s in [s for s in world.get("stones") or [] if s["id"] == 12]:
            x0, y0, z0 = s["solid"]["pos"]
            world["stones"].append({**s, "id": 14, "solid": {**s["solid"], "pos": [x0 + 0.1, y0, z0]}})
        deleted = [e for e in world.get("events") or [] if (e.get("type"), e.get("id")) == ("delete", 12)]
        if deleted:
            world["events"] += [{**e, "id": 14} for e in deleted]
    path = tmp_path / "trays-synthetic-twin.ndjson"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    core = InterpreterCore()
    while_lost: list[list[tuple[int | None, str] | None]] = []
    for update in Replay.open(path).updates(core=core):
        if update.frame is not None and 58 <= update.frame.g <= 67:  # the rename to the tray's last frame (0:13.4)
            two = next(t for t in core.trays() if t.n == 2)
            stones = [two.stone(f"20261118T000006.4Z-2-{n}") for n in (12, 14)]
            while_lost.append([None if one is None else (one.world_id, one.status) for one in stones])
    assert while_lost == [[(None, "lost"), (None, "lost")]] * 11  # frames 58 to 67, and the revision of 58
    tray = next(t for t in core.trays() if t.n == 2)
    assert [f.stone for f in tray.facts if f.code == "found_again"] == ["20261118T000006.4Z-2-13"]
    assert text_of(tray.facts[-2:]) == (
        "0:13.4 tray 2: stone 20261118T000006.4Z-2-14 not found (unclear)\n"
        "0:13.4 tray 2 leaving: 5 stones (0 sure), 0 added, 0 removed, 1 unclear\n"
    )
    assert tray.check() == []


def test_ids_without_a_clock_name_the_session() -> None:
    """Without a clock state the stamp is the session and the media time: ids stay unique after a seek back."""
    assert stamp(None, 22.5, 1795314878) == "s1795314878t22.5"
    assert stamp(1794960000.84, 0.0, 1) == "20261118T000000.8Z"
    core = InterpreterCore()  # no clock fed
    facts: list[TrayFact] = []
    for frame in synthetic_frames():
        facts += core.feed(frame).tray_facts
    facts += core.end().tray_facts
    lines = text_of(facts).splitlines()
    assert lines[1] == "0:04.4 tray 1: stone s1794684199t0.8-1-2 was removed (carried away)"
    assert lines[2] == "0:05.2 tray 1: stone s1794684199t3.8-1-7 was set down"
    assert all(f.wall is None for f in facts)


def test_events_name_their_tray_stone_and_the_state_its_tray() -> None:
    core = InterpreterCore()
    seen: dict[str, str | None] = {}
    trays_seen = set()
    for update in Replay.open(recording(SYNTH)).updates(core=core):
        for e in update.events:
            seen[f"{e.type} {e.stone_id}"] = e.tray_stone
        if update.state.tray is not None:
            trays_seen.add(update.state.tray.n)
            assert update.state.tray.leaving is None  # the open tray
    assert seen["gone 2"] == "20261118T000000.8Z-1-2"  # it stood for 2 before this frame
    assert seen["new 7"] is None  # a stone not on the tray yet
    assert seen["lift 13"] == "20261118T000006.4Z-2-13"
    assert seen["place 18"] == "20261118T000006.4Z-2-13"  # renamed: the same tray stone
    # revisions: the tray stone before the revised frame, not the links of the newest one
    assert seen["confirm 2"] == "20261118T000000.8Z-1-2"  # frame 13, when 2 still stood for its tray stone
    assert seen["confirm 7"] is None  # frame 26, still the newest, which set 7 down: no tray stone before it
    assert seen["confirm 17"] is None  # frame 58, whose rename linked 17
    assert trays_seen == {1, 2, 3, 4, 5, 6, 7, 8}
    assert core.state.tray is None  # after the end
    new = next(e for e in Replay.open(recording(SYNTH)).events() if e.type == "new")
    assert new.to_dict()["tray_stone"] is None
    lift = next(e for e in Replay.open(recording(SYNTH)).events() if e.type == "lift")
    assert lift.to_dict()["tray_stone"] == "20261118T000000.8Z-1-2"


def test_the_core_keeps_the_newest_trays() -> None:
    core = InterpreterCore(trays_kept=2)
    for frame in synthetic_frames():
        core.feed(frame)
        if frame.g == 80:  # tray 3 is on
            assert [t.n for t in core.trays()] == [1, 2, 3]
    core.end()
    assert [t.n for t in core.trays()] == [7, 8]
    unbounded = InterpreterCore(trays_kept=None)
    for frame in synthetic_frames():
        unbounded.feed(frame)
    assert [t.n for t in unbounded.trays()] == [1, 2, 3, 4, 5, 6, 7, 8]  # tray 8 is still on
    assert unbounded.state.tray is not None
    assert unbounded.state.tray.n == 8


def test_finish_leaves_the_tray_facts_in_trays() -> None:
    core = clocked_core()
    for frame in synthetic_frames():
        core.feed(frame)
    assert core.finish() == []  # no activity was open
    assert text_of(f for t in core.trays() for f in t.facts) == golden(SYNTH, "trays.txt")


async def test_the_interpreter_streams_tray_facts() -> None:
    fake = FakeFactory(recording=recording(SYNTH))
    async with client(fake) as factory:
        interpreter = factory.interpreter(resume=False)
        async with interpreter.tray_facts() as facts:
            got = [fact async for fact in facts]
        assert text_of(got) == golden(SYNTH, "trays.txt")
        assert [t.n for t in interpreter.trays()] == [1, 2, 3, 4, 5, 6, 7, 8]
        assert interpreter.end().tray_facts == []  # the stream's end made the last tray leave


async def test_a_tray_facts_loop_stopped_inside_a_frame_leaves_the_rest() -> None:
    """Three facts come with one frame (0:08.6); a loop that stops after the first leaves the other two to the next."""
    fake = FakeFactory(recording=recording(SYNTH))
    async with client(fake) as factory:
        interpreter = factory.interpreter(resume=False)
        async with interpreter.tray_facts() as facts:
            async for fact in facts:
                if fact.clock == "0:08.6":
                    break
        async with interpreter.tray_facts() as facts:
            rest = [fact async for fact in facts]
    lines = golden(SYNTH, "trays.txt").splitlines()
    assert text_of(rest).splitlines() == lines[lines.index(rest[0].describe()) :]
    assert rest[0].describe() == (
        "0:08.6 tray 2: stone 20261118T000006.4Z-2-15 is not sure any more (its box overlaps another stone)"
    )


def test_a_summary_without_trays_reads_as_zeros() -> None:
    d = Summary().to_dict()
    del d["trays"]  # a summary published before trays were counted
    assert Summary.from_dict(d).trays == TrayCounts()
    assert Summary.from_dict(Summary().to_dict()).trays.to_dict() == {
        "total": 0,
        "arrived": 0,
        "stones": 0,
        "added": 0,
        "added_unclear": 0,
        "removed": 0,
        "removed_unclear": 0,
        "unsure": 0,
    }


def test_an_update_lists_its_tray_facts_last() -> None:
    core = InterpreterCore()
    for frame in synthetic_frames():
        update = core.feed(frame)
        if update.tray_facts and update.events:
            lines = update.lines()
            assert lines[-len(update.tray_facts) :] == [f.describe() for f in update.tray_facts]
            break
    else:  # pragma: no cover - the recording has such a frame
        raise AssertionError("no frame with events and tray facts")

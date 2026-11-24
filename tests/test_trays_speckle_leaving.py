"""The leaving's speckle answer (SDK-DESIGN.md 4.6.13) beyond the golden files: tray 3 of `trays-speckle-synthetic` as
values, which answer is the leaving's, which pairs link a tray stone and what linking does, a moved stone's world stone
freed on a tray of its own, the evidence of a stone, and the self-check's C3, C4 and C9 on accounts made broken on
purpose. The rules themselves are golden-tested over `trays-speckle-synthetic` (its tray 3) and `dry1-0018-0153`."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from functools import cache
from typing import Any

import pytest

from pdigger_factory_sdk import InterpreterCore, Replay, StoneView, Tray, TrayFact, TrayStone, WorldLayer, WorldSpeckle
from pdigger_factory_sdk.models import Solid, SpeckleAt, SpeckleFind, SpeckleRef
from pdigger_factory_sdk.text import milli
from pdigger_factory_sdk.trays import (
    _EVIDENCE,
    CONFIDENCE,
    MOVED_CONFIDENCE,
    SPECKLE_CLEAN,
    SPECKLE_CODES,
    TrayCounts,
    TrayLedger,
    TrayPoint,
    _Question,
    _speckle_find,
    _Stone,
    _stone_evidence,
    _Tray,
    _Unid,
)

from . import synth
from .support import recording

SPECKLE = "trays-speckle-synthetic"


@cache
def speckle_trays() -> tuple[Tray, ...]:
    """The trays of `trays-speckle-synthetic`."""
    trays = tuple(Replay.open(recording(SPECKLE)).trays())
    assert len(trays) == 3
    return trays


def stone_of(t: Tray, wid: int) -> TrayStone:
    """The tray stone whose id ends with that world id (`...-3-61`)."""
    return next(s for s in t.stones if s.id.endswith(f"-{t.n}-{wid}"))


def with_facts(t: Tray, facts: Sequence[TrayFact]) -> list[str]:
    return dataclasses.replace(t, facts=tuple(facts)).check()


def with_stone(t: Tray, s: TrayStone, **kw: Any) -> list[str]:
    stones = tuple(dataclasses.replace(x, **kw) if x.id == s.id else x for x in t.stones)
    return dataclasses.replace(t, stones=stones).check()


def find(wid: int, score: float | None = None, second: float | None = None) -> SpeckleFind:
    """A find of world stone `wid`, scored `score` with `second` (not checked without them)."""
    return SpeckleFind(id=wid, score=score, second=second)


def answer(
    at: int,
    refs: Sequence[SpeckleFind] = (),
    stones: Sequence[SpeckleFind] = (),
    pairs: Sequence[tuple[int, int]] = (),
) -> WorldSpeckle:
    """An answer of the unit for its tray 1, of the reference g 4 (0:00.8) and the later moment g `at` (5 frames a
    second)."""
    return WorldSpeckle(
        v=1,
        tray=1,
        ref=SpeckleRef(g=4, t=0.8),
        at=SpeckleAt(g=at, t=at / 5),
        device="d",
        gpu_ms=1.0,
        wall_ms=1.0,
        stones=list(stones),
        refs=list(refs),
        pairs=[list(p) for p in pairs],
    )


def new_stone(first: int, *, index: int = 0, world: int | None = None, status: str = "on", **kw: Any) -> _Stone:
    """The rules' tray stone `s-{first}`, made on world stone `first` (its arrival world id), 0.3 by 0.2, standing for
    world stone `world` (`None`: lost or removed, as the rules leave a stone they lost)."""
    return _Stone(
        id=f"s-{first}",
        index=index,
        world=world,
        world_t=0.0,
        size=(0.3, 0.2),
        frozen=False,
        place=(0.0, 0.0),
        seen_t=0.0,
        born=TrayPoint(0, 0.0),
        status=status,
        first_world=first,
        **kw,
    )


def world(*ids: int) -> dict[int, StoneView]:
    """A world by id, as the rules keep the leaving point's (`_Tray.point`) and the last frame's: world stones `ids`,
    0.3 by 0.2, detected at (id / 10, 0), kept (11 held)."""
    return {
        wid: StoneView(
            id=wid,
            mode="carry" if wid == 11 else "kept",
            family="rect",
            shape="frozen",
            detected=True,
            uncertain=False,
            outline=[],
            solid=Solid(pos=[wid / 10, 0.0], q=[0.0, 0.0, 0.0, 1.0], dims=[0.3, 0.2]),
        )
        for wid in ids
    }


def speckle_fact(x: TrayFact) -> str:
    """An identified fact of the speckle: its describe(), code, the world stone it is `from` (`?`: none), margin and
    bar."""
    assert x.margin is not None
    assert x.bar is not None
    frm = "?" if x.from_world is None else str(x.from_world)
    return f"{x.describe()} {x.code} from {frm} margin {milli(x.margin)} bar {milli(x.bar)}"


def test_the_leavings_speckle_answer() -> None:
    """The synthetic speckle recording's tray 3 beyond its golden files: the identified facts of its leaving's answer
    as values, which stones the answer linked or confirmed, every stone's evidence and the confidence of every change;
    no stone has evidence while its tray is on, and trays 1 and 2, whose answers are none a leaving's, are left as
    without answers."""
    core = InterpreterCore()
    open_stones = 0
    for update in Replay.open(recording(SPECKLE)).updates(core=core):
        live = update.state.tray
        for s in () if live is None else live.stones:
            open_stones += 1
            assert (s.evidence, s.by_speckle) == (None, False), s
    trays = core.trays()
    assert len(trays) == 3
    assert open_stones > 0
    for t in trays[:2]:
        for s in t.stones:
            assert (s.by_speckle, s.evidence) == (False, "undecided"), (t.n, s)
    tr = trays[2]
    assert tr.check() == []
    leaving = tr.leaving
    assert leaving is not None

    def sid(wid: int) -> str:
        return stone_of(tr, wid).id

    identified = [x for x in tr.facts if x.kind == "identified"]
    for x in identified:
        assert x.code in SPECKLE_CODES, x
        assert (x.certain, x.g) == (None, leaving.g), x
    assert {x.stone: (x.code, x.world_id, x.from_world, x.score, x.margin, x.bar) for x in identified} == {
        sid(61): ("speckle_same", 61, None, 600, 500, 300),
        sid(62): ("speckle_same", 62, None, 620, 520, 300),
        sid(63): ("speckle_same", 63, None, 600, 480, 300),
        sid(65): ("speckle_found", 75, None, 660, 560, 300),
        sid(66): ("speckle_moved", 67, 66, 640, 540, 300),
        sid(67): ("speckle_moved", 66, 67, 630, 530, 300),
    }
    # linked or confirmed by the answer: sure, by speckle; 68's pair would have moved it, which nothing disturbed
    evidence = {
        61: "found",
        62: "found",
        63: "found",
        64: "found",
        65: "found",
        66: "found",
        67: "found",
        68: "found",
        69: "not_found",
        71: "not_found",
        72: "found",
        73: "undecided",
        78: "found",
    }
    for wid, e in evidence.items():
        s = stone_of(tr, wid)
        by_speckle = 61 <= wid <= 67 and wid != 64
        assert (s.evidence, s.by_speckle) == (e, by_speckle), (wid, s)
        if by_speckle:
            assert s.sure, (wid, s)
    s68 = stone_of(tr, 68)
    assert (s68.sure, s68.touched, s68.world_id) == (True, None, 68)
    report = tr.report()
    assert report is not None
    assert {f"{c.kind} {c.stone}": c.confidence for c in (*report.removed, *report.added, *report.moved)} == {
        f"removed {sid(64)}": CONFIDENCE[("removed", True, "found")],
        f"removed {sid(69)}": CONFIDENCE[("removed", False, "not_found")],
        f"added {sid(71)}": CONFIDENCE[("added", False, "not_found")],
        f"added {sid(72)}": CONFIDENCE[("added", False, "found")],
        f"added {sid(73)}": CONFIDENCE[("added", False, "undecided")],
        f"added {sid(78)}": CONFIDENCE[("added", False, "found")],
        f"moved {sid(62)}": MOVED_CONFIDENCE,
        f"moved {sid(65)}": MOVED_CONFIDENCE,
        f"moved {sid(66)}": MOVED_CONFIDENCE,
        f"moved {sid(67)}": MOVED_CONFIDENCE,
    }
    # the leaving's answer, read at g 123 after the leaving point and before the tray left, as its stones hold it (the
    # stones added as it left hold none)
    read = core.speckle()[-1]
    assert (read.g, read.tray, read.at.g) == (123, 3, leaving.g)
    assert all(read.stone(s.id) == s.speckle for s in tr.stones)
    assert read.to_dict()["stones"] == [s.speckle.to_dict() for s in tr.stones if s.speckle is not None]


def test_the_leavings_answer_is_the_newest_near_the_leaving_point() -> None:
    """Of the answers read while the tray was on, the newest whose later moment lies within SPECKLE_NEAR frames of the
    leaving point; none when none does."""
    near, far, newer, later = answer(31), answer(27), answer(28), answer(33)
    tr = _Tray(n=1)
    assert TrayLedger._leaving_answer(tr, TrayPoint(30, 6.0)) is None  # no answers
    tr.answers = [near, far, newer, later]
    cases = {30: newer, 33: later, 29: newer, 26: newer, 25: far, 36: None, 20: None}
    for g, want in cases.items():
        assert TrayLedger._leaving_answer(tr, TrayPoint(g, g / 5)) is want, g


def test_which_pairs_link_a_tray_stone() -> None:
    """A tray at its leaving and an answer with a pair for each rule of 4.6.13, in the answer's order: a pair links a
    stone the tray arrived with (on the tray or lost) to a world stone of the leaving point's world (or of the last
    frame's) that is not held, never a world stone or a stone twice, never to move a trusted stone or to take a
    trusted stone's world stone, and only with a scored find (but for a sure stone that stays, which needs no fact);
    then the links it makes and the facts it gives."""
    ref = TrayPoint(30, 6.0)
    tr = _Tray(n=1, k=4)

    def stone(wid: int, status: str, doubted: bool, **kw: Any) -> _Stone:
        """Tray stone `s-{wid}`, linked to world stone `wid` at the arrival, and now while it is on the tray."""
        on = status == "on"
        s = new_stone(wid, index=len(tr.stones), world=wid if on else None, status=status, doubted=doubted, **kw)
        tr.stones.append(s)
        tr.arrival_links[wid] = s.id
        if on:
            tr.link[wid] = s
        return s

    def unseen() -> _Question:
        return _Question("unseen", "not seen at two calm moments", 0, TrayPoint(0, 0.0))

    a = stone(1, "on", False)  # trusted
    pushed = _Question("pushed", "it was pushed", 21, TrayPoint(20, 4.0))
    b = stone(2, "on", True, question=pushed)  # doubted, in question
    lost = stone(3, "lost", False)
    stone(4, "removed", False)
    e = stone(5, "on", False)  # trusted
    stone(6, "on", True, added=True)  # never among the arrival links on a real tray
    stone(7, "on", True)  # no scored find for its pair
    stone(8, "on", True)  # its pair's later find not checked
    asked = stone(9, "on", True, question=unseen())  # in question on its pair's world stone
    unsure = stone(15, "on", True, sure=False)  # not sure on its pair's world stone
    stays = stone(16, "on", True, question=unseen())  # in question on its pair's world stone, with no find
    tr.unid[10] = _Unid(born=TrayPoint(25, 5.0), place=(1.0, 0.0), size=(0.3, 0.2))
    tr.point = world(1, 2, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16)  # the leaving point's: 12 only in the last frame's
    last = world(1, 2, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16)
    sp = answer(
        30,
        refs=[
            find(1, 0.6, 0.1),
            find(2, 0.7, 0.1),
            find(3, 0.4, 0.3),
            find(7),
            find(9, 0.65, 0.2),
            find(15, 0.55, 0.05),
        ],
        stones=[find(10, 0.5, 0.1), find(12, 0.6, 0.1), find(14)],
        pairs=[
            (10, 99),  # no stone linked to 99 at the arrival
            (10, 6),  # an added stone
            (10, 4),  # a removed stone
            (20, 2),  # no world stone 20, in either world
            (11, 2),  # 11 is held
            (10, 1),  # 1 is trusted, on another world stone
            (5, 2),  # 5 is the trusted stone e's
            (12, 3),  # links the lost stone; its find is not clean, so the later find is the pair's
            (10, 2),  # links b
            (10, 3),  # 10 is taken, and so is the lost stone
            (13, 2),  # b is taken
            (13, 7),  # no find scored: 7 not checked, 13 not in the later finds
            (14, 8),  # no find scored: 8 not in the arrival finds, 14 not checked
            (1, 1),  # a stays where it is
            (9, 9),  # in question, stays: its question ends
            (15, 15),  # not sure, stays: sure again
            (16, 16),  # in question, stays, with no find: its question ends, no fact needs one
        ],
    )
    ledger = TrayLedger(TrayCounts(), 1, "v", lambda t: None)
    links = [
        f"{s.id} {c} {'-' if x is None else x.id} {st}" for s, c, x, st, _ in ledger._speckle_targets(tr, sp, last)
    ]
    assert links == ["s-3 12 12 lost", "s-2 10 2 on", "s-1 1 1 on", "s-9 9 9 on", "s-15 15 15 on", "s-16 16 - on"]
    tr.answers = [sp]
    out: list[TrayFact] = []
    ledger._speckle_leaving(tr, ref, last, out)
    assert tr.leaving_answer is sp
    assert [speckle_fact(x) for x in out] == [
        "0:06.0 tray 1: stone s-3 found again by its speckle on world stone 12 (0.600) speckle_found from ? "
        "margin 0.500 bar 0.300",
        "0:06.0 tray 1: stone s-2 recognised by its speckle on world stone 10 (0.700) speckle_moved from 2 "
        "margin 0.600 bar 0.300",
        "0:06.0 tray 1: stone s-15 confirmed by its speckle on world stone 15 (0.550) speckle_same from ? "
        "margin 0.500 bar 0.300",
    ]
    # the lost stone is on 12 as the last frame shows it; b on 10, which it takes with its first sight; b's world
    # stone 2, on the board, not held and in no pair, is unidentified again (freed)
    assert tr.link[12] is lost
    assert (lost.status, lost.world, lost.world_t, lost.seen_t, lost.touched) == ("on", 12, 6.0, 6.0, 30)
    assert (lost.place, lost.at_k, lost.at) == ((1.2, 0.0), 4, (1.2, 0.0))
    assert tr.link[10] is b
    assert (b.world, b.world_t, b.question) == (10, 5.0, None)
    assert 2 not in tr.link
    assert 10 not in tr.unid
    freed = tr.unid[2]
    assert (freed.freed, freed.born, freed.place, freed.size) == (True, ref, (0.2, 0.0), (0.3, 0.2))
    linked = (lost, b, a, asked, unsure, stays)
    for s in tr.stones:
        paired = any(s is z for z in linked)
        assert s.by_speckle is paired, s.id
        if paired:
            assert (s.sure, s.question) == (True, None), s.id
    # the trusted stones
    assert not e.by_speckle
    assert tr.link[5] is e
    assert (a.touched, asked.touched, stays.touched) == (None, None, None)


def speckle_scene(held: bool) -> Tray:
    """A tray at 5 frames a second: stones 1 (x -0.6), 2 (x 0) and 3 (x 0.6) arrive at g 4; a hand pushes 2 at g 15,
    so it is not sure from the settle at g 18; world stone 7 comes at (0, -0.4) at g 20, where the hand pushed 2, while
    2's box stays (held in the last frame, when `held`); calm to g 30, the leaving point; g 31 moves and carries the
    answer, read while the tray is on: 1, 2 and 3 at the arrival are 1, 7 and 3 at g 30."""
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 1, "dry1", closed.append)
    for g in range(31):
        stones = [synth.stone(1, -0.6), synth.stone(2, 0.0), synth.stone(3, 0.6)]
        events: list[dict[str, Any]] = []
        left = "free"
        if g == 15:
            events, left = [synth.ev("push", 2, "left")], "touch"
        if g >= 20:
            stones.append(synth.stone(7, 0.0, -0.4))
        if g == 20:
            events = [synth.ev("new", 7)]
        if g == 30 and held:
            stones[1]["mode"] = "carry"
        w = synth.world(g, g / 5, stones=stones, events=events, left=left)
        ledger.feed(WorldLayer.from_json(w), TrayPoint(g, g / 5))
    here = TrayPoint(31, 6.2)
    moving = synth.world(31, 6.2)
    moving["motion"] = "moving"
    ledger.feed(WorldLayer.from_json(moving), here)
    sp = answer(
        30,
        refs=[find(1, 0.6, 0.1), find(2, 0.62, 0.1), find(3, 0.6, 0.1)],
        stones=[find(1, 0.6, 0.1), find(7, 0.62, 0.1), find(3, 0.6, 0.1), find(2, 0.1, 0.05)],
        pairs=[(1, 1), (7, 2), (3, 3)],
    )
    read = ledger.speckle(sp, here, lambda g: {})
    assert read is not None
    assert read.tray == 1
    ledger.close()
    assert len(closed) == 1
    return closed[0]


@pytest.mark.parametrize("held", [False, True])
def test_a_moved_stone_frees_its_world_stone(held: bool) -> None:
    """The leaving's answer links stone 2, not sure, to world stone 7; 2's own world stone, free on the board, is
    unidentified again, so the leaving counts it as an unclear addition (cause freed) that the answer did not find at
    the arrival; held, it is not counted. Stone 2 is reported moved."""
    tr = speckle_scene(held)
    facts = [
        "0:00.8 tray 1 arrived: 3 stones",
        "0:03.6 tray 1: stone s1t0.8-1-2 is not sure any more (it was pushed)",
        "0:06.0 tray 1: stone s1t0.8-1-2 recognised by its speckle on world stone 7 (0.620)",
        "0:06.0 tray 1: stone s1t6.0-1-2 not recognised (unclear)",
        "0:06.0 tray 1 leaving: 4 stones (3 sure), 0 added, 0 removed, 1 unclear",
    ]
    report = (
        "tray 1, 0:00.8 to 0:06.0: 0 removed, 1 added, 1 moved, not complete: 1 unclear\n"
        "  0:06.0 added s1t6.0-1-2 at (0.00, 0.00) (unclear, 0.96)\n"
        "  moved s1t0.8-1-2 0.40 to (0.00, -0.40) (0.87)"
    )
    if held:
        facts = [*facts[:3], "0:06.0 tray 1 leaving: 3 stones (3 sure), 0 added, 0 removed, 0 unclear"]
        report = (
            "tray 1, 0:00.8 to 0:06.0: 0 removed, 0 added, 1 moved\n  moved s1t0.8-1-2 0.40 to (0.00, -0.40) (0.87)"
        )
    assert [x.describe() for x in tr.facts] == facts
    r = tr.report()
    assert r is not None
    assert r.describe() == report
    assert tr.check() == []
    moved = tr.stones[1]
    assert (moved.sure, moved.by_speckle, moved.world_id, moved.evidence) == (True, True, 7, "found")
    if not held:
        assert [o.cause for o in tr.open or ()] == ["freed"]
        assert tr.stones[3].evidence == "not_found"


def test_the_evidence_of_a_stone() -> None:
    """What the leaving's answer says of a world id on each side (a pair, then a clean find, then a find of
    SPECKLE_CLEAN or more, then none; the first find of an id counts), and which world id a tray stone's evidence
    reads."""
    sp = answer(
        30,
        refs=[
            find(1, 0.6, 0.1),
            find(2, 0.45, 0.3),
            find(3, 0.2, 0.1),
            find(4),
            find(5, 0.2, 0.15),
            find(1, 0.1, 0.05),
            find(8, 0.3, -1),
        ],
        stones=[find(11, 0.6, 0.1), find(12, 0.1, 0.05), find(15, 0.25, 0.2)],
        pairs=[(15, 5), (16, 6)],
    )
    for side, wid, found, evidence in [
        ("arrival", 1, "clean", "found"),  # its second find does not count
        ("arrival", 2, "alike", "undecided"),  # 0.450 is not twice 0.300: look-alikes
        ("arrival", 3, "none", "not_found"),  # checked, no find of SPECKLE_CLEAN
        ("arrival", 4, "absent", "undecided"),  # not checked
        ("arrival", 5, "paired", "found"),  # though its own find is not clean
        ("arrival", 6, "absent", "undecided"),  # paired, but not in the answer's arrival finds
        ("arrival", 8, "clean", "found"),  # 0.300 with no second place (-1)
        ("arrival", 11, "absent", "undecided"),
        ("later", 11, "clean", "found"),
        ("later", 12, "none", "not_found"),
        ("later", 15, "paired", "found"),  # paired by its later id
        ("later", 5, "absent", "undecided"),
    ]:
        got = _speckle_find(sp, side, wid)
        assert (got, _EVIDENCE[got]) == (found, evidence), (side, wid)
    assert _speckle_find(None, "arrival", 1) == "absent"  # no answer
    assert _speckle_find(sp, "later", None) == "absent"  # no world id
    tr = _Tray(n=1, leaving_answer=sp)
    for s, want in [
        (new_stone(3, world=11), "not_found"),  # arrived as 3, now on 11
        (new_stone(1, status="removed"), "found"),  # removed, found as the tray left
        (new_stone(12, world=11, added=True), "found"),  # added: on 11 as the tray left
        (new_stone(12, world=12, status="removed", added=True), "undecided"),  # removed again: not by 12's find
        (new_stone(12, world=12, status="lost", added=True), "undecided"),
    ]:
        assert _stone_evidence(tr, s) == want, s
    assert _stone_evidence(_Tray(n=1), new_stone(1, world=1)) == "undecided"  # no leaving answer


def test_check_the_leavings_speckle_facts() -> None:
    """The self-check (4.6.10) of tray 3 of `trays-speckle-synthetic`, whose stones not sure are sure again through the
    leaving's answer, on copies broken on purpose."""
    tr = speckle_trays()[2]
    assert tr.check() == []
    facts = list(tr.facts)
    i, same = next((i, x) for i, x in enumerate(facts) if x.code == "speckle_same")  # 61's, after its unsure fact
    s = stone_of(tr, 61)
    assert (same.stone, s.sure) == (s.id, True)

    def insert(*xs: TrayFact) -> list[TrayFact]:
        return [*facts[: i + 1], *xs, *facts[i + 1 :]]

    unsure = next(x for x in facts if x.kind == "unsure" and x.stone == same.stone)
    gone = next(x for x in facts if x.kind == "removed" and x.certain is True)  # 64's
    look = dataclasses.replace(same, code="look_found")
    late = dataclasses.replace(same, stone=gone.stone)
    for name, got in {
        "two identified facts of the speckle": with_facts(tr, insert(same)),
        "identified by look after unsure": with_facts(tr, [*facts[:i], look, *facts[i + 1 :]]),
        "identified by the speckle after a removal": with_facts(tr, [*facts[:-1], late, facts[-1]]),
    }.items():
        assert "C3" in got, name
    for name, got in {
        "not sure after its speckle fact": with_stone(tr, s, sure=False),
        "an unsure fact after its speckle fact": with_facts(tr, insert(unsure)),
        "sure with no speckle fact": with_facts(tr, [*facts[:i], *facts[i + 1 :]]),
    }.items():
        assert "C4" in got, name
    bad: dict[str, dict[str, Any]] = {
        "a look's bar": {"bar": 750, "score": 760},
        "below its bar": {"score": SPECKLE_CLEAN - 1},
        "certain": {"certain": True},
        "no score": {"score": None},
        "no bar": {"bar": None},
    }
    for name, change in bad.items():
        assert "C9" in with_facts(tr, [*facts[:i], dataclasses.replace(same, **change), *facts[i + 1 :]]), name
    # a speckle fact has no margin rule, unlike a look's
    assert with_facts(tr, [*facts[:i], dataclasses.replace(same, margin=10), *facts[i + 1 :]]) == []

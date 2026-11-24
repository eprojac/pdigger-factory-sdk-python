"""Presence questions (SDK-DESIGN.md 4.6.1) beyond the golden files: every tray of the four presence recordings ends as
its generator says; a stone held in question reads as one and is sure again once it is seen; a settle at once holds
only if the next calm moment shows the stone; a stone with a look chain is never held in question; held stones, hand
deletes, revisions, a crowd of stones of its size and another stone's look keep a question from settling; the leaving
counts its own frame again when something could have changed the stone; and the frames the tray rules can not read
(the core's `gap()`, a replay's `gap` and `reset` lines, a live stream's `gap` and `reset` events) end the questions
open then. The rules themselves are golden-tested over `trays-presence-synthetic` (35 trays),
`trays-presence-looks-synthetic` (8 trays, with made-up looks), `trays-presence-attacks-synthetic` (28 trays) and
`trays-presence-attacks-looks-synthetic` (7 trays, with made-up looks)."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from pdigger_factory_sdk import Frame, InterpreterCore, Replay, Tray, TrayFact, TrayQuestion, TrayStone
from pdigger_factory_sdk.models import ClockEvent
from pdigger_factory_sdk.testing import FakeFactory

from .support import client, golden, recording, recording_lines, text_of

PRESENCE = "trays-presence-synthetic"
LOOKS = "trays-presence-looks-synthetic"
ATTACKS = "trays-presence-attacks-synthetic"
ATTACK_LOOKS = "trays-presence-attacks-looks-synthetic"

ENDS: dict[str, dict[int, tuple[str, tuple[int, ...], tuple[int, ...], tuple[int, ...]]]] = {
    PRESENCE: {
        1: ("CTRL", (101, 102, 103), (), ()),
        2: ("MOT1", (202, 203), (201,), (201,)),
        3: ("MOT2", (302, 303), (301,), (301,)),
        4: ("MOT3", (402, 403), (401,), ()),
        5: ("NOW1", (502, 503), (501,), (501,)),
        6: ("GAP1", (602, 603), (601,), (601,)),
        7: ("REV1", (702, 703), (701,), (701,)),
        8: ("DROP1", (802, 803), (801, 804), ()),
        9: ("PSAME", (902,), (901, 903), (901,)),
        10: ("DROP2", (1002, 1003), (1001, 1004), ()),
        11: ("SIDE1", (1102, 1103), (1101,), (1101,)),
        12: ("T15", (1122, 1123), (1121,), (1121,)),
        13: ("T16", (1132,), (1131,), (1131,)),
        14: ("T17", (1143,), (1141, 1142), (1141,)),
        15: ("Q1", (1501, 1502, 1503), (), ()),
        16: ("Q2", (1602, 1603), (1601,), ()),
        17: ("Q3", (1702, 1703, 1704), (1701,), ()),
        18: ("QS", (1802, 1803), (1801, 1804), ()),
        19: ("T13", (1903,), (1901, 1902), ()),
        20: ("T14", (2002,), (2001,), ()),
        21: ("QP", (2102, 2103), (2101,), ()),
        22: ("PNEIGH", (2203,), (2201, 2202), (2201,)),
        23: ("LEAVE1", (2301, 2302, 2303), (), ()),
        24: ("NOARR", (2401, 2402, 2403), (), ()),
        25: ("PUSHX", (2502, 2503), (2501,), (2501,)),
        26: ("ORDER", (2603,), (2601, 2602), (2602,)),
        27: ("MINT", (2701, 2702), (2707,), ()),
        28: ("REVPUSH", (2802, 2803), (2801,), (2801,)),
        29: ("REVSAME", (2902, 2903), (2901,), (2901,)),
        30: ("CROWD", (3002, 3003), (3001,), (3001,)),
        31: ("ONCELOST", (3102, 3103), (3101,), ()),
        32: ("ONCETOUCH", (3202, 3203), (3201,), ()),
        33: ("CROWD1", (3302, 3303), (3301,), (3301,)),
        34: ("LVHAND", (3402, 3403), (3401,), ()),
        35: ("SETTLE3", (3501, 3502, 3503), (), ()),
    },
    LOOKS: {
        1: ("LK1", (102, 103), (101,), (101,)),
        2: ("LK2", (202, 203), (201,), (201,)),
        3: ("LK0", (302, 303), (301,), (301,)),
        4: ("QE", (402, 403), (401,), (401,)),
        5: ("QL1", (501, 502, 503), (), ()),
        6: ("QL2", (602, 603), (601,), (601,)),
        7: ("LKNOCH", (702, 703), (701, 704), (701, 704)),
        8: ("LKNOCH0", (801, 802, 803), (804,), (804,)),
    },
    ATTACKS: {
        1: ("CTRL", (101, 102, 103), (), ()),
        2: ("NHELD", (202, 203, 204), (201,), (201,)),
        3: ("NHELDF", (302, 303), (301,), (301,)),
        4: ("XHELDD", (402, 403), (401,), (401,)),
        5: ("DELHF", (503,), (501,), (501,)),
        6: ("DELCA", (602, 603), (601,), (601,)),
        7: ("DELHN", (702, 703), (701,), (701,)),
        8: ("REVHAND", (802, 803), (801,), (801,)),
        9: ("REVUNTR", (902, 903), (901,), (901,)),
        10: ("REVHELD", (1002, 1003), (1001,), (1001,)),
        11: ("REVLATE", (1101, 1102, 1103), (), ()),
        12: ("REVMID", (1202, 1203), (1201,), (1201,)),
        13: ("MOTFS", (1302, 1303), (1301,), (1301,)),
        14: ("MOTS1", (1402, 1403), (1401,), (1401,)),
        15: ("ONCEMOT", (1502, 1503), (1501,), ()),
        16: ("ONCEBUSY", (1602, 1603), (1601,), ()),
        17: ("ONCEGAP", (1702, 1703), (1701,), ()),
        18: ("GAPC2", (1802, 1803), (1801,), ()),
        19: ("RENSWAP", (1902, 1903), (1901, 1904), (1901, 1904)),
        20: ("SMALLSW", (2002, 2003), (2001, 2004), (2001, 2004)),
        21: ("SMALLSWD", (2101, 2102, 2103, 2104), (), ()),
        22: ("PUSHX1", (2202, 2203), (2201,), (2201,)),
        23: ("TWOQ", (2302, 2303), (2301, 2304), (2301,)),
        24: ("NEWNEXT", (2401, 2402, 2403), (), ()),
        25: ("LVBUSY", (2502, 2503), (2501,), (2501,)),
        26: ("LVHOLE", (2602, 2603), (2601,), (2601,)),
        27: ("LVUNTR", (2702, 2703), (2701,), (2701,)),
        28: ("LVPUSH", (2802,), (2801, 2803), (2801,)),
    },
    ATTACK_LOOKS: {
        1: ("LKSET", (102, 103), (101,), (101,)),
        2: ("LKONCE", (202, 203), (201,), (201,)),
        3: ("LKLIFT", (302, 303), (301,), (301,)),
        4: ("LKSWAPZ", (402, 403, 404), (401,), (401,)),
        5: ("LKSWAPZ0", (502, 503, 504), (501,), (501,)),
        6: ("LKGREY", (602, 603), (601,), (601,)),
        7: ("LKSWAPZG", (702, 703, 704), (701,), (701,)),
    },
}
"""Each tray as its generator writes it: its name there, the world ids of the stones that end sure (the leaving counts
exactly these), of those that end not sure, and of those among them whose `unsure` fact ends a question ("open since",
`opened` set); every other stone that ends not sure gets its fact at its doubt."""

CASES = [pytest.param(name, n, id=f"{name}-{n}-{end[0]}") for name, trays in ENDS.items() for n, end in trays.items()]


@cache
def left_trays(name: str) -> tuple[Tray, ...]:
    return tuple(Replay.open(recording(name)).trays())


def tray_of(name: str, n: int) -> Tray:
    return next(t for t in left_trays(name) if t.n == n)


def stone_of(t: Tray, wid: int) -> TrayStone:
    """The tray stone first seen under that world id (`...-1-101`)."""
    return next(s for s in t.stones if s.id.endswith(f"-{t.n}-{wid}"))


def unsure_facts(t: Tray) -> dict[int, TrayFact]:
    """The tray's `unsure` facts in fact order, by the world id their stone was first seen under."""
    out: dict[int, TrayFact] = {}
    for f in t.facts:
        if f.kind == "unsure" and f.stone is not None:
            out[int(f.stone.split("-")[2])] = f
    return out


def frames(name: str) -> list[Frame]:
    return [Frame.from_json(x) for x in recording_lines(name) if "g" in x and "cursor" in x]


def fed(name: str, gap_after: int | None = None) -> InterpreterCore:
    """A core fed the recording as a replay feeds it (its clock lines, its gap lines, its frames), with `gap()` after
    its `gap_after`-th frame; then its end."""
    core = InterpreterCore()
    n = 0
    for x in recording_lines(name)[1:]:
        if x.get("sse") == "clock":
            core.clock(ClockEvent.from_json(x["data"]))
        elif x.get("sse") == "gap":
            core.gap()
        else:
            core.feed(Frame.from_json(x))
            n += 1
            if n == gap_after:
                core.gap()
    core.end()
    return core


def views(name: str, n: int, wid: int) -> list[tuple[int, TrayStone, Tray]]:
    """Frame by frame while tray `n` is on: how many frames were fed, its stone first seen under `wid`, the tray."""
    core = InterpreterCore()
    out: list[tuple[int, TrayStone, Tray]] = []
    for k, frame in enumerate(frames(name), 1):
        core.feed(frame)
        live = core.trays()[-1] if core.trays() else None
        if live is not None and live.n == n and live.leaving is None:
            out.append((k, stone_of(live, wid), live))
    return out


def states(name: str, n: int, wid: int) -> list[tuple[bool, bool]]:
    """The stone's (sure, in question) frame by frame while its tray is on, each change once."""
    seen = [(s.sure, s.question is not None) for _k, s, _live in views(name, n, wid)]
    return [x for i, x in enumerate(seen) if i == 0 or x != seen[i - 1]]


SURE, ASKED, GIVEN_UP = (True, False), (False, True), (False, False)


@cache
def in_question() -> int:
    """How many frames of `trays-presence-synthetic` were fed when tray 1's stone 101 first held its question."""
    return next(k for k, s, _live in views(PRESENCE, 1, 101) if s.question is not None)


def after_the_first(trays: list[Tray] | tuple[Tray, ...]) -> str:
    """The facts of every tray but the first, as text."""
    return text_of(f for t in trays[1:] for f in t.facts)


def ended_unseen(t: Tray, wid: int) -> TrayFact:
    """The stone's `unsure` fact, which must end its presence question of the doubt at 0:04.4 (tray 1's stone 101)."""
    fact = unsure_facts(t)[wid]
    assert (fact.why, fact.code) == ("not seen at two calm moments", "unseen")
    assert fact.opened is not None
    assert fact.opened.clock == "0:04.4"
    assert not stone_of(t, wid).sure
    return fact


def edited(tmp_path: Path, name: str, change: Callable[[dict[str, Any]], dict[str, Any] | None]) -> Path:
    """A copy of the recording with every line after its header passed through `change` (None drops the line)."""
    lines = recording_lines(name)
    out = [lines[0]] + [y for x in lines[1:] if (y := change(x)) is not None]
    path = tmp_path / f"{name}.ndjson"
    path.write_text("".join(json.dumps(x) + "\n" for x in out), encoding="utf-8")
    return path


def test_the_recordings_hold_the_trays_of_their_generator() -> None:
    for name, trays in ENDS.items():
        assert [t.n for t in left_trays(name)] == list(trays)
        assert all(t.check() == [] for t in left_trays(name))


@pytest.mark.parametrize(("name", "n"), CASES)
def test_every_tray_ends_as_its_generator_says(name: str, n: int) -> None:
    """The stones that end sure are on the tray, sure, out of question and without a fact `unsure`, and the leaving
    counts exactly them; each of the others has one, which ends a question ("open since", `opened` set) exactly where
    the stone was held in question, and else comes at its doubt."""
    _, sure, not_sure, held = ENDS[name][n]
    t = tray_of(name, n)
    facts = unsure_facts(t)
    assert sorted(facts) == sorted(not_sure)
    for wid in sure:
        s = stone_of(t, wid)
        assert (s.status, s.sure, s.question) == ("on", True, None), wid
    for wid in not_sure:
        assert not stone_of(t, wid).sure, wid
        assert (facts[wid].opened is not None) == (wid in held), wid
    leaving = t.facts[-1]
    assert leaving.kind == "leaving"
    assert leaving.sure == len(sure)


def test_a_stone_held_in_question_is_not_sure_until_it_is_seen_again() -> None:
    """Tray 1: stone 101 is not seen at two calm moments while nothing could have changed it. It is in question (not
    sure, `question` set, no fact) until the world sees it again on its place, and then sure again."""
    seen = views(PRESENCE, 1, 101)
    assert states(PRESENCE, 1, 101) == [SURE, ASKED, SURE]
    _k, s, live = next(x for x in seen if x[1].question is not None)
    q = s.question
    assert q is not None
    assert q == TrayQuestion(cause="unseen", why="not seen at two calm moments", since=q.since, opened=q.opened)
    assert (q.since, q.opened.g, q.opened.clock) == (22, 22, "0:04.4")
    assert s.to_dict()["question"] == q.to_dict()
    assert not any(f.kind == "unsure" for f in live.facts)
    assert {"C1", "C4"} <= set(live.check())  # it has not left, and a question is open
    one = left_trays(PRESENCE)[0]
    assert stone_of(one, 101).sure
    assert unsure_facts(one) == {}


def test_a_question_seen_again_before_its_doubt_is_settled_at_once() -> None:
    """Tray 15: stone 1501 is seen again on its place between the two calm moments that miss it, with nothing at work:
    the doubt settles at once, so the stone is never in question; its next unseen run, which starts at that calm
    moment, is on probation, and the calm moment that shows it again settles that run too: it stays sure."""
    assert states(PRESENCE, 15, 1501) == [SURE]
    assert unsure_facts(tray_of(PRESENCE, 15)) == {}


def test_a_question_settled_on_a_calm_moment_that_does_not_show_it_is_on_probation() -> None:
    """Tray 35: stone 3501's question is settled by a sight between two calm moments, and the second does not show it:
    sure again there, its next unseen run starts there on probation, and the calm moment after, which shows the stone,
    settles that run too: it stays sure, with no fact."""
    assert states(PRESENCE, 35, 3501) == [SURE, ASKED, SURE]
    assert unsure_facts(tray_of(PRESENCE, 35)) == {}


@pytest.mark.parametrize(
    ("name", "n", "wid"),
    [(PRESENCE, 31, 3101), (PRESENCE, 32, 3201), (ATTACKS, 15, 1501), (ATTACKS, 16, 1601), (ATTACKS, 17, 1701)],
    ids=["lost", "busy-first-sight", "motion", "hands", "dropped-frames"],
)
def test_a_settle_at_once_holds_only_if_the_next_calm_moment_settles_its_run(name: str, n: int, wid: int) -> None:
    """The stone is settled at once (never in question) and is sure until the next calm moment that shows it; when its
    run on probation can not be settled there (the stone was lost meanwhile, its first sight was busy, the tray moved
    or hands worked while it was out of sight, frames were dropped), it gets its fact `unsure` there, a plain one, and
    the tray has not left yet."""
    assert states(name, n, wid) == [SURE, GIVEN_UP]
    t = tray_of(name, n)
    fact = unsure_facts(t)[wid]
    assert (fact.code, fact.why, fact.opened) == ("unseen", "not seen at two calm moments", None)
    assert t.leaving is not None
    assert fact.t < t.leaving.t


def test_a_question_keeps_its_run_and_ends_before_the_tray_leaves() -> None:
    """Tray 12: the stone is seen at a calm moment after its question opened, then a hand works while it is out of
    sight: the question keeps the run it was opened with, which that hand spoils, so it ends at the next settle and
    not when the tray leaves. Tray 14: at the leaving's step 1 the question that ends comes first, then the doubt of
    the stone after it in tray order."""
    twelve = tray_of(PRESENCE, 12)
    assert twelve.leaving is not None
    assert unsure_facts(twelve)[1121].t < twelve.leaving.t
    assert list(unsure_facts(tray_of(PRESENCE, 14))) == [1141, 1142]


def test_the_settle_goes_stone_by_stone_and_still_tests_a_later_reason() -> None:
    """Tray 26: stone 2601, first in tray order, was pushed while stone 2602 was in question and out of sight; one
    settle gives both facts, 2601's first. Tray 25: stone 2501, in question, was pushed and is still unseen: its own
    later reason ends the question at that settle, with the question's cause and why, and not when the tray leaves."""
    order = unsure_facts(tray_of(PRESENCE, 26))
    assert list(order) == [2601, 2602]
    assert order[2601].t == order[2602].t
    assert (order[2601].code, order[2601].opened) == ("pushed", None)
    assert order[2602].opened is not None
    t = tray_of(PRESENCE, 25)
    fact = unsure_facts(t)[2501]
    assert (fact.code, fact.why) == ("unseen", "not seen at two calm moments")
    assert fact.opened is not None
    assert t.leaving is not None
    assert fact.t < t.leaving.t


def test_a_stone_added_for_certain_was_last_seen_where_its_world_stone_was_first_seen() -> None:
    """Tray 27: a slab a hand set down is added for certain, then not seen at two calm moments, seen once between them:
    its last sight is its placing, where the hand worked, so the doubt is not a presence question (a plain fact)."""
    t = tray_of(PRESENCE, 27)
    kinds = [(f.kind, f.certain) for f in t.facts if f.stone is not None and f.stone.endswith("-27-2707")]
    assert kinds == [("added", True), ("unsure", None)]
    assert unsure_facts(t)[2707].opened is None


@pytest.mark.parametrize(("n", "wid"), [(30, 3001), (33, 3301)], ids=["at-the-settle", "at-its-first-sight"])
def test_a_stone_of_its_size_near_its_place_keeps_the_question_open(n: int, wid: int) -> None:
    """Trays 30 and 33: a small stone in question is seen again on its place, but another stone of its size stands
    within NEAR of that place at the settle (tray 30) or at its first sight (tray 33): a sight can not tell the two
    apart, so the question is never settled and ends when the tray leaves."""
    assert states(PRESENCE, n, wid) == [SURE, ASKED]
    t = tray_of(PRESENCE, n)
    fact = unsure_facts(t)[wid]
    assert t.leaving is not None
    assert fact.opened is not None
    assert fact.t == t.leaving.t


def test_a_look_of_another_open_stone_on_its_world_stone_ends_the_question() -> None:
    """Looks tray 7: stone 701 (no chain) is in question; its world stone is seen again on its place with the face of
    stone 704, which is in question too: the look reaches the bar against 704's chain, so the sight does not settle
    701, and its question ends at that settle. Looks tray 8: the same with a face that is not 804's (its chain scores
    low on it): 801 is settled, 804 stays in question until the tray leaves."""
    assert states(LOOKS, 7, 701) == [SURE, ASKED, GIVEN_UP]
    t = tray_of(LOOKS, 7)
    fact = unsure_facts(t)[701]
    assert fact.opened is not None
    assert t.leaving is not None
    assert fact.t < t.leaving.t
    assert stone_of(t, 701).looks == ()
    assert states(LOOKS, 8, 801) == [SURE, ASKED, SURE]
    eight = tray_of(LOOKS, 8)
    assert eight.leaving is not None
    assert unsure_facts(eight)[804].t == eight.leaving.t


@pytest.mark.parametrize(("name", "n", "wid"), [(LOOKS, 4, 401), (LOOKS, 3, 301), (ATTACK_LOOKS, 4, 401)])
def test_a_stone_with_a_chain_is_never_held_in_question(name: str, n: int, wid: int) -> None:
    """A stone with a look chain that is not seen at two calm moments gets a question only a look settles: in question
    from that doubt on, it is not settled by being seen again on its place; its fact ends the question when the tray
    leaves. Looks tray 4: still unseen at the next calm moment, the question's `since` grows to it."""
    seen = [q for _k, s, _live in views(name, n, wid) if (q := s.question) is not None]
    assert seen
    assert all(q.cause == "unseen" and q.opened == seen[0].opened for q in seen)
    assert states(name, n, wid) == [SURE, ASKED]
    t = tray_of(name, n)
    assert stone_of(t, wid).looks != ()
    fact = unsure_facts(t)[wid]
    assert fact.opened is not None
    assert fact.opened.g == seen[0].opened.g  # the views feed no clock lines: no wall there
    assert t.leaving is not None
    assert fact.t == t.leaving.t
    if (name, n) == (LOOKS, 4):
        assert len({q.since for q in seen}) == 2
        assert seen[-1].since > seen[0].since


def test_a_world_stone_held_or_taken_by_a_hand_makes_a_busy_frame(tmp_path: Path) -> None:
    """Attack trays 2 and 6: while stone 201 is in question and out of sight, the brick next to it reads `carry` (tray
    2), or the one next to 601 is deleted, why `carried-away` (tray 6), with both hands free and no hand event: busy
    frames, so neither question is settled. The same recording with that brick `kept`, and that delete why `absent`:
    both settled, every other tray the recording's."""

    def calm(x: dict[str, Any]) -> dict[str, Any]:
        for s in (x.get("world") or {}).get("stones") or []:
            if s["id"] == 204:
                s["mode"] = "kept"
        for e in (x.get("world") or {}).get("events") or []:
            if e["id"] == 604 and e.get("why") == "carried-away":
                e["why"] = "absent"
        return x

    trays = Replay.open(edited(tmp_path, ATTACKS, calm)).trays()
    for n, wid in ((2, 201), (6, 601)):
        assert not stone_of(tray_of(ATTACKS, n), wid).sure
        assert stone_of(trays[n - 1], wid).sure
        assert unsure_facts(trays[n - 1]) == {}
    rest = [t for t in trays if t.n not in (2, 6)]
    assert text_of(f for t in rest for f in t.facts) == text_of(
        f for t in left_trays(ATTACKS) if t.n not in (2, 6) for f in t.facts
    )


def test_every_revision_is_a_frame_the_rules_could_not_read(tmp_path: Path) -> None:
    """Tray 29: a frame inside stone 2901's unseen run comes again as a revision that changes nothing: the next frame
    counts as one after frames the rules could not read, so the question ends ("open since"). Without that revision
    the stone is seen again and sure, and every other tray is the recording's."""
    assert unsure_facts(tray_of(PRESENCE, 29))[2901].opened is not None
    revision = next(
        x["g"]
        for x in recording_lines(PRESENCE)
        if x.get("world", {}).get("rev") == 1 and any(s["id"] == 2901 for s in x["world"]["stones"])
    )
    trays = Replay.open(
        edited(tmp_path, PRESENCE, lambda x: None if x.get("g") == revision and x["world"].get("rev") else x)
    ).trays()
    assert stone_of(trays[28], 2901).sure
    assert unsure_facts(trays[28]) == {}
    rest = [t for t in trays if t.n != 29]
    assert text_of(f for t in rest for f in t.facts) == text_of(
        f for t in left_trays(PRESENCE) if t.n != 29 for f in t.facts
    )


@pytest.mark.parametrize("n", [25, 26, 27, 28], ids=["hands", "dropped-frames", "untrusted", "push"])
def test_the_leaving_counts_its_own_frame_again_when_something_could_have_changed_the_stone(n: int) -> None:
    """Attack trays 25-28: stone X is not detected at the last calm moment, whose frame is the tray's last, after hands
    worked, frames were dropped, a hand was flagged untrusted or a stone was pushed while it was out of sight: the
    leaving counts that frame again, so X's question opens and ends there ("open since" the leaving itself). Tray 23
    of `trays-presence-synthetic`, where nothing happened: the frame is not counted twice, the stone stays sure."""
    t = tray_of(ATTACKS, n)
    fact = unsure_facts(t)[n * 100 + 1]
    assert t.leaving is not None
    assert fact.opened == t.leaving
    assert fact.t == t.leaving.t
    assert unsure_facts(tray_of(PRESENCE, 23)) == {}


def test_the_leaving_refuses_the_question_of_a_stone_a_hand_handled_since_its_last_sight() -> None:
    """Tray 34: a hand lifted and set down stone 3401 while it was not detected, and the last calm moment, the tray's
    last frame, does not show it: the leaving counts that frame again, and the doubt there is no question (a hand
    handled it since its last sight): a plain fact at the leaving."""
    t = tray_of(PRESENCE, 34)
    fact = unsure_facts(t)[3401]
    assert t.leaving is not None
    assert (fact.code, fact.opened, fact.t) == ("unseen", None, t.leaving.t)


def test_the_core_s_gap_ends_the_questions_open_then() -> None:
    """`InterpreterCore.gap()` while stone 101 of tray 1 is in question: the next frame comes after frames the rules
    could not read, so the question ends at the next settle with the fact `unsure` ("open since" its doubt). Without
    it the stone is seen again and sure; every other tray is the recording's."""
    core = fed(PRESENCE, gap_after=in_question() + 2)
    trays = core.trays()
    ended_unseen(trays[0], 101)
    assert sorted(unsure_facts(trays[0])) == [101]
    assert after_the_first(trays) == after_the_first(left_trays(PRESENCE))
    plain = fed(PRESENCE)
    assert text_of(f for t in plain.trays() for f in t.facts) == golden(PRESENCE, "trays.txt")


def with_line(tmp_path: Path, line: dict[str, Any], after: int) -> Path:
    """A copy of `trays-presence-synthetic` with `line` after its `after`-th frame."""
    out: list[dict[str, Any]] = []
    n = 0
    for x in recording_lines(PRESENCE):
        out.append(x)
        if "g" in x and "cursor" in x:
            n += 1
            if n == after:
                out.append(line)
    path = tmp_path / f"{PRESENCE}.ndjson"
    path.write_text("".join(json.dumps(x) + "\n" for x in out), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "line",
    [
        {"sse": "reset", "data": {"boot": "0123456789", "cursor": 7}, "recv": 1794960004.8},
        {"sse": "gap", "data": {"gap": 2}},
    ],
    ids=["reset", "gap"],
)
def test_a_replay_s_gap_or_reset_line_ends_the_questions_open_then(tmp_path: Path, line: dict[str, Any]) -> None:
    trays = Replay.open(with_line(tmp_path, line, in_question() + 2)).trays()
    ended_unseen(trays[0], 101)
    assert after_the_first(trays) == after_the_first(left_trays(PRESENCE))


@pytest.mark.parametrize(
    "line",
    [
        {"sse": "reset", "data": {"boot": 5, "cursor": 7}},
        {"sse": "gap", "data": {"gap": 1.5}},
        {"sse": "gap", "data": {"gap": 2}, "recv": "late"},
    ],
    ids=["reset-boot-a-number", "gap-not-whole", "gap-recv-a-string"],
)
def test_a_gap_or_reset_line_that_does_not_decode_changes_no_tray(tmp_path: Path, line: dict[str, Any]) -> None:
    """A side line that does not decode is a line a reader does not understand (section 6): ignored."""
    replay = Replay.open(with_line(tmp_path, line, in_question() + 2))
    assert text_of(replay.tray_facts()) == golden(PRESENCE, "trays.txt")


@pytest.mark.parametrize(
    ("event", "data"), [("reset", {"boot": "other", "cursor": 3}), ("gap", {"gap": 2})], ids=["reset", "gap"]
)
async def test_a_live_stream_s_gap_or_reset_ends_the_questions_open_then(event: str, data: dict[str, Any]) -> None:
    """The Interpreter tells its core about a `gap` or `reset` event of its stream (`gap()`)."""
    fake = FakeFactory(recording=recording(PRESENCE))
    fake.inject(event, data, after=in_question() + 2)
    async with client(fake) as factory:
        interpreter = factory.interpreter(resume=False)
        async with interpreter.tray_facts() as facts:
            got = [fact async for fact in facts]
        trays = interpreter.trays()
    fact = ended_unseen(trays[0], 101)
    assert fact in got
    assert after_the_first(trays) == after_the_first(left_trays(PRESENCE))

"""The tray rules by look (SDK-DESIGN.md 4.6.3-4.6.10) beyond the golden files: the self-check finds each broken code
on accounts made broken on purpose, the open account and a live question as values, and the permille text. The rules
themselves are golden-tested over `trays-looks-synthetic` (32 trays, every rule by look) and the real excerpts."""

from __future__ import annotations

import dataclasses
import json
from functools import cache
from typing import Any

from pdigger_factory_sdk import (
    Frame,
    InterpreterCore,
    Replay,
    Tray,
    TrayCandidate,
    TrayFact,
    TrayOpen,
    TrayPoint,
    TrayQuestion,
    TrayStone,
)
from pdigger_factory_sdk.looks import look_pairing
from pdigger_factory_sdk.text import milli
from pdigger_factory_sdk.trays import check_tray

from .support import recording, recording_lines

LOOKS = "trays-looks-synthetic"


@cache
def left_trays() -> tuple[Tray, ...]:
    return tuple(Replay.open(recording(LOOKS)).trays())


def tray(n: int) -> Tray:
    return next(t for t in left_trays() if t.n == n)


def stone_of(t: Tray, wid: int) -> TrayStone:
    """The tray stone whose id ends with that world id (`...-13-301`)."""
    return next(s for s in t.stones if s.id.endswith(f"-{t.n}-{wid}"))


def with_facts(t: Tray, facts: list[TrayFact]) -> list[str]:
    return dataclasses.replace(t, facts=tuple(facts)).check()


def with_stone(t: Tray, s: TrayStone, **kw: Any) -> list[str]:
    stones = tuple(dataclasses.replace(x, **kw) if x.id == s.id else x for x in t.stones)
    return dataclasses.replace(t, stones=stones).check()


def with_entry(t: Tray, stone_id: str, **kw: Any) -> list[str]:
    """The tray with one open entry of its leaving fact changed."""
    leave = t.facts[-1]
    assert leave.open is not None
    entries = tuple(dataclasses.replace(o, **kw) if o.stone == stone_id else o for o in leave.open)
    return with_facts(t, [*t.facts[:-1], dataclasses.replace(leave, open=entries)])


def entry(t: Tray, stone_id: str) -> TrayOpen:
    assert t.open is not None
    return next(o for o in t.open if o.stone == stone_id)


def test_every_left_tray_checks_clean() -> None:
    assert [t.n for t in left_trays()] == list(range(1, 33))
    assert all(t.check() == [] for t in left_trays())
    assert all(check_tray(t) == t.check() for t in left_trays())


def test_the_certain_facts_with_looks_are_the_first_of_v1s() -> None:
    """4.6.7: on each tray the same certain facts on the same frames, up to where the tray is relinked; the copy of the
    recording without any look is v1."""

    def certain(trays: tuple[Tray, ...]) -> dict[int, list[tuple[str, str | None, int, str | None]]]:
        return {t.n: [(f.kind, f.stone, f.g, f.code) for f in t.facts if f.certain is True] for t in trays}

    looks = certain(left_trays())
    v1 = certain(tuple(Replay.open(recording(LOOKS + "-nolooks")).trays()))
    assert list(looks) == list(v1)
    assert all(looks[n] == v1[n][: len(looks[n])] for n in looks)
    held = {n: len(v1[n]) - len(looks[n]) for n in v1 if len(v1[n]) > len(looks[n])}
    # tray 2 a swap, 10 a stone linked by its look, 30 twins, 31 a look that changed (no hand took 15's X: v1
    # leaves its loss unclear too)
    assert held == {2: 1, 10: 1, 30: 3, 31: 1}


def test_c1_the_facts_run_from_arrived_to_leaving() -> None:
    t = tray(13)
    never = Tray(session=t.session, video=t.video, n=99, arrived=None, leaving=None)
    assert never.check() == []  # a tray that never arrived passes
    assert "C1" in with_facts(t, list(t.facts[:-1]))  # not left yet: no leaving fact
    assert "C1" in with_facts(t, [])
    assert "C1" in with_facts(t, [*t.facts, t.facts[-1]])  # a fact after the leaving
    assert "C1" in with_facts(t, list(t.facts[1:]))  # no arrival first


def test_c2_ids_are_unique_and_every_fact_names_a_stone_of_the_tray() -> None:
    t = tray(13)
    assert "C2" in dataclasses.replace(t, stones=(*t.stones, t.stones[0])).check()
    named = next(i for i, x in enumerate(t.facts) if x.stone is not None)
    facts = list(t.facts)
    facts[named] = dataclasses.replace(facts[named], stone="nobody")
    assert "C2" in with_facts(t, facts)


def test_c3_each_stone_s_facts_come_in_order() -> None:
    t = tray(13)
    pushed = stone_of(t, 301)  # unsure (pushed) at the leaving
    unsure = next(x for x in t.facts if x.kind == "unsure" and x.stone == pushed.id)
    i = t.facts.index(unsure)
    head, tail = list(t.facts[: i + 1]), list(t.facts[i + 1 :])
    again = dataclasses.replace(unsure, code="pushed")
    assert "C3" in with_facts(t, [*head, again, *tail])  # two unsure facts
    added = dataclasses.replace(unsure, kind="added", certain=False)
    assert "C3" in with_facts(t, [*head, added, *tail])  # an addition after its first fact
    found = dataclasses.replace(unsure, kind="identified", certain=None, score=900, bar=750)
    assert "C3" in with_facts(t, [*head, found, *tail])  # identified after unsure
    gone = dataclasses.replace(unsure, kind="removed", certain=False)
    assert "C3" in with_facts(t, [*head, gone, gone, *tail])  # two removals, and a fact after a removal
    ten = tray(10)
    removed = next(x for x in ten.facts if x.kind == "removed")
    j = ten.facts.index(removed)
    late = dataclasses.replace(removed, kind="identified", certain=None, score=900, bar=750)
    assert "C3" in with_facts(ten, [*ten.facts[: j + 1], late, *ten.facts[j + 1 :]])  # identified after removed


def test_c4_the_final_state_matches_the_facts() -> None:
    t = tray(13)
    sure = stone_of(t, 292)
    assert sure.status == "on"
    assert sure.sure
    assert "C4" in with_stone(t, sure, status="removed", world_id=None)  # removed with no removal
    assert "C4" in with_stone(t, sure, sure=False)  # not sure with no unsure fact
    question = TrayQuestion(cause="pushed", why="it was pushed", since=120, opened=TrayPoint(119, 23.8, None))
    assert "C4" in with_stone(t, sure, question=question)  # a question still open
    ten = tray(10)
    gone = next(s for s in ten.stones if s.status == "removed")
    assert "C4" in with_stone(ten, gone, certain=True)  # the removal unclear, the stone certain


def test_c5_the_counts_and_the_open_list() -> None:
    t = tray(13)
    first, leave = t.facts[0], t.facts[-1]
    assert first.stones is not None
    assert leave.sure is not None
    assert "C5" in with_facts(t, [dataclasses.replace(first, stones=first.stones + 1), *t.facts[1:]])
    assert "C5" in with_facts(t, [*t.facts[:-1], dataclasses.replace(leave, sure=leave.sure + 1)])
    assert "C5" in with_facts(t, [*t.facts[:-1], dataclasses.replace(leave, open=())])


def test_c5_one_open_entry_per_open_stone_in_tray_order() -> None:
    t = tray(13)
    leave = t.facts[-1]
    assert leave.open is not None
    assert len(leave.open) >= 2
    assert t.check() == []
    a, b, *rest = leave.open
    assert with_facts(t, [*t.facts[:-1], dataclasses.replace(leave, open=(a, b, *rest, a))]) == ["C5"]  # twice
    assert with_facts(t, [*t.facts[:-1], dataclasses.replace(leave, open=(b, a, *rest))]) == ["C5"]  # out of order
    # an id on the tray twice (C2): each stone is open or settled by itself (4.6.8), so an open copy of a sure stone
    # needs its own entry
    sure = stone_of(t, 291)
    assert sure.status == "on"
    assert sure.sure
    assert leave.stones is not None
    copy = dataclasses.replace(sure, sure=False, added=True, certain=False, world_id=99999)
    twin = dataclasses.replace(t, stones=(*t.stones, copy))
    added = next(o for o in leave.open if o.kind == "added")
    listed = dataclasses.replace(
        leave, stones=leave.stones + 1, open=(*leave.open, dataclasses.replace(added, stone=sure.id))
    )
    assert dataclasses.replace(twin, facts=(*t.facts[:-1], listed)).check() == ["C2"]
    unlisted = dataclasses.replace(leave, stones=leave.stones + 1)
    assert dataclasses.replace(twin, facts=(*t.facts[:-1], unlisted)).check() == ["C2", "C5"]


def test_c6_world_ids_are_unique_on_the_tray() -> None:
    t = tray(13)
    a, b = stone_of(t, 292), stone_of(t, 294)
    assert "C6" in with_stone(t, a, world_id=b.world_id)
    assert "C6" in with_stone(t, a, world_id=None)


def test_c7_the_open_entries_are_well_formed() -> None:
    t = tray(13)
    unsure, added = stone_of(t, 301).id, stone_of(t, 311).id
    assert "C7" in with_entry(t, unsure, cause="no such cause")
    assert "C7" in with_entry(t, unsure, look="no such look")
    assert "C7" in with_entry(t, unsure, candidates=())
    twice = entry(t, unsure).candidates
    assert "C7" in with_entry(t, unsure, candidates=(*twice, twice[0]))
    assert "C7" in with_entry(t, unsure, candidates=twice[1:])  # its own world stone is not first
    assert "C7" in with_entry(t, unsure, candidates=tuple(c for c in twice if c.kind != "gone"))
    assert "C7" in with_entry(t, added, candidates=entry(t, added).candidates[:-1])  # no "new" last
    ten = tray(10)
    removed = entry(ten, next(s.id for s in ten.stones if s.status == "removed"))
    assert "C7" in with_entry(ten, removed.stone, candidates=removed.candidates[:-1])  # no "unseen" last


def test_c8_a_candidate_is_never_a_settled_stone() -> None:
    t = tray(13)
    added = entry(t, stone_of(t, 311).id)
    sure = stone_of(t, 292)
    settled = TrayCandidate("stone", None, sure.id)
    assert "C8" in with_entry(t, added.stone, candidates=(settled, *added.candidates))
    taken = TrayCandidate("world", sure.world_id, sure.id)
    assert "C8" in with_entry(t, added.stone, candidates=(taken, *added.candidates))


def test_a_malformed_entry_still_has_its_candidates_checked() -> None:
    """An entry with a bad cause breaks C7, and its candidate naming a sure stone C8 as well: each broken code."""
    t = tray(13)
    added = entry(t, stone_of(t, 311).id)
    settled = TrayCandidate("stone", None, stone_of(t, 292).id)
    assert with_entry(t, added.stone, cause="no such cause", candidates=(settled, *added.candidates)) == ["C7", "C8"]


def test_c9_an_identified_fact_cleared_its_bar() -> None:
    t = tray(12)
    i, found = next((i, x) for i, x in enumerate(t.facts) if x.kind == "identified")
    assert found.score is not None
    assert found.bar is not None
    for bad in (
        {"score": found.bar - 1},
        {"bar": 700, "score": 720},
        {"certain": True},
        {"margin": 149},
        {"score": None},
    ):
        facts = list(t.facts)
        facts[i] = dataclasses.replace(found, **bad)
        assert "C9" in with_facts(t, facts), bad


def test_the_open_account_as_values() -> None:
    t = tray(13)
    d = t.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["t_id"] == 750
    tie = entry(t, stone_of(t, 311).id)
    assert tie.to_dict() == {
        "stone": tie.stone,
        "world_id": 311,
        "kind": "added",
        "cause": "doubtful",
        "look": "tie",
        "score": tie.score,
        "hand_near": True,
        "seen_at_arrival": False,
        "candidates": [c.to_dict() for c in tie.candidates],
    }
    assert tie.candidates[0].to_dict() == {
        "kind": "stone",
        "world_id": None,
        "stone": stone_of(t, 301).id,
        "score": tie.score,
        "why": None,
    }
    assert tie.candidates[-1].to_dict() == {"kind": "new", "world_id": None, "stone": None, "score": None, "why": None}
    assert d["open"] == [o.to_dict() for o in t.open or ()]
    assert d["stones"][[s.id for s in t.stones].index(tie.stone)]["open"] == tie.to_dict()
    assert t.facts[-1].to_dict()["open"] == d["open"]
    assert TrayCandidate("gone", why="hand").describe() == "gone (hand)"


def test_a_question_while_its_tray_is_on() -> None:
    """A stone in question is not sure while its tray is on, and holds its question; the live tray breaks C1 (it has not
    left) and C4 (a question is open) until it leaves."""
    core = InterpreterCore()
    seen: list[tuple[TrayStone, Tray]] = []
    for line in recording_lines(LOOKS):
        if "g" not in line or "cursor" not in line:
            continue
        core.feed(Frame.from_json(line))
        trays = core.trays()
        if not trays:
            continue
        live = trays[-1]
        asked = [s for s in live.stones if s.question is not None]
        if asked and live.leaving is None:
            seen.append((asked[0], live))
            break
    assert seen
    s, live = seen[0]
    q = s.question
    assert q is not None
    assert not s.sure
    assert q.cause in ("pushed", "neighbour_pushed", "found_again")
    assert q.to_dict() == {"cause": q.cause, "why": q.why, "since": q.since, "opened": q.opened.to_dict()}
    assert s.to_dict()["question"] == q.to_dict()
    assert {"C1", "C4"} <= set(live.check())


def test_permille_text_and_pairing_of_no_look() -> None:
    assert milli(853) == "0.853"
    assert milli(1000) == "1.000"
    assert milli(-5) == "-0.005"
    assert milli(-1000) == "-1.000"
    assert milli(0) == "0.000"
    assert look_pairing(None, None) is None

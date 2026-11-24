"""The unit's speckle answers (SDK-DESIGN.md 4.6.12): read by value, the tray stones that hold them, the answer that
comes after its tray left, and the answers as dicts. The golden tests compare every answer read (`speckle.txt`)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from pdigger_factory_sdk import Replay, StoneSpeckle, TraySpeckle, WorldLayer, WorldSpeckle
from pdigger_factory_sdk.interpreter import InterpreterCore

from .support import FIXTURES

ANSWER: dict[str, Any] = {
    "v": 1,
    "tray": 2.0,
    "ref": {"g": 4, "t": 0.8},
    "at": {"g": 20, "t": 4},
    "device": "d",
    "gpu_ms": 1.5,
    "wall_ms": 2,
    "stones": [{"id": 1, "at": None, "score": None, "second": None}, {"id": 2}],
    "refs": [{"id": 3, "at": 4.0, "score": 0.5, "second": -1}],
    "pairs": [[1, 3]],
}
"""A well-formed answer: whole numbers written with a fraction and finds without at, score or second still fit."""

WORLD: dict[str, Any] = {
    "schema": "stw1",
    "session": 1,
    "video": "dry1",
    "g": 7,
    "t": 1.4,
    "motion": "static",
    "logF": 0.0,
    "stones": [],
    "hands": {"left": {"state": "free"}, "right": {"state": "free"}},
    "events": [],
    "stats": {"stones": 0, "confirmed": 0, "frozen": 0, "fitted": 0, "held": 0, "stepMs": 1.0},
}


def changed(**members: Any) -> dict[str, Any]:
    """The answer with `members` replaced (a member given as `...` is left out)."""
    out = copy.deepcopy(ANSWER)
    for k, v in members.items():
        if v is ...:
            del out[k]
        else:
            out[k] = v
    return out


def test_an_answer_is_read_by_value() -> None:
    sp = WorldLayer.from_json({**WORLD, "speckle": ANSWER}).speckle
    assert isinstance(sp, WorldSpeckle)
    assert (sp.v, sp.tray, sp.ref.g, sp.ref.t, sp.at.g) == (1, 2, 4, 0.8, 20)
    assert (sp.device, sp.gpu_ms, sp.wall_ms) == ("d", 1.5, 2)
    assert [(f.id, f.at, f.score, f.second) for f in sp.stones] == [(1, None, None, None), (2, None, None, None)]
    assert [(f.id, f.at, f.score, f.second) for f in sp.refs] == [(3, 4, 0.5, -1)]
    assert [list(p) for p in sp.pairs] == [[1, 3]]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        5,
        "x",
        [],
        {},
        changed(v=2),
        changed(v=True),
        changed(tray="2"),
        changed(tray=1.5),
        changed(ref={"g": 4}),
        changed(at={"g": 1.5, "t": 0}),
        changed(at=3),
        changed(device=5),
        changed(gpu_ms="1"),
        changed(wall_ms=...),
        changed(stones={}),
        changed(stones=[5]),
        changed(refs=[{"id": 1.5}]),
        changed(refs=[{"id": 1, "at": "x"}]),
        changed(refs=[{"id": 1, "score": 0.5}]),
        changed(refs=[{"id": 1, "score": 0.5, "second": "x"}]),
        changed(pairs=[[1]]),
        changed(pairs=[5]),
        changed(pairs=[[1, 2.5]]),
        changed(pairs={}),
    ],
)
def test_an_answer_that_does_not_fit_is_dropped(bad: Any) -> None:
    world = WorldLayer.from_json({**WORLD, "speckle": bad})
    assert world.speckle is None
    assert world.g == 7


def test_the_tray_stones_hold_the_newest_answer_read_while_their_tray_was_on() -> None:
    core = InterpreterCore()
    updates = list(Replay(FIXTURES / "recordings" / "trays-speckle-synthetic.ndjson.gz").updates(core=core))
    assert [a.g for u in updates for a in u.speckle] == [24, 30, 32, 36, 46, 50, 123]  # 123: the third tray's leaving
    revised = [
        u
        for u in updates
        if u.is_revision and u.frame is not None and u.frame.world is not None and u.frame.world.speckle is not None
    ]
    assert [u.speckle for u in revised] == [[]]  # a revision's answer is not read again
    trays = core.trays()
    one, two = "20261118T000000.8Z-1-", "20261118T000007.8Z-2-"

    def held(tray: int, stone: str) -> StoneSpeckle:
        found = trays[tray].stone(stone)
        assert found is not None
        assert found.speckle is not None
        return found.speckle

    s1 = held(0, one + "1")  # g 32's
    assert (s1.found, s1.found_world, s1.clean) == (None, 1, True)
    s2 = held(0, one + "2")  # g 36's: the tray was still on
    assert (s2.score, s2.same) == (0.8, True)
    s4 = held(0, one + "4")  # g 30's, not g 46's
    assert (s4.pair, s4.same, s4.score) == (one + "5", False, 0.5)
    s13 = held(1, two + "13")
    assert (s13.side, s13.found) == ("later", two + "13")
    s12 = held(1, two + "12")
    assert (s12.pair, s12.clean) == (None, False)
    answers = core.speckle()
    late = answers[4]
    assert (len(answers), late.g, late.tray, late.unit_tray) == (7, 46, 1, 1)
    assert late.stone(one + "4") is not None
    assert late.stone(one + "1") is None
    assert isinstance(late, TraySpeckle)
    assert late.wall is not None
    d = late.to_dict()
    assert (d["stones"][0]["side"], d["ref"]) == ("arrival", {"g": 4, "t": 0.8})
    stone = trays[0].stone(one + "4")
    assert stone is not None
    assert stone.to_dict()["speckle"] == s4.to_dict()

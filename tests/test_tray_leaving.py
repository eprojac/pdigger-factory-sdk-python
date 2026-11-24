"""Version 5 of the tray rules (SDK-DESIGN.md 4.6.1, Leaving): the leaving pairs a lost tray stone S with an
unidentified world stone W only on evidence. Looks that agree (the tray's bar over at least N_ID cells) pair them and
looks that disagree (below T_DIFF over at least N_ID cells) keep them apart, whatever the time and the hands; looks in
between, too few cells or no look leave it to the time and the hands: W first seen no earlier than RENAME_T before S's
last sight, and no hand mark near S's place or W's since the earliest of S's last sight, S's loss and W's first sight,
less RENAME_T.

Version 7 (4.6.1, Positive evidence at departure): an unidentified world stone that the leaving point's world keeps
(lists, not detected), not held, with less than OVERLAP of its box under one other unheld stone's box there, is counted
as an unclear addition at its place there."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from pdigger_factory_sdk import Tray
from pdigger_factory_sdk.models import WorldLayer
from pdigger_factory_sdk.trays import HAND_R, N_ID, RENAME_T, T_DIFF, TrayCounts, TrayLedger, TrayPoint

from .support import recording_lines

BAR = 700  # the tray's bar for "the looks agree", stubbed


@pytest.fixture
def same(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`same(score, born=..., mark=..., chain=..., stored=...)`: the leaving's verdict on S (last seen at 10.0, lost at
    10.4, at (0, 0)) and W (at (0.1, 0)), with S's chain scoring `score` against W's stored look."""
    score: list[tuple[int, int] | None] = [None]
    monkeypatch.setattr(TrayLedger, "_chain_score", lambda self, tr, s, look: score[0])
    monkeypatch.setattr(TrayLedger, "_t_id", staticmethod(lambda tr: BAR))
    ledger = TrayLedger(TrayCounts(), 1, "dry1", lambda tray: None)

    def verdict(
        x: tuple[int, int] | None,
        born: float = 7.2,
        mark: tuple[float, tuple[float, float]] | None = None,
        chain: bool = True,
        stored: bool = True,
    ) -> bool:
        score[0] = x
        tr: Any = SimpleNamespace(store={7: object()} if stored else {}, marks=[] if mark is None else [mark])
        s: Any = SimpleNamespace(chain=[object()] if chain else [], seen_t=10.0, lost_t=10.4, place=(0.0, 0.0))
        u: Any = SimpleNamespace(born=SimpleNamespace(t=born), place=(0.1, 0.0))
        return ledger._same_stone(tr, s, 7, u)

    return verdict


def test_looks_that_agree_pair_whatever_the_time_and_the_hands(same: Any) -> None:
    assert same((BAR, N_ID))
    assert same((BAR, N_ID), born=1.0, mark=(9.0, (0.0, 0.0)))


def test_looks_that_disagree_never_pair(same: Any) -> None:
    assert not same((T_DIFF - 1, N_ID))
    assert not same((T_DIFF - 1, N_ID), born=9.0)


@pytest.mark.parametrize("x", [(T_DIFF, N_ID), (BAR - 1, N_ID), (T_DIFF - 1, N_ID - 1), (BAR, N_ID - 1), None])
def test_otherwise_the_time_and_the_hands_decide(same: Any, x: tuple[int, int] | None) -> None:
    """In between, too few cells or no score: W first seen 2.8 s before S's last sight with no hand near pairs; first
    seen earlier than RENAME_T before it, or a hand mark near either place since the window opened (4.2 s), does not."""
    assert same(x)
    assert same(x, born=10.0 - RENAME_T)
    assert not same(x, born=10.0 - RENAME_T - 0.1)
    assert not same(x, mark=(9.0, (HAND_R - 0.05, 0.0)))  # near S's place
    assert not same(x, mark=(9.0, (0.1 + HAND_R - 0.05, 0.0)))  # near W's place only
    assert same(x, mark=(9.0, (0.1 + HAND_R + 0.05, 0.0)))  # near neither
    assert same(x, mark=(4.1, (0.0, 0.0)))  # before the window: min(10.0, 10.4, 7.2) - RENAME_T = 4.2
    assert not same(x, mark=(4.3, (0.0, 0.0)))


def test_no_chain_or_no_stored_look_is_no_score(same: Any) -> None:
    """Without S's chain or W's stored look nothing is scored, so even a disagreeing score is never read."""
    assert same((T_DIFF - 1, N_ID), chain=False)
    assert same((T_DIFF - 1, N_ID), stored=False)
    assert not same((T_DIFF - 1, N_ID), chain=False, born=1.0)


# A neighbour 5 of stone 3: its mode from g 12 on, and its place across from 3's (0, 0); its box (0.3 by 0.2, as 3's)
# then covers 6 (y 0.09: 0.24) or 8 (y 0.05: 0.32) of the 25 points of 3's box.
KEPT = {
    "alone": None,
    "beside_kept": ("kept", 0.09),
    "under_kept": ("kept", 0.05),
    "under_seen": ("seen", 0.05),
    "under_held": ("carry", 0.05),
    "held": None,
    "moved_later": None,
    "rival_later": ("seen", 0.05),
}


def _kept_scene(case: str) -> Tray:
    """Stones 1 and 2 arrive with the tray; world stone 3 comes at g 10, boxed at g 10 and 11, then kept (held, in case
    `held`), and in some cases a neighbour 5 comes with it. The tray leaves at its last frame, g 17, a calm one; in the
    `_later` cases a new event at g 15 makes g 14 the leaving point, after which 3 moves 0.05 or 5 first comes."""
    base = next(row["world"] for row in recording_lines("trays-synthetic") if "world" in row)
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 1, "sample", closed.append)
    for g in range(18):
        w = deepcopy(base)
        later = case.endswith("_later") and g >= 15
        w.update(g=g, t=g * 0.2, motion="static", events=[{"type": "new", "id": 100}] if later and g == 15 else [])
        a, b = w["stones"][:2]
        a["solid"]["pos"][0], b["solid"]["pos"][0] = -0.6, 0.6
        w["stones"] = [a, b]
        if g >= 10:
            stone = deepcopy(a)
            stone["id"], stone["solid"]["pos"][0] = 3, 0.05 if later and case == "moved_later" else 0.0
            stone["detected"] = g < 12
            stone["mode"] = "seen" if g < 12 else "carry" if case == "held" else "kept"
            w["stones"].append(stone)
            neighbour = KEPT[case]
            if neighbour is not None and (case != "rival_later" or later):
                mode, y = neighbour
                other = deepcopy(a)
                other["id"], other["solid"]["pos"][:2] = 5, [0.20, y]
                other["mode"] = "seen" if g < 12 else mode
                other["detected"] = g < 12 or mode == "seen"
                w["stones"].append(other)
        for key in ("stones", "confirmed", "frozen", "fitted"):
            w["stats"][key] = len(w["stones"])
        ledger.feed(WorldLayer.from_json(w), TrayPoint(g, g * 0.2, None))
    ledger.close()
    return closed[0]


@pytest.mark.parametrize(
    ("case", "counted"),
    [
        ("alone", {3: (0.0, 0.0)}),
        ("beside_kept", {3: (0.0, 0.0), 5: (0.2, 0.09)}),  # 0.24 of each box under the other
        ("under_kept", {}),  # 0.32: a kept stone hides one
        ("under_seen", {5: (0.2, 0.05)}),  # so does a detected one, itself counted by its box on the last frame
        ("under_held", {3: (0.0, 0.0)}),  # a held stone hides none, and is not counted itself
        ("held", {}),
        ("moved_later", {3: (0.0, 0.0)}),  # its place at the leaving point, not at the last frame
        ("rival_later", {3: (0.0, 0.0)}),  # the stones of the leaving point hide it, not the last frame's
    ],
)
def test_a_kept_stone_at_the_leaving_point_is_a_possible_addition(
    case: str, counted: dict[int, tuple[float, float]]
) -> None:
    tray = _kept_scene(case)
    leaving = tray.facts[-1]
    assert leaving.g == (14 if case.endswith("_later") else 17)
    added = {s.world_id: s for s in tray.stones if s.added}
    assert {wid: s.place for wid, s in added.items()} == counted
    for s in added.values():
        assert (s.sure, s.certain) == (False, False)
        assert [f.code for f in tray.facts if f.stone == s.id] == ["not_recognised"]
    # only a detected stone has a place seen at the leaving point
    seen = {5: (0.2, 0.05)} if case == "under_seen" else {}
    assert {wid: s.left_place for wid, s in added.items()} == {wid: seen.get(wid) for wid in counted}
    assert (leaving.stones, leaving.sure, leaving.added, leaving.unclear) == (2 + len(counted), 2, 0, len(counted))


def _stone(
    seed: dict[str, Any],
    wid: int,
    x: float,
    y: float = 0.0,
    mode: str = "seen",
    detected: bool | None = None,
    dims: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """A copy of `seed` as world stone `wid` at (x, y), in `mode`, detected when it is seen unless `detected` says."""
    s = deepcopy(seed)
    s["id"], s["solid"]["pos"][:2] = wid, [x, y]
    s["mode"], s["detected"] = mode, mode == "seen" if detected is None else detected
    if dims is not None:
        s["solid"]["dims"][:2] = list(dims)
    return s


# Where the leaving reads the leaving point's world, and which of its stones hide a kept one: the case, its leaving g,
# its leaving fact's (stones, sure, added, removed, unclear), and each stone counted as a possible addition: its world
# id, (place, size, left_place).
LEAVING: dict[str, tuple[int, tuple[int, int, int, int, int], dict[int, Any]]] = {
    # a hand works at g 15, after the calm g 14: the leaving point is the last frame, g 17, where 3 is kept at x 0.05
    "worked": (17, (3, 2, 0, 0, 1), {3: ((0.05, 0.0), (0.3, 0.2), None)}),
    # the same, with a stone 5 boxed over 0.32 of 3's box from g 15: 5 is counted (boxed on the last frame), 3 is hidden
    "worked_under_seen": (17, (3, 2, 0, 0, 1), {5: ((0.2, 0.05), (0.3, 0.2), (0.2, 0.05))}),
    # boxed but held at the leaving point g 14, kept after: neither version 6 nor version 7 counts it
    "held_seen": (14, (2, 2, 0, 0, 0), {}),
    # boxed at the leaving point g 14 with 0.32 of its box under a kept 5: version 6 counts it whatever the overlap (and
    # 5, under 3, is hidden)
    "seen_under_kept": (14, (3, 2, 0, 0, 1), {3: ((0.0, 0.0), (0.3, 0.2), (0.0, 0.0))}),
    # held only after the leaving point: counted, the leaving point's row decides
    "held_later": (14, (3, 2, 0, 0, 1), {3: ((0.0, 0.0), (0.3, 0.2), None)}),
    # its box grows to 0.36 after the leaving point: counted with the leaving point's size
    "resized_later": (14, (3, 2, 0, 0, 1), {3: ((0.0, 0.0), (0.3, 0.2), None)}),
    # 0.32 of its box under a tray stone's (stone 1, there since the arrival, itself doubted for the overlap): hidden
    "under_linked": (17, (2, 1, 0, 0, 0), {}),
    # 0.40 of its box under the box of a stone off the board (never on the tray): hidden all the same
    "under_offboard": (17, (2, 2, 0, 0, 0), {}),
    # 0.32 under a kept 5 at the leaving point g 14 that is gone by the last frame: hidden (and 5 is not on the tray)
    "gone_later": (14, (2, 2, 0, 0, 0), {}),
    # kept in another mode (transit, not detected): the world keeps it on the tray all the same
    "transit": (14, (3, 2, 0, 0, 1), {3: ((0.0, 0.0), (0.3, 0.2), None)}),
}


def _leaving_scene(case: str) -> Tray:
    """Stones 1 and 2 arrive with the tray (1 at (0.2, 0.05) in case under_linked); world stone 3 comes at g 10 (at
    (0.95, 0) in case under_offboard, beside stone 9 off the board), boxed at g 10 and 11, then kept, unless the case
    says otherwise. A hand event at g 15 (the worked cases) makes the last frame, g 17, the leaving point; a `new` event
    there (the others but under_linked and under_offboard) makes it the calm g 14; with neither it is g 17, calm too."""
    seed = next(row["world"] for row in recording_lines("trays-synthetic") if "world" in row)
    closed: list[Tray] = []
    ledger = TrayLedger(TrayCounts(), 1, "sample", closed.append)
    worked = case.startswith("worked")
    for g in range(18):
        w = deepcopy(seed)
        later = g >= 15
        events: list[dict[str, Any]] = []
        if g == 15 and worked:
            events = [{"type": "place", "side": "left", "id": 1}]
        elif g == 15 and case not in ("under_linked", "under_offboard"):
            events = [{"type": "new", "id": 100}]
        one = _stone(seed["stones"][0], 1, 0.2, 0.05) if case == "under_linked" else _stone(seed["stones"][0], 1, -0.6)
        stones = [one, _stone(seed["stones"][0], 2, 0.6)]
        if g >= 10:
            mode, detected = ("seen", True) if g < 12 else ("kept", False)
            if case == "held_seen" and 12 <= g <= 14:
                mode, detected = "carry", True
            if case == "seen_under_kept" and g <= 14:
                mode, detected = "seen", True
            if case == "held_later" and later:
                mode, detected = "carry", False
            if case == "transit" and g >= 12:
                mode, detected = "transit", False
            x = 0.95 if case == "under_offboard" else 0.05 if later and case == "worked" else 0.0
            dims = (0.36, 0.2) if case == "resized_later" and later else None
            stones.append(_stone(seed["stones"][0], 3, x, 0.0, mode, detected, dims))
            if case == "worked_under_seen" and later:
                stones.append(_stone(seed["stones"][0], 5, 0.2, 0.05))
            if case == "seen_under_kept" or (case == "gone_later" and not later):
                stones.append(_stone(seed["stones"][0], 5, 0.2, 0.05, "seen" if g < 12 else "kept"))
            if case == "under_offboard":
                stones.append(_stone(seed["stones"][0], 9, 1.15))
        w.update(g=g, t=g * 0.2, motion="static", events=events, stones=stones)
        for key in ("stones", "confirmed", "frozen", "fitted"):
            w["stats"][key] = len(stones)
        ledger.feed(WorldLayer.from_json(w), TrayPoint(g, g * 0.2, None))
    ledger.close()
    return closed[0]


@pytest.mark.parametrize("case", list(LEAVING))
def test_the_leaving_point_world_decides_what_is_counted(case: str) -> None:
    leave_g, counts, counted = LEAVING[case]
    tray = _leaving_scene(case)
    leaving = tray.facts[-1]
    assert leaving.g == leave_g
    assert (leaving.stones, leaving.sure, leaving.added, leaving.removed, leaving.unclear) == counts
    assert {s.world_id: (s.place, s.size, s.left_place) for s in tray.stones if s.added} == counted
    for s in tray.stones:
        if s.added:
            assert (s.sure, s.certain) == (False, False)
            assert [f.code for f in tray.facts if f.stone == s.id] == ["not_recognised"]

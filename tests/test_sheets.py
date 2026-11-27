"""Sheet actions across revisions, board changes and an ordinary hand removal."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pdigger_factory_sdk import Frame, InterpreterCore, Replay, Summary
from pdigger_factory_sdk.interpreter import event_key
from pdigger_factory_sdk.models import WorldEvent
from pdigger_factory_sdk.testing import FakeFactory
from pdigger_factory_sdk.text import event_sentence

from .support import client, recording, recording_lines

CASE = "trays-sheet-synthetic"
PARTIAL = "trays-sheet-partial-synthetic"


def test_revision_keeps_actions_and_distinct_stones() -> None:
    result = Replay(recording(CASE)).interpret()
    at = [e for e in result.events if e.g == 45 and e.type == "sheet"]
    assert [(e.why, e.ids) for e in at] == [("laid", [11, 12]), ("laid", [13]), ("lifted", [13])]
    assert len({e.key for e in at}) == 3
    assert event_key(WorldEvent.from_json({"type": "sheet", "why": "laid", "ids": [12, 11, 11]})) == at[0].key
    assert at[0].to_dict()["ids"] == [11, 12]
    assert event_sentence("sheet", None, why="laid", ids=[]) == "a sheet is laid"
    assert event_sentence("sheet", None, why="lifted", ids=[1]) == "a sheet is lifted off stone 1"
    assert event_sentence("sheet", None, why="folded") == "a sheet event (folded)"
    assert event_sentence("sheet", None) == "a sheet event"
    assert event_sentence("sheet", None, why="laid", ids=list(range(1, 9))) == (
        "a sheet is laid over stones 1, 2, 3, 4, 5, 6 and 2 more"
    )
    assert result.summary.events["sheet"] == 8
    assert Summary.from_dict(result.summary.to_dict()).describe() == result.summary.describe()
    old = Replay(recording(CASE + "-nosheet")).summary().to_dict()
    assert "sheet" not in old["events"]
    assert Summary.from_dict(old).events["sheet"] == 0


def test_sheets_leave_with_their_board() -> None:
    result = Replay(recording(CASE)).interpret()
    first, second, third = result.trays
    assert all(tray.check() == [] for tray in result.trays)
    for tray, under, departure in ((first, 4, 28), (second, 2, 56), (third, 2, 98)):
        report = tray.report()
        assert report
        assert report.left.g == departure
        assert len(report.under_sheet) == tray.facts[-1].under_sheet == under
        assert all(s.sure and s.sheet_since and s.to_dict()["under_sheet"] for s in tray.stones if s.under_sheet)
        assert report.to_dict()["sheet_facts"] == [f.to_dict() for f in report.sheet_facts]
        assert tuple(sorted(s.id for s in tray.stones if s.under_sheet)) == report.under_sheet
    first_report, second_report, third_report = (tray.report() for tray in result.trays)
    assert first_report
    assert first_report.complete
    assert first_report.sheets == 2
    assert not first_report.removed
    assert second_report
    assert [f.code for f in second_report.sheet_facts] == ["sheet_laid", "sheet_lifted"]
    assert len(second_report.removed) == 1
    assert second_report.removed[0].stone.endswith("-14")
    assert all(s.world_id != 5 for s in second.stones)
    assert third_report
    assert third_report.complete
    assert third_report.removed[0].code == "carried_away"
    assert third_report.removed[0].certain
    assert result.summary.trays.sheets == 4
    assert result.summary.trays.under_sheet == 8


def test_clear_sight_ends_cover_on_a_continuing_board(tmp_path: Path) -> None:
    lines = recording_lines(CASE)
    for row in lines:
        if row.get("g") in (17, 18):
            stone = next(s for s in row["world"]["stones"] if s["id"] == 1)
            stone.update(under_sheet=False, detected=True, mode="seen")
    path = tmp_path / "cleared.ndjson"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    first = Replay(path).trays()[0]
    facts = [(f.g, f.code) for f in first.facts if f.kind == "sheet"]
    assert facts == [(14, "sheet_laid"), (17, "sheet_lifted"), (19, "sheet_laid"), (20, "sheet_laid")]
    assert first.check() == []


def test_partial_lifts_keep_the_board_and_allow_a_new_world_id() -> None:
    first, second = Replay(recording(PARTIAL)).trays()
    assert first.check() == second.check() == []
    assert first.leaving
    assert first.leaving.g == 35
    assert second.arrived
    assert second.arrived.g == 42
    assert second.leaving
    assert second.leaving.g == 67
    assert [(f.g, f.code, f.stones) for f in second.facts if f.kind == "sheet"] == [
        (52, "sheet_laid", 6),
        (56, "sheet_lifted", 4),
    ]
    assert len(second.stones) == 6
    report = first.report()
    assert report
    assert not report.removed
    assert not report.added
    assert [(f.g, f.code) for f in report.sheet_facts] == [
        (14, "sheet_laid"),
        (20, "sheet_lifted"),
        (26, "sheet_lifted"),
    ]
    renamed, unseen, *covered = first.stones
    assert renamed.sure
    assert renamed.world_id == 41
    assert unseen.status == "on"
    assert not unseen.sure
    assert unseen.world_id is None
    assert unseen.open
    assert [c.kind for c in unseen.open.candidates] == ["unseen", "gone"]
    assert all(s.sure and s.under_sheet for s in covered)
    assert "C6" in replace(first, stones=(renamed, replace(unseen, under_sheet=True), *covered)).check()


def test_exposed_faces_keep_the_same_bounded_board_during_a_partial_lift() -> None:
    rectangle = (0.0, 0.0, 1920.0, 1080.0)

    def leaving(*, bounded: bool = True, missing: bool = False, displaced: bool = False, gap: bool = False) -> int:
        core = InterpreterCore(tray_bounds=rectangle if bounded else None)
        for raw in recording_lines(PARTIAL):
            if "world" not in raw or raw["g"] > 31:
                continue
            grid, world = raw["g"], raw["world"]
            if grid == 19 and gap:
                continue
            for s in world["stones"]:
                if s["id"] in (3, 4):
                    s.update(under_sheet=False, mode="seen", detected=True)
                    if s["id"] == 4 and grid == 20:
                        if missing:
                            s.update(detected=False, mode="kept")
                        if displaced:
                            s["solid"]["pos"][0] += 0.4
            if grid == 14:
                world["events"][0]["ids"] = [1, 2]
            core.feed(Frame.from_json(raw))
        core.end()
        first = core.trays()[0]
        assert first.leaving is not None
        return first.leaving.g

    assert leaving() > 26
    assert leaving(bounded=False) == 20
    assert leaving(missing=True) == 20
    assert leaving(displaced=True) == 20
    assert leaving(gap=True) == 20


def test_partial_lift_of_an_already_uncertain_stone(tmp_path: Path) -> None:
    rows = recording_lines(PARTIAL)
    for row in rows:
        if 5 <= row.get("g", -1) < 14:
            stones = row["world"]["stones"]
            stones[1]["solid"]["pos"] = stones[0]["solid"]["pos"]
            if row["g"] == 5:
                row["world"]["events"].append({"type": "push", "id": 1, "side": "right"})
    path = tmp_path / "overlap.ndjson"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    first = Replay(path).trays()[0]
    unseen = first.stones[1]
    assert unseen.open
    assert unseen.open.cause == "overlap"
    assert unseen.world_id is None
    assert [f.code for f in first.facts if f.kind == "unsure" and f.stone == unseen.id] == ["overlap"]
    assert first.check() == []


def test_partial_lift_closes_on_a_gap_or_displaced_covered_track(tmp_path: Path) -> None:
    for condition in ("gap", "displaced"):
        frames = recording_lines(PARTIAL)
        if condition == "gap":
            frames = [row for row in frames if row.get("g") != 55]
        else:
            frame = next(row for row in frames if row.get("g") == 56)
            anchor = next(s for s in frame["world"]["stones"] if s["id"] == 13)
            anchor["solid"]["pos"][0] += 0.5
        path = tmp_path / (condition + ".ndjson")
        path.write_text("".join(json.dumps(row) + "\n" for row in frames), encoding="utf-8")
        tray = Replay(path).trays()[1]
        assert tray.leaving
        assert tray.leaving.g == 56
        assert tray.check() == []


def test_sheet_self_check_detects_corrupt_actions_and_departures() -> None:
    first = Replay(recording(CASE)).trays()[0]
    arrival, laid, again, left = first.facts
    assert replace(first, facts=(arrival, replace(laid, stones=3), again, left)).check() == ["C10"]
    assert replace(first, facts=(arrival, replace(laid, tray_stones=laid.tray_stones[::-1]), again, left)).check() == [
        "C10"
    ]
    assert replace(first, facts=(arrival, replace(laid, g=arrival.g - 1), again, left)).check() == ["C10"]
    assert replace(first, facts=(arrival, replace(laid, tray_stones=("unknown",)), again, left)).check() == ["C10"]
    assert "C11" in replace(first, facts=(arrival, laid, again, replace(left, stones=1))).check()
    assert replace(first, facts=(arrival, laid, laid, again, left)).check() == ["C10"]
    assert replace(first, facts=(arrival, replace(laid, code="sheet_lifted"), again, left)).check() == ["C10"]
    assert replace(first, facts=(arrival, laid, again, replace(left, under_sheet=0))).check() == ["C11"]


async def test_fake_event_log_accepts_the_sheet_filter() -> None:
    fake = FakeFactory(recording=recording(CASE))
    async with client(fake) as factory:
        root = await factory.api.get_api_root()
        assert root.api == "2.3.0"
        events = await factory.api.get_world_events(types="sheet")
        assert events.events
        assert all(e.type == "sheet" and e.ids for e in events.events)

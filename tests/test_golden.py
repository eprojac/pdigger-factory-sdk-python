"""Golden tests: every recording of `fixtures/recordings` replays to `fixtures/golden` exactly.

Text byte for byte, JSON after parsing; the same through `Replay` (sync and async) and through the live Interpreter
over the fake server.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from pdigger_factory_sdk import Activity, Event, Replay, Summary, TrayFact, TrayReport, TraySpeckle
from pdigger_factory_sdk.testing import FakeExhausted, FakeFactory
from pdigger_factory_sdk.trays import describe_open_txt, describe_speckle_txt

from .support import (
    FIXTURES,
    GOLDEN,
    client,
    golden,
    golden_summary,
    recording_name,
    recordings,
    text_of,
    tray_stones_of,
)

CASES = [pytest.param(path, id=recording_name(path)) for path in recordings()]
PARTS = (
    "events.txt",
    "activities.txt",
    "trays.txt",
    "open.txt",
    "tray-stones.txt",
    "reports.txt",
    "speckle.txt",
    "summary.json",
    "summary.txt",
)
COMPLETE = GOLDEN / "complete"
"""The golden summaries of the complete published recordings (the opt-in integration tests replay those)."""


def reports_of(reports: Iterable[TrayReport]) -> str:
    """The golden `reports.txt` form: every report's describe(), then a newline (SDK-DESIGN.md 4.7)."""
    return "".join(r.describe() + "\n" for r in reports)


def manifest() -> dict[str, Any]:
    files: dict[str, Any] = json.loads((FIXTURES / "MANIFEST.json").read_bytes())["files"]
    return files


def check(name: str, events: list[Event], activities: list[Activity], facts: list[TrayFact], summary: Summary) -> None:
    assert text_of(events) == golden(name, "events.txt")
    assert tray_stones_of(events) == golden(name, "tray-stones.txt")  # each event's tray stone, revisions' too
    assert text_of(activities) == golden(name, "activities.txt")
    assert text_of(facts) == golden(name, "trays.txt")
    assert describe_open_txt(facts) == golden(name, "open.txt")
    assert summary.to_dict() == golden_summary(name)
    assert summary.describe() == golden(name, "summary.txt")


def test_every_recording_has_its_golden_files_and_no_other() -> None:
    names = {recording_name(p) for p in recordings()}
    assert names
    assert names == {recording_name(Path(k)) for k in manifest() if k.startswith("recordings/")}
    files = {p.name for p in GOLDEN.iterdir() if p.is_file() and not p.name.startswith(".")}  # no .DS_Store
    assert files == {f"{n}.{part}" for n in names for part in PARTS}


def test_the_fixtures_are_the_ones_of_the_manifest() -> None:
    """Every fixture file is listed in MANIFEST.json with its size and sha256 (the golden summaries of the complete
    recordings in `golden/complete` too), and every listed file is there."""
    present = {p.relative_to(FIXTURES).as_posix() for p in FIXTURES.rglob("*") if p.is_file() and p.name[0] != "."}
    listed = manifest()
    assert present - {"MANIFEST.json"} == set(listed)
    assert any(name.startswith("golden/complete/") for name in listed)
    for name, entry in listed.items():
        data = (FIXTURES / name).read_bytes()
        assert (len(data), hashlib.sha256(data).hexdigest()) == (entry["bytes"], entry["sha256"]), name


def test_the_complete_summaries_come_in_pairs() -> None:
    files = {p.name for p in COMPLETE.iterdir()} if COMPLETE.is_dir() else set()
    videos = {name.split(".")[0] for name in files}
    assert files == {f"{v}.summary.{ext}" for v in videos for ext in ("json", "txt")}
    assert videos == {"aug28", "jul30", "dry1", "dry2"}  # the four published recordings


@pytest.mark.parametrize("path", sorted(COMPLETE.glob("*.summary.json")), ids=lambda p: p.name.split(".")[0])
def test_a_complete_summary_reads_as_its_text(path: Path) -> None:
    """The SDK writes the text of a complete recording's golden summary from its JSON, byte for byte."""
    data = json.loads(path.read_bytes())
    summary = Summary.from_dict(data)
    assert summary.to_dict() == data
    assert summary.describe().encode("utf-8") == path.with_name(path.name.replace(".json", ".txt")).read_bytes()


@pytest.mark.parametrize("path", CASES)
def test_replay(path: Path) -> None:
    result = Replay.open(path).interpret()
    check(recording_name(path), result.events, result.activities, result.tray_facts, result.summary)
    assert result.header is not None
    assert [f for tray in result.trays for f in tray.facts] == result.tray_facts
    assert [tray.check() for tray in result.trays] == [[] for _ in result.trays]  # every account is consistent
    reports = [r for r in (tray.report() for tray in result.trays) if r is not None]
    assert reports_of(reports) == golden(recording_name(path), "reports.txt")
    assert describe_speckle_txt(result.speckle) == golden(recording_name(path), "speckle.txt")  # 4.6.12


@pytest.mark.parametrize("path", CASES)
def test_replay_iterators(path: Path) -> None:
    replay = Replay(path)
    name = recording_name(path)
    assert text_of(replay.events()) == golden(name, "events.txt")
    assert tray_stones_of(replay.events()) == golden(name, "tray-stones.txt")
    assert text_of(replay.activities()) == golden(name, "activities.txt")
    assert text_of(replay.tray_facts()) == golden(name, "trays.txt")
    assert text_of(f for tray in replay.trays() for f in tray.facts) == golden(name, "trays.txt")
    assert reports_of(replay.tray_reports()) == golden(name, "reports.txt")
    assert replay.summary().to_dict() == golden_summary(name)


@pytest.mark.parametrize("path", CASES)
async def test_async_replay(path: Path) -> None:
    replay = Replay(path)
    events: list[Event] = []
    activities: list[Activity] = []
    facts: list[TrayFact] = []
    async with replay.aupdates() as updates:
        async for update in updates:
            events += update.events
            activities += update.activities
            facts += update.tray_facts
    check(recording_name(path), events, activities, facts, await replay.asummary())
    async with replay.atray_facts() as stream:
        assert [fact async for fact in stream] == facts
    assert [f for tray in await replay.atrays() for f in tray.facts] == facts
    assert reports_of(await replay.atray_reports()) == golden(recording_name(path), "reports.txt")


@pytest.mark.parametrize("path", CASES)
def test_summary_round_trip(path: Path) -> None:
    name = recording_name(path)
    summary = Summary.from_dict(golden_summary(name))
    assert summary.to_dict() == golden_summary(name)
    assert summary.describe() == golden(name, "summary.txt")


@pytest.mark.parametrize("resume", [False, True], ids=["one-connection", "resuming"])
@pytest.mark.parametrize("path", CASES)
async def test_live_interpreter(path: Path, resume: bool) -> None:
    """The Interpreter following the fake server's stream gives the golden output too."""
    fake = FakeFactory(recording=path)
    events: list[Event] = []
    activities: list[Activity] = []
    facts: list[TrayFact] = []
    speckle: list[TraySpeckle] = []
    async with client(fake) as factory:
        interpreter = factory.interpreter(resume=resume)
        try:
            async for update in interpreter.updates():
                events += update.events
                activities += update.activities
                facts += update.tray_facts
                speckle += update.speckle
        except FakeExhausted:
            assert resume
            end = interpreter.end()
            activities += end.activities
            facts += end.tray_facts
        else:
            assert not resume
        check(recording_name(path), events, activities, facts, interpreter.summary())
        assert describe_speckle_txt(speckle) == golden(recording_name(path), "speckle.txt")
        assert interpreter.speckle() == speckle
    assert fake.streams_opened == fake.streams_closed == 1


@pytest.mark.parametrize("path", CASES)
async def test_live_tray_reports(path: Path) -> None:
    """The Interpreter's report stream over the fake server gives the golden reports, one as each tray leaves."""
    fake = FakeFactory(recording=path)
    async with client(fake) as factory:
        reports = [r async for r in factory.interpreter(resume=False).tray_reports()]
    assert reports_of(reports) == golden(recording_name(path), "reports.txt")

"""Every example runs here, over `FakeFactory` and the fixture recordings, and every Python sample of the README and
the docs is an excerpt of an example (so the docs show only code that runs)."""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import textwrap
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from pdigger_factory_sdk import FactoryError, Replay
from pdigger_factory_sdk.testing import FakeExhausted, FakeFactory, use_fake

from .support import API, EXAMPLES, GOLDEN, ROOT, golden, recording, recording_frames

DRY1 = "dry1-0000-0130"
Output = Callable[[], str]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def output(capsys: pytest.CaptureFixture[str]) -> Output:
    def read() -> str:
        return capsys.readouterr().out

    return read


def main(name: str) -> Callable[..., Coroutine[Any, Any, None]]:
    fn: Callable[..., Coroutine[Any, Any, None]] = load(name).main
    return fn


async def test_quickstart_live(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording(DRY1))), pytest.raises(FakeExhausted):
        await main("quickstart_live")()
    assert output() == golden(DRY1, "events.txt")


def test_quickstart_reports(output: Output) -> None:
    load("quickstart_reports").main(str(recording("dry1-0018-0153")))
    assert output() == golden("dry1-0018-0153", "reports.txt")


def test_quickstart_reports_reads_the_published_url(output: Output) -> None:
    fake = FakeFactory(files={"/developers/data/dry1.world.ndjson.gz": recording(DRY1).read_bytes()})
    with use_fake(fake):
        load("quickstart_reports").main()
    assert output() == golden(DRY1, "reports.txt")


async def test_quickstart_reports_follows_the_unit(output: Output) -> None:
    """Against the unit, a report as each tray leaves: tray 1 leaves when tray 2 arrives; tray 2 is still on when the
    fake's frames run out."""
    with use_fake(FakeFactory(recording=recording(DRY1))), pytest.raises(FakeExhausted):
        await load("quickstart_reports").follow()
    first = golden(DRY1, "reports.txt").split("\ntray ")[0] + "\n"
    assert output() == first


def test_quickstart_replay(output: Output) -> None:
    load("quickstart_replay").main(str(recording("aug28-1270-1300")))
    assert output() == golden("aug28-1270-1300", "events.txt") + golden("aug28-1270-1300", "summary.txt")


def test_quickstart_replay_reads_the_published_url(output: Output) -> None:
    fake = FakeFactory(files={"/developers/data/dry1.world.ndjson.gz": recording(DRY1).read_bytes()})
    with use_fake(fake):
        load("quickstart_replay").main()
    assert output() == golden(DRY1, "events.txt") + golden(DRY1, "summary.txt")
    assert [str(r.url) for r in fake.requests] == ["https://factory.pdigger.com/developers/data/dry1.world.ndjson.gz"]


async def test_high_level(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording(DRY1))):
        await main("high_level_interpreter")(frames=500)
    lines = output().splitlines()
    assert lines[0] == "First lift: 0:36.3 Right hand lifts stone 117"
    assert "[move] 1:19.2 Left hand takes stone 155 away (1.0 s)" in lines
    assert "[tray] 1:23.6 tray 2: stone 20261122T023847.0Z-2-112 was removed" in lines
    assert lines[-10] == "Left hand: Not visible; right hand: Not visible"  # the hand cards' titles
    assert lines[-9].startswith("frames: 500 (with world 500")


async def test_interpreter_core(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording(DRY1))):
        await main("interpreter_core")(frames=632)
    replay = Replay.open(recording(DRY1))
    expected = [line for update in replay.updates() for line in update.lines()]
    assert output() == "".join(f"{line}\n" for line in expected) + golden(DRY1, "summary.txt")


async def test_mid_level(output: Output) -> None:
    fake = FakeFactory(recording=recording("dry2-0582-0600"))
    fake.inject("gap", {"gap": 2}, after=4)
    fake.inject("reset", {"boot": "0123456789", "cursor": 7}, after=6)
    with use_fake(fake):
        await main("mid_level_streams")(frames=20)
    lines = output().splitlines()
    assert lines[0] == "clock: session 1794684133 shows dry2, rate 0.8"  # GET /session: no clock event on connect
    assert lines[1] == "frame g 2918 at 9:42.0: 4 stones"
    assert "gap: the server skipped 2 frames" in lines
    assert "reset: the server restarted (boot 0123456789)" in lines
    assert "worker: running" in lines
    assert sum(line.startswith("frame g ") for line in lines) == 20


async def test_mid_level_marks_revisions_and_late_worlds(output: Output) -> None:
    lines = recording_lines_with_a_late_world()
    with use_fake(FakeFactory(recording=lines)):
        await main("mid_level_streams")(frames=3)
    assert output().splitlines() == [
        "clock: session 1795314878 shows dry1, rate 0.8",  # read with GET /session after connecting
        "frame g 548 at 1:49.3: 37 stones",
        "frame g 548 at 1:49.3: 37 stones (revised)",
        "frame g 549 at 1:49.5: 37 stones (late world read back)",
    ]


def recording_lines_with_a_late_world() -> list[dict[str, Any]]:
    """Real frames of `revisions-synthetic`: g 548, its revision, then g 549 released without its world (the world is
    in the fake's later copy of that frame, which the backfill reads)."""
    frames = {(f["g"], f.get("world", {}).get("rev", 0)): f for f in recording_frames("revisions-synthetic")}
    g548, revised, g549 = frames[(548, 0)], frames[(548, 1)], frames[(549, 0)]
    late = {k: v for k, v in g549.items() if k != "world"} | {"missing": ["world"]}
    return [g548, revised, late, {**g549, "cursor": g549["cursor"] + 100}]


async def test_long_poll(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording("dry2-0582-0600"))):
        await load("mid_level_streams").long_poll(frames=3)
    assert output().splitlines() == [
        "polled frame g 2918 (cursor 15)",
        "polled frame g 2919 (cursor 16)",
        "polled frame g 2920 (cursor 17)",
    ]


async def test_raw_packets(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording("dry1-0296-0306-full"))):
        await load("mid_level_streams").raw_packets(packets=2)
    assert output().splitlines() == ["packet g 1480: 40 stones, person True", "packet g 1481: 40 stones, person True"]


async def test_media_and_metrics(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording("dry1-0296-0306-full"), replies=API, live=True)):
        await load("low_level_api").media_and_metrics()
    assert output().splitlines() == [
        'dry1: 1024 bytes of 184866573, etag "b04d70d-6af0f48f"',
        "889 samples; v2 requests: 7664.0",
    ]


async def test_live_picture(output: Output) -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"), replies=API, live=True)
    with use_fake(fake):
        await load("low_level_api").live_picture()
    assert output() == "cam-live frame g 50: a 315-byte JPEG, live\n"  # the newest frame, after the warm-up
    with use_fake(FakeFactory(recording=recording("dry1-0296-0306-full"))):
        await load("low_level_api").live_picture()
    assert output() == "the session shows the recording dry1: its pictures are the proxy video\n"


async def test_live_camera(output: Output) -> None:
    fake = FakeFactory(recording=recording(DRY1), live=True)
    with use_fake(fake):
        await fake_watch_live(fake, "cam-live")
    lines = output().splitlines()
    assert lines[0].startswith("cam-live (Live camera) is live: session ")
    assert lines[0].endswith(", 0.25 s behind the camera")
    assert re.fullmatch(r"frame g \d+: a \d+-byte JPEG", lines[1])
    assert len(lines) == 5
    for line in lines[2:]:
        assert re.fullmatch(r"\d\d:\d\d:\d\d UTC  \d+:\d\d\.\d .+", line), line
    with use_fake(FakeFactory(recording=recording(DRY1))):
        await load("live_camera").main()
    assert output() == "this unit lists no camera\n"


async def fake_watch_live(fake: FakeFactory, serial: str) -> None:
    await load("live_camera").main(serial, events=3)
    assert fake.state["video"] == serial


def test_replay_in_one_pass(output: Output) -> None:
    replay = load("replay_in_one_pass").main
    replay(str(recording(DRY1)), str(GOLDEN / f"{DRY1}.summary.json"))
    lines = output().splitlines()
    assert lines[0] == "dry1-0000-0130: dry1, frames g 0 to 650, build claude-20261122-hr4x1"
    assert lines[1] == "1134 events, 60 activities, 9 moves"
    assert lines[2] == "placed: 0:36.3 Right hand tips stone 117 and sets it down (0.2 s)"
    assert lines[-1] == "the summary matches"
    replay(str(recording("joined-mid-move")), str(GOLDEN / f"{DRY1}.summary.json"))
    assert output().splitlines()[-1] == "the summary differs"
    replay(str(recording("revisions-synthetic")))
    assert "summary" not in output()


async def test_low_level(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording("dry1-0296-0306-full"), replies=API)):
        await main("low_level_api")()
    assert output().splitlines() == [
        "build claude-20261122-hr4x1: worker idle, 0 viewers",
        "source dry1: recording, 30:07.2",
        "frame g 1530: stone cells 1063 of 40 x 64",
        "event g 1512: lift stone 643 (right)",
        "event g 1518: place stone 643 (right)",
        "overlay2d of g 3079: 44 polygons at [1920, 1080] px",  # the recorded render reply, of another frame
    ]


async def test_error_handling(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording(DRY1))):
        await main("error_handling")()
    assert output().splitlines() == [
        "no live camera: the server's catalog has no live camera",
        "HTTP 410 session_gone: session 1795314877 has ended",
        "time_out_of_range (field time): time must be within 0..1807.158 s",
        "ready: True",
    ]


async def test_error_handling_on_the_live_camera(output: Output) -> None:
    fake = FakeFactory(recording=recording(DRY1), live=True)
    fake.fail("getReady", status=503, body=b'{"error": "warming up", "code": "not_ready", "waiting": ["engines"]}')
    with use_fake(fake):
        await main("error_handling")()
    assert output().splitlines() == [
        "live camera: Live camera",
        "HTTP 410 session_gone: session 1795314877 has ended",
        "not_seekable (field video): a live camera cannot be seeked; pause and play instead",
        "not ready yet, waiting for ['engines']",
    ]


@pytest.mark.parametrize("operation", ["listVideos", "getFrame", "getReady"])
async def test_error_handling_passes_other_errors_on(operation: str) -> None:
    fake = FakeFactory(recording=recording(DRY1))
    fake.fail(operation, status=500, body=b'{"error": "boom", "code": "internal"}', times=9)
    with use_fake(fake), pytest.raises(FactoryError, match="internal"):
        await main("error_handling")()


@pytest.fixture
def sdk_log() -> Iterator[None]:
    """The SDK logger as it was before an example set it up."""
    logger = logging.getLogger("pdigger_factory_sdk")
    handlers, level = list(logger.handlers), logger.level
    yield
    logger.handlers[:] = handlers
    logger.setLevel(level)


@pytest.mark.usefixtures("sdk_log")
async def test_reconnects_gives_up(capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeFactory(recording=recording(DRY1))
    fake.fail("streamFrames", exc=httpx.ConnectError("refused"), times=99)
    with use_fake(fake):
        await main("reconnects")()
    out, err = capsys.readouterr()
    reason = "could not connect (transport: GET /api/v2/session/frames/stream: ConnectError: refused)"
    assert out == f"stream_closed: the frames stream gave up after 3 reconnects: {reason}\n"
    assert err.count(f"WARNING pdigger_factory_sdk.streams: the frames stream: {reason}; reconnecting in") == 3
    assert "DEBUG pdigger_factory_sdk._http: GET /api/v2/session/frames/stream: ConnectError: refused; retry 1" in err


@pytest.mark.usefixtures("sdk_log")
async def test_reconnects_prints_events(capsys: pytest.CaptureFixture[str]) -> None:
    with use_fake(FakeFactory(recording=recording(DRY1))):
        await main("reconnects")(events=3)
    out, _ = capsys.readouterr()
    assert out == "".join(golden(DRY1, "events.txt").splitlines(keepends=True)[:3])


async def test_session_control(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording(DRY1), replies=API)):
        await main("session_control")()
    assert output().splitlines() == [
        "dry1: Dry side · 1 (recording, 30:07.2)",
        "session 1795314878 shows dry1, playing at 0.8x",
        "playhead at 0:00.0; the server's clock is +0.000 s from ours",
        "watching dry1 would start session 1795314879 at 2:19.0",
    ]


async def test_session_control_on_the_live_camera(output: Output) -> None:
    with use_fake(FakeFactory(recording=recording(DRY1), replies=API, live=True)):
        await main("session_control")()
    lines = output().splitlines()
    assert lines[1] == "cam-live: Live camera (camera, live)"
    assert lines[2] == "session 1795314878 shows cam-live, playing at 1.0x"
    assert lines[-1] == "watching cam-live would start session 1795314879 at 0:00.0"


async def test_tray_points(output: Output) -> None:
    path = recording("dry1-0018-0153")
    with use_fake(FakeFactory(recording=path, replies=API)):
        await main("tray_points")(str(path), 1)
    assert output().splitlines() == [
        "tray 1: arrived 0:22.5, leaving 2:24.0",
        *(f"  removed: 20261122T023847.0Z-1-{n}" for n in (105, 112, 117, 125, 126, 127)),
        "seek to 0:22.5 would show dry1 from 0:22.5",
        "seek to 2:24.0 would show dry1 from 2:24.0",
    ]


async def test_tray_points_reads_the_published_url(output: Output) -> None:
    data = recording(DRY1).read_bytes()
    fake = FakeFactory(recording=recording(DRY1), replies=API, files={"/developers/data/dry1.world.ndjson.gz": data})
    with use_fake(fake):
        await main("tray_points")()
    lines = output().splitlines()
    assert lines[:2] == ["tray 1: arrived 0:05.0, leaving 0:14.0", "tray 2: arrived 0:22.5, leaving 2:09.6"]
    assert lines[-2:] == ["seek to 0:22.5 would show dry1 from 0:22.5", "seek to 2:09.6 would show dry1 from 2:09.6"]


async def test_replay_paced(output: Output) -> None:
    fake = FakeFactory()
    with use_fake(fake):
        await main("replay_paced")(str(recording("aug28-1270-1300")), speed=4.0)
    assert output() == golden("aug28-1270-1300", "activities.txt")
    assert sum(fake.time.slept) == pytest.approx(30.0 / 4.0, abs=0.1)


def test_tutorial_high_level(output: Output) -> None:
    name = "dry1-0018-0153"
    load("tutorial_high_level").main(str(recording(name)))
    moves = [
        "0:36.3 stone 117: right hand, placed, 0.2 s",
        "0:50.5 stone 128: left hand, dropped, 0.2 s",
        "1:09.2 stone 102: right hand, placed, 6.0 s",
        "1:18.8 stone 95: right hand, placed, 1.0 s",
        "1:19.2 stone 155: left hand, carried_away, 1.0 s",
        "1:25.6 stone 106: right hand, carried_away, 2.6 s",
        "1:30.3 stone 94: right hand, placed, 0.2 s",
        "2:01.3 stone 250: left hand, dropped, 1.6 s",
        "2:07.8 stone 102: right hand, placed, 0.2 s",
    ]
    tray = "tray 1: 0:22.5 to 2:24.0, 6 stones removed for certain"
    track = "stone 155: 5 points from 1:15.6 to 1:19.8"
    activities, facts, summary = (golden(name, f) for f in ("activities.txt", "trays.txt", "summary.txt"))
    want = activities + "".join(f"{move}\n" for move in moves) + facts + f"{tray}\n" + summary + f"{track}\n"
    assert output() == want


def test_tutorial_reports(output: Output) -> None:
    name = "dry1-0018-0153"
    load("tutorial_reports").main(str(recording(name)))
    reports, facts = golden(name, "reports.txt"), golden(name, "trays.txt")
    (report,) = Replay.open(str(recording(name))).tray_reports()
    row = {
        "tray": 1,
        "arrived": report.arrived.wall,  # 2026-11-22T02:38:47.0Z
        "left": report.left.wall,
        "removed": [f"20261122T023847.0Z-1-{n}" for n in (117, 127, 126, 105, 112, 125)],  # 0.98 each
        "added": [  # 0.96 each; 151, 246, 249 and 278 are 0.52
            "20261122T024104.0Z-1-177",
            "20261122T024018.8Z-1-193",
            "20261122T024029.7Z-1-201",
            "20261122T024031.0Z-1-203",
            "20261122T024118.1Z-1-279",
            "20261122T024121.3Z-1-280",
            "20261122T024121.3Z-1-281",
            "20261122T024124.2Z-1-282",
            "20261122T024126.9Z-1-284",
        ],
        "moved": [f"20261122T023847.0Z-1-{n}" for n in (85, 86, 88, 89, 90, 104, 118, 120)],
        "unclear": 18,
        "complete": False,
    }
    lines = output().splitlines(keepends=True)
    n = len(reports.splitlines())
    assert "".join(lines[:n]) == reports
    assert json.loads(lines[n]) == row
    assert "".join(lines[n + 1 :]) == facts


async def test_tutorial_low_level(output: Output) -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"), replies=API)
    with use_fake(fake):
        await main("tutorial_low_level")(g=1510)
    assert output().splitlines() == [
        "ready true, worker idle: 3.8 frames a second, 241.6 ms each",
        "checkpoint 8d09531c915f on trt: 6 engines",
        "70 joints, 62 bones, 11 colours",
        "stone 627: 51 points, g 1480 to 1530",
        "focus on stone 627: dry run true",
        "frame g 1510 at 5:01.2: 42 stones, rect",
        "maps of g 1510: 40 x 64 cells, 920 of the tray, 1169 of stones",
        # the fake does not draw: the draw lists are the unit's recorded ones, of dry2's frame 3079
        "overlay of g 3079: 44 polygons, 7 segments, 8 circles at 1920 x 1080 px",
        "scene of g 3079: 42 stones, 7 bones",
        "g 1510: 42 outlines, 168 points",
        "g 1510: 50 of 62 bones",
        "left hand: Hand on a stone, wrist at 1528, 595 px",
        "right hand: Holding a stone, wrist at 1270, 415 px",
        "regions of g 1510: a 1106-byte PNG",
        "picture: /api/v2/videos/dry1/media at 5:01.2, 960 x 540",
    ]
    changes = [r for r in fake.requests if r.method != "GET"]
    assert [(r.method, r.url.path, r.url.params.get("dry_run")) for r in changes] == [
        ("PUT", "/api/v2/session/focus", "1")
    ]


async def test_testing_with_fake() -> None:
    module = load("testing_with_fake")
    for name in ("test_count_moves", "test_the_whole_recording", "test_a_server_that_fails"):
        await getattr(module, name)()


# ---- the docs show only code of the examples ------------------------------------------------------------------------
_BLOCK = re.compile(r"^```python\n(.*?)^```", re.S | re.M)


def doc_files() -> list[Path]:
    return [
        ROOT / "README.md",
        *sorted((ROOT / "docs").glob("*.md")),
        *sorted((ROOT / "docs" / "tutorials").glob("*.md")),
    ]


def samples() -> list[tuple[str, str]]:
    out = []
    for path in doc_files():
        for n, block in enumerate(_BLOCK.findall(path.read_text(encoding="utf-8")), 1):
            out.append((f"{path.relative_to(ROOT)}#{n}", block))
    return out


def excerpt_of_an_example(block: str) -> bool:
    needle = textwrap.dedent(block).strip("\n").splitlines()
    for example in EXAMPLES.glob("*.py"):
        lines = example.read_text(encoding="utf-8").splitlines()
        for i in range(len(lines) - len(needle) + 1):
            window = textwrap.dedent("\n".join(lines[i : i + len(needle)])).splitlines()
            if window == needle:
                return True
    return False


def test_the_docs_have_samples() -> None:
    assert len(samples()) >= 10


@pytest.mark.parametrize(("where", "block"), samples(), ids=[w for w, _ in samples()])
def test_every_sample_is_an_excerpt_of_an_example(where: str, block: str) -> None:
    assert excerpt_of_an_example(block), f"{where} is not in examples/:\n{block}"


def test_every_example_is_tested() -> None:
    tested = set(re.findall(r'(?:main|load)\("([a-z_]+)"\)', Path(__file__).read_text(encoding="utf-8")))
    assert tested == {p.stem for p in EXAMPLES.glob("*.py")}

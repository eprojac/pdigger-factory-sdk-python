"""fixtures/damaged: recordings and streams with lines and events that do not decode (SDK-DESIGN.md section 6, 2.3
rule 9). Every SDK replays and streams them and gets what their JSON files say."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from pdigger_factory_sdk import FactoryClient, FrameItem, PacketItem, Replay, Update

from .support import BASE_URL, FIXTURES

DAMAGED = FIXTURES / "damaged"
LINES = DAMAGED / "lines.ndjson"
EXPECTED: dict[str, Any] = json.loads((DAMAGED / "lines.json").read_text(encoding="utf-8"))
HEADERS = sorted(DAMAGED.glob("header-*.ndjson"))


def check_updates(updates: list[Update]) -> None:
    *frames, final = updates
    assert [u.frame.g for u in frames if u.frame] == EXPECTED["updates"]
    assert [u.state.wall for u in frames] == pytest.approx(EXPECTED["walls"], abs=1e-6)
    assert final.final
    assert [a.describe() for a in final.activities] == EXPECTED["final_activities"]


def test_a_replay_skips_every_damaged_line() -> None:
    replay = Replay(LINES)
    check_updates(list(replay.updates()))
    summary = replay.summary()
    assert (summary.frames, summary.sessions) == (EXPECTED["summary"]["frames"], EXPECTED["summary"]["sessions"])
    header = replay.header
    assert header is not None
    assert header.name == EXPECTED["header"]["name"]


async def test_an_async_replay_skips_every_damaged_line() -> None:
    replay = Replay(LINES)
    async with replay.aupdates() as updates:
        check_updates([u async for u in updates])
    assert (await replay.asummary()).frames == EXPECTED["summary"]["frames"]


@pytest.mark.parametrize("path", HEADERS, ids=lambda p: p.stem)
async def test_the_header_is_the_first_line_when_it_decodes(path: Path) -> None:
    want = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    header = Replay(path).header
    assert (header and {"name": header.name, "source": header.source is not None}) == want["header"]
    aheader = await Replay(path).aheader()
    assert (aheader and aheader.name) == (header and header.name)
    replay = Replay(path)
    assert [u.frame.g for u in replay.updates() if u.frame] == want["updates"]
    assert (replay.header and replay.header.name) == (header and header.name)  # read by the pass too
    assert [f.g for f in Replay(path).frames()] == want["updates"]  # the frames read the first line as a header too


DEEP = "[" * 100_000 + "]" * 100_000  # JSON nested deeper than Python's recursion limit


def test_a_line_nested_too_deep_is_one_that_does_not_decode(tmp_path: Path) -> None:
    first, rest = LINES.read_bytes().split(b"\n", 1)  # its damaged lines are not all UTF-8
    path = tmp_path / "deep.ndjson"
    path.write_bytes(first + b"\n" + DEEP.encode() + b"\n" + rest)
    check_updates(list(Replay(path).updates()))


def sse(events: list[dict[str, str]]) -> bytes:
    return "".join(f"id: {e['id']}\nevent: {e['event']}\ndata: {e['data']}\n\n" for e in events).encode("utf-8")


def unit(events: list[dict[str, str]], path: str) -> httpx.MockTransport:
    """A unit whose stream at `path` sends `events` and ends; the shared state cannot be read (404)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == path:
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=sse(events))
        return httpx.Response(404, json={"error": "not here", "code": "not_found"})

    return httpx.MockTransport(handle)


def skipped(caplog: pytest.LogCaptureFixture) -> int:
    return sum("does not decode" in r.getMessage() for r in caplog.records)


async def test_a_stream_skips_every_event_that_does_not_decode(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="pdigger_factory_sdk")
    stream = EXPECTED["stream"]
    transport = unit(stream["events"], "/api/v2/session/frames/stream")
    async with FactoryClient(BASE_URL, transport=transport) as factory:
        items = [i async for i in factory.streams.frames(resume=False)]
        assert [i.frame.g for i in items if isinstance(i, FrameItem)] == stream["frames"]
        assert skipped(caplog) == stream["decode_errors"]
        caplog.clear()
        async with factory.interpreter(resume=False).updates() as updates:
            check_updates([u async for u in updates])
        assert skipped(caplog) == stream["decode_errors"]
        found = await factory.interpreter(resume=False).wait_for(EXPECTED["wait_for"]["type"], timeout=5)
    assert found.g == EXPECTED["wait_for"]["g"]


async def test_an_event_nested_too_deep_is_one_that_does_not_decode(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="pdigger_factory_sdk")
    stream = EXPECTED["stream"]
    events = [*stream["events"][:1], {"id": "deep", "event": "frame", "data": DEEP}, *stream["events"][1:]]
    async with FactoryClient(BASE_URL, transport=unit(events, "/api/v2/session/frames/stream")) as factory:
        items = [i async for i in factory.streams.frames(resume=False)]
    assert [i.frame.g for i in items if isinstance(i, FrameItem)] == stream["frames"]
    assert skipped(caplog) == stream["decode_errors"] + 1


async def test_a_packet_stream_skips_every_packet_that_does_not_decode(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="pdigger_factory_sdk")
    packets = EXPECTED["packets"]
    async with FactoryClient(BASE_URL, transport=unit(packets["events"], "/api/v2/raw/stream")) as factory:
        items = [i async for i in factory.streams.raw_packets(resume=False)]
    assert [i.packet.g for i in items if isinstance(i, PacketItem)] == packets["g"]
    assert skipped(caplog) == packets["decode_errors"]

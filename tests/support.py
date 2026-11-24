"""Paths and helpers shared by the tests."""

from __future__ import annotations

import asyncio
import gzip
import json
import random
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

import httpx

from pdigger_factory_sdk import Event, FactoryClient
from pdigger_factory_sdk.testing import FakeFactory

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
RECORDINGS = FIXTURES / "recordings"
GOLDEN = FIXTURES / "golden"
API = FIXTURES / "api"
FRAMES = FIXTURES / "frames"
SPEC = ROOT / "spec"
EXAMPLES = ROOT / "examples"
BASE_URL = "http://fake.test"


class Describable(Protocol):
    def describe(self) -> str: ...


def recording_name(path: Path) -> str:
    """`dry1-0000-0130.ndjson.gz` -> `dry1-0000-0130`."""
    for suffix in (".ndjson.gz", ".ndjson"):
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    return path.name


def recordings() -> list[Path]:
    """Every recording of `fixtures/recordings` (the golden tests iterate the folder, never a fixed list)."""
    return sorted(p for p in RECORDINGS.iterdir() if p.is_file() and p.name.endswith((".ndjson", ".ndjson.gz")))


def recording(name: str) -> Path:
    return RECORDINGS / f"{name}.ndjson.gz"


def recording_lines(name: str) -> list[dict[str, Any]]:
    """The decoded lines of a recording, header first."""
    text = gzip.decompress(recording(name).read_bytes()).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def recording_frames(name: str) -> list[dict[str, Any]]:
    return [x for x in recording_lines(name) if "g" in x and "cursor" in x]


def golden(name: str, part: str) -> str:
    """A golden file exactly as stored (decoded bytes: no newline translation)."""
    return (GOLDEN / f"{name}.{part}").read_bytes().decode("utf-8")


def golden_summary(name: str) -> Any:
    return json.loads(golden(name, "summary.json"))


def text_of(items: Iterable[Describable]) -> str:
    """The `describe()` lines of events or activities, as the golden files hold them."""
    return "".join(item.describe() + "\n" for item in items)


def tray_stones_of(events: Iterable[Event]) -> str:
    """The golden `tray-stones.txt` form: `{g} {type} {stone_id} {tray_stone}` for every event with a tray stone."""
    return "".join(f"{e.g} {e.type} {e.stone_id} {e.tray_stone}\n" for e in events if e.tray_stone is not None)


def api_index() -> dict[str, Any]:
    index: dict[str, Any] = json.loads((API / "INDEX.json").read_text(encoding="utf-8"))
    return index


def api_bytes(key: str) -> bytes:
    name: str = api_index()[key]["file"]
    return (API / name).read_bytes()


def api_json(key: str) -> Any:
    return json.loads(api_bytes(key))


def frame_json(name: str) -> Any:
    return json.loads((FRAMES / f"{name}.json").read_text(encoding="utf-8"))


def client(fake: FakeFactory, **options: Any) -> FactoryClient:
    """A client of `fake` with the fake's time and a seeded jitter (no real sleeping); `options` override them."""
    t = fake.time
    options.setdefault("rng", random.Random(7))
    options.setdefault("sleep", t.sleep)
    options.setdefault("monotonic", t.monotonic)
    options.setdefault("wall_clock", t.time)
    return FactoryClient(BASE_URL, transport=fake, **options)


class Gated(httpx.AsyncBaseTransport):
    """`fake`, with the replies to the paths that end in `suffix` held until `gate` is set."""

    def __init__(self, fake: FakeFactory, suffix: str, gate: asyncio.Event) -> None:
        self.fake, self.suffix, self.gate = fake, suffix, gate

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(self.suffix):
            await self.gate.wait()
        return await self.fake.handle_async_request(request)


def gated_client(fake: FakeFactory, suffix: str, gate: asyncio.Event, **options: Any) -> FactoryClient:
    """`client(fake)`, with the replies to the paths that end in `suffix` held until `gate` is set."""
    t = fake.time
    options.setdefault("rng", random.Random(7))
    options.setdefault("sleep", t.sleep)
    options.setdefault("monotonic", t.monotonic)
    options.setdefault("wall_clock", t.time)
    return FactoryClient(BASE_URL, transport=Gated(fake, suffix, gate), **options)


def stream_requests(fake: FakeFactory, path: str = "/session/frames/stream") -> list[httpx.Request]:
    """The requests of one route (the frames stream by default), in order: other reads (`GET /session` after each
    connect, backfill reads) come between them."""
    return [r for r in fake.requests if r.url.path.endswith(path)]

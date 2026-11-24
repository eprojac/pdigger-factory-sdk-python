"""Test your own code offline: `FakeFactory` answers the API from a recording, with no server and no real clock.

Copy the pattern into your tests (pytest with pytest-asyncio); run this one with `pytest examples/testing_with_fake.py`.
"""

from pathlib import Path

import pytest

from pdigger_factory_sdk import FactoryClient, FactoryError, Move
from pdigger_factory_sdk.testing import FakeExhausted, FakeFactory

pytestmark = pytest.mark.asyncio  # or asyncio_mode = "auto" in your pytest configuration

RECORDING = Path(__file__).parent.parent / "fixtures" / "recordings" / "dry1-0000-0130.ndjson.gz"


async def count_moves(factory: FactoryClient, limit: int) -> dict[str, int]:
    """Your code: count the moves of each outcome among the first `limit` activities (moves and push bursts)."""
    counts: dict[str, int] = {}
    seen = 0
    async with factory.interpreter().activities() as activities:
        async for activity in activities:
            if isinstance(activity, Move) and activity.outcome is not None:
                counts[activity.outcome] = counts.get(activity.outcome, 0) + 1
            seen += 1
            if seen == limit:
                break
    return counts


async def test_count_moves() -> None:
    fake = FakeFactory(recording=RECORDING)
    async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep) as factory:
        assert await count_moves(factory, limit=44) == {"carried_away": 2, "dropped": 1, "placed": 4}


async def test_the_whole_recording() -> None:
    fake = FakeFactory(recording=RECORDING)  # the follow never ends by itself: the fake ends it when it runs dry
    async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep) as factory:
        with pytest.raises(FakeExhausted):
            await count_moves(factory, limit=10_000)


async def test_a_server_that_fails() -> None:
    fake = FakeFactory(recording=RECORDING)
    fake.fail("listVideos", status=503, code="busy", times=4)  # more failures than the client retries
    async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep, retries=3) as factory:
        with pytest.raises(FactoryError) as error:
            await factory.sources()
    assert error.value.code == "busy"
    assert fake.time.slept  # the retries waited, on the fake clock

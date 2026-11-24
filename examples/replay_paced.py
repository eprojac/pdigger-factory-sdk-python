"""Replay at the pace it was recorded (here four times faster), as an async stream of activities.

Run: `python examples/replay_paced.py dry1.world.ndjson.gz`.
"""

import asyncio
import sys

from pdigger_factory_sdk import Replay


async def main(source: str, speed: float = 4.0) -> None:
    replay = Replay.open(source)
    async with replay.aactivities(pace="realtime", speed=speed) as activities:
        async for activity in activities:
            print(activity.describe())


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))

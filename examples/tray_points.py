"""Trays: when each tray had arrived and settled, and when it was about to move out, with what was removed from it.

The tray stones carry the SDK's own ids, one per stone and never reused, so "stone X was removed" holds over time; the
world's ids are the engine tracker's, which renames stones and reuses ids. The program lists the trays of a recording
and then shows one on the shared session: a seek to its arrival and one to its leaving point. A seek changes what
every viewer sees, so this example sends dry runs only (`dry_run=True`).

Run: `PDIGGER_FACTORY_URL=http://factory-orin.local:8120 python examples/tray_points.py [recording] [tray]`
(default dry1 from factory.pdigger.com, tray 2; the dry-run seeks of its second half need a unit).
"""

import asyncio
import sys

from pdigger_factory_sdk import FactoryClient, Replay
from pdigger_factory_sdk.text import clock

DRY1 = "https://factory.pdigger.com/developers/data/dry1.world.ndjson.gz"


async def main(source: str = DRY1, show: int = 2) -> None:
    trays = await Replay.open(source).atrays()
    for tray in trays:
        window = tray.window()
        if window is None:
            continue  # a stop that never settled
        arrived, leaving = window
        print(f"tray {tray.n}: arrived {clock(arrived)}, leaving {clock(leaving)}")
        for stone in tray.stones:
            if stone.status == "removed" and stone.certain:
                print(f"  removed: {stone.id}")

    tray = next(t for t in trays if t.n == show)
    async with FactoryClient() as factory:
        for point in (tray.arrived, tray.leaving):
            if point is not None:
                would = await factory.session.seek(point.t, video=tray.video, dry_run=True)
                print(f"seek to {point.clock} would show {would.video} from {clock(would.anchor_media)}")


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else DRY1
    show = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    asyncio.run(main(source, show))

"""The shared session: its sources (recordings and live cameras), its clock, and its controls.

A control changes what every viewer sees, so this example sends dry runs only (`dry_run=True`).
Run: `python examples/session_control.py`.
"""

import asyncio

from pdigger_factory_sdk import FactoryClient
from pdigger_factory_sdk.text import clock


async def main() -> None:
    async with FactoryClient() as factory:
        sources = await factory.sources()
        for source in sources:
            length = "live" if source.duration is None else clock(source.duration)
            print(f"{source.id}: {source.title} ({source.kind}, {length})")

        shared = await factory.session.clock()
        state = shared.state
        playing = "playing" if state.playing else "paused"
        print(f"session {state.session} shows {state.video}, {playing} at {state.rate}x")
        print(f"playhead at {clock(shared.media_time_now())}; the server's clock is {shared.offset:+.3f} s from ours")

        cameras = [s for s in sources if s.is_live]
        target = cameras[0].id if cameras else sources[0].id
        would = await factory.session.watch(target, dry_run=True)
        print(f"watching {target} would start session {would.session} at {clock(would.anchor_media)}")


if __name__ == "__main__":
    asyncio.run(main())

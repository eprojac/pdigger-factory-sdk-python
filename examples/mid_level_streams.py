"""Mid level: the frames stream itself, its frames and its side events (clock, worker, gap, reset).

It resumes by itself with `Last-Event-ID`, and with `backfill=True` holds a frame whose world came late until the
world is read back, so frames always arrive in order. The unit sends a `clock` event only when the shared state
changes, so `clock_on_connect=True` reads the state after every (re)connect and passes it on first. Also here: the
same frames by long-poll, and the raw packets.

Run: `python examples/mid_level_streams.py [frames]` (default 25).
"""

import asyncio
import sys

from pdigger_factory_sdk import ClockItem, FactoryClient, FrameItem, GapItem, PacketItem, ResetItem, WorkerItem
from pdigger_factory_sdk.text import clock


async def main(frames: int = 25) -> None:
    async with FactoryClient() as factory:
        seen = 0
        async with factory.streams.frames(["world"], backfill=True, clock_on_connect=True) as stream:
            async for item in stream:
                match item:
                    case FrameItem(frame=frame, is_revision=revised, is_backfill=late):
                        world = "no world" if frame.world is None else f"{len(frame.world.stones)} stones"
                        note = " (revised)" if revised else " (late world read back)" if late else ""
                        print(f"frame g {frame.g} at {clock(frame.t)}: {world}{note}")
                        seen += 1
                        if seen == frames:
                            break
                    case ClockItem(state=state):
                        print(f"clock: session {state.session} shows {state.video}, rate {state.rate}")
                    case WorkerItem(worker=worker):
                        print(f"worker: {worker.state} {worker.reason}".rstrip())
                    case GapItem(count=count):
                        print(f"gap: the server skipped {count} frames")
                    case ResetItem(boot=boot):
                        print(f"reset: the server restarted (boot {boot})")


async def long_poll(frames: int = 10) -> None:
    """The same frames by long-poll (`GET /session/frames`), where a stream cannot stay open."""
    async with FactoryClient() as factory:
        seen = 0
        async with factory.streams.poll(["world"], wait=1000) as polls:
            async for item in polls:
                if isinstance(item, FrameItem):
                    print(f"polled frame g {item.frame.g} (cursor {item.frame.cursor})")
                    seen += 1
                    if seen == frames:
                        break


async def raw_packets(packets: int = 5) -> None:
    """The model's raw packets (`sip1`): what it saw in each frame, before the world made sense of it."""
    async with FactoryClient() as factory:
        seen = 0
        async with factory.streams.raw_packets(format="ndjson") as stream:
            async for item in stream:
                if isinstance(item, PacketItem):
                    packet = item.packet
                    print(f"packet g {packet.g}: {packet.stones.count} stones, person {packet.person.present}")
                    seen += 1
                    if seen == packets:
                        break


if __name__ == "__main__":
    asyncio.run(main(*map(int, sys.argv[1:2])))

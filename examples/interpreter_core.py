"""Feed the interpreter's core yourself: here from the long-poll, for places where a stream cannot stay open.

The core is a plain state machine (`feed(frame) -> Update`), so the same events, activities and text come out whatever
delivers the frames. The `cid` makes the long-poll count as a viewer, which keeps the worker interpreting.

Run: `python examples/interpreter_core.py [frames]` (default 3000).
"""

import asyncio
import sys

from pdigger_factory_sdk import ClockItem, FactoryClient, FrameItem, InterpreterCore


async def main(frames: int = 3000) -> None:
    core = InterpreterCore()
    async with (
        FactoryClient() as factory,
        factory.streams.poll(["world", "cards"], cid="example-core-01", backfill=True) as polls,
    ):
        async for item in polls:
            if isinstance(item, ClockItem):
                core.clock(item.state)  # gives the events their wall time
            elif isinstance(item, FrameItem):
                for line in core.feed(item.frame).lines():
                    print(line)
                if core.state.frames >= frames:
                    break
    for line in core.end().lines():  # what is still open ends as interrupted; the open tray leaves
        print(line)
    print(core.summary().describe(), end="")


if __name__ == "__main__":
    asyncio.run(main(*map(int, sys.argv[1:2])))

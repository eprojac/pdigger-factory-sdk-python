"""High level: wait for the first lift, then follow the moves, push bursts and trays as they happen, with the hands.

Run: `python examples/high_level_interpreter.py [frames]`; it stops after that many frames (default 3000, ten minutes
of media).
"""

import asyncio
import sys

from pdigger_factory_sdk import FactoryClient, Move


async def main(frames: int = 3000) -> None:
    async with FactoryClient() as factory:
        interpreter = factory.interpreter()
        lift = await interpreter.wait_for("lift", timeout=600)
        print(f"First lift: {lift.describe()}")

        async with interpreter.updates() as updates:  # goes on right after that lift
            async for update in updates:
                for activity in update.activities:
                    kind = "move" if isinstance(activity, Move) else "push"
                    print(f"[{kind}] {activity.describe()}")
                for fact in update.tray_facts:  # the SDK's own stone ids: one per stone, never reused
                    print(f"[tray] {fact.describe()}")
                if update.state.frames >= frames:
                    break

        left, right = interpreter.state.hands.left, interpreter.state.hands.right
        print(f"Left hand: {left.card or left.state}; right hand: {right.card or right.state}")
        print(interpreter.summary().describe(), end="")


if __name__ == "__main__":
    asyncio.run(main(*map(int, sys.argv[1:2])))

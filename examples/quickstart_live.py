"""Follow the shared session (whatever it shows: a live camera or a recording) and print every event as a sentence.

Run: `PDIGGER_FACTORY_URL=http://factory-orin.local:8120 python examples/quickstart_live.py`; stop it with Ctrl-C.
"""

import asyncio

from pdigger_factory_sdk import FactoryClient


async def main() -> None:
    async with FactoryClient() as factory:  # PDIGGER_FACTORY_URL, else http://factory-orin.local:8120
        async for event in factory.interpreter().events():
            print(event.describe())  # 21:17.8 Right hand lifts stone 2660


if __name__ == "__main__":
    asyncio.run(main())

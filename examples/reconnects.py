"""Watch the SDK reconnect, and give up after a few failed reconnects in a row.

The SDK logs to the `pdigger_factory_sdk` logger: a warning for every backoff wait before a reconnect and for a frame
released without its late layers, info when the server restarted, debug for retries and for reconnects after a stream
ended normally. Python shows the warnings on stderr even when logging is not set up; this program also shows the rest.
A stream resumes for ever by default; `max_reconnects` makes it give up with `stream_closed` instead.

Run: `PDIGGER_FACTORY_URL=http://factory-orin.local:8120 python examples/reconnects.py`.
"""

import asyncio
import logging

from pdigger_factory_sdk import ErrorCode, FactoryClient, FactoryError


def show_the_sdk_log() -> None:
    handler = logging.StreamHandler()  # stderr
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    sdk = logging.getLogger("pdigger_factory_sdk")
    sdk.addHandler(handler)
    sdk.setLevel(logging.DEBUG)


async def main(events: int = 5) -> None:
    show_the_sdk_log()
    async with FactoryClient() as factory:
        try:
            async with factory.interpreter(max_reconnects=3).events() as stream:
                async for event in stream:
                    print(event.describe())
                    events -= 1
                    if events == 0:
                        break
        except FactoryError as e:
            if e.code != ErrorCode.STREAM_CLOSED:
                raise
            print(e)  # stream_closed: the frames stream gave up after 3 reconnects: could not connect (...)


if __name__ == "__main__":
    asyncio.run(main())

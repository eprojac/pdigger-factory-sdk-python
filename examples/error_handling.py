"""Errors: one type, `FactoryError`, keyed on its stable `code`; reads are retried by themselves.

Run: `python examples/error_handling.py` (the server from `PDIGGER_FACTORY_URL`). It sends a dry-run seek only.
"""

import asyncio

from pdigger_factory_sdk import ErrorCode, FactoryClient, FactoryError


async def main() -> None:
    async with FactoryClient(retries=2, timeout=5.0) as factory:
        try:
            camera = await factory.live()
            print(f"live camera: {camera.title}")
        except FactoryError as e:
            if e.code != ErrorCode.LIVE_UNAVAILABLE:
                raise
            print(f"no live camera: {e.message}")

        state = await factory.session.state()
        try:
            await factory.api.get_frame("latest", session=state.session - 1)
        except FactoryError as e:
            if not e.is_session_gone:
                raise
            print(f"HTTP {e.status} {e.code}: {e.message}")

        try:
            await factory.session.seek(-1.0, dry_run=True)
        except FactoryError as e:
            print(f"{e.code} (field {e.field}): {e.message}")

        try:
            ready = await factory.api.with_options(retries=0).get_ready()
            print(f"ready: {ready.ready}")
        except FactoryError as e:
            if e.code != ErrorCode.NOT_READY:
                raise
            print(f"not ready yet, waiting for {e.waiting}")


if __name__ == "__main__":
    asyncio.run(main())

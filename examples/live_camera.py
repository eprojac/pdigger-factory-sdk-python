"""Watch a plant camera live: select it for the shared session, wait out its warm-up, then print what happens.

Selecting a camera changes what every viewer of the unit sees (the page follows the session). The unit connects to
the camera, runs the interpreter on its first 3 seconds of frames to warm it up, and then shows it live.
Run: `PDIGGER_FACTORY_URL=http://factory-orin.local:8120 python examples/live_camera.py [serial]`; stop it with Ctrl-C.
"""

import asyncio
import sys
import time

from pdigger_factory_sdk import FactoryClient


async def main(serial: str | None = None, events: int | None = None) -> None:
    async with FactoryClient() as factory:
        cameras = await factory.cameras()  # the plant cameras, by serial
        camera = await factory.source(serial) if serial else (cameras[0] if cameras else None)  # that one, or the first
        if camera is None:
            print("this unit lists no camera")
            return
        await factory.session.watch(camera)  # connecting, then warming up on the camera's first 3 s
        state = await factory.session.until_live()
        lag = state.live.lag if state.live else 0.0
        print(f"{camera.id} ({camera.title}) is live: session {state.session}, {lag:.2f} s behind the camera")

        picture = await factory.session.picture()  # the JPEG the newest frame was interpreted from
        print(f"frame g {picture.g}: a {len(picture.jpeg)}-byte JPEG")

        seen = 0
        async for event in factory.interpreter().events():
            at = time.strftime("%H:%M:%S", time.gmtime(event.wall)) if event.wall else "--:--:--"
            print(f"{at} UTC  {event.describe()}")  # on a camera, the wall time is the timestamp that matters
            seen += 1
            if events is not None and seen >= events:
                break


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))

"""Low level: one method per API operation with typed replies, and the codec for the layers' packed buffers.

Run: `python examples/low_level_api.py` (the server from `PDIGGER_FACTORY_URL`).
"""

import asyncio

from pdigger_factory_sdk import FactoryClient, codec
from pdigger_factory_sdk.text import clock


async def main() -> None:
    async with FactoryClient() as factory:
        api = factory.api
        root, health = await asyncio.gather(api.get_api_root(), api.get_health())
        print(f"build {root.build}: worker {health.worker}, {health.viewers} viewers")

        catalog = await api.list_videos()
        for source in catalog.videos if catalog else []:
            length = "live" if source.duration is None else clock(source.duration)
            print(f"source {source.id}: {source.kind}, {length}")

        frame = await api.get_frame("latest", layers=["overlay", "world"])
        if frame.overlay is not None and frame.overlay.regions is not None:
            regions = frame.overlay.regions
            stone_map = codec.decode_u8_map(regions.stone, regions.rows, regions.cols)
            cells = stone_map.count() if stone_map else 0
            print(f"frame g {frame.g}: stone cells {cells} of {regions.rows} x {regions.cols}")

        log = await api.get_world_events(limit=5, types=["lift", "place"])
        for entry in log.events:
            print(f"event g {entry.g}: {entry.type} stone {entry.id} ({entry.side})")

        overlay = await api.render_overlay2d(g=frame.g, draw="primitives")
        polygons = overlay.primitives.polygons if overlay.primitives else None
        print(f"overlay2d of g {overlay.g}: {len(polygons or [])} polygons at {overlay.native_wh} px")


async def media_and_metrics() -> None:
    """The proxy video of a recording, by byte range, and the server's Prometheus metrics."""
    async with FactoryClient() as factory:
        api = factory.api
        recording = next(s for s in await factory.sources() if not s.is_live)
        first_kib = await api.get_video_media(recording.id, range=(0, 1023))
        print(f"{recording.id}: {len(first_kib.data)} bytes of {first_kib.total}, etag {first_kib.etag}")

        metrics = await api.get_metrics()
        print(f"{len(metrics.samples)} samples; v2 requests: {metrics.value('interp_http_requests_total', route='v2')}")


async def live_picture() -> None:
    """On a live camera, every frame comes with the camera's own picture: the JPEG it was interpreted from."""
    async with FactoryClient() as factory:
        state = await factory.session.state()
        if state.live is None:
            print(f"the session shows the recording {state.video}: its pictures are the proxy video")
            return
        frame = await factory.api.get_frame("latest", layers=["overlay"])
        picture = await factory.api.get_frame_picture(frame.g, session=frame.session)
        print(f"{state.video} frame g {picture.g}: a {len(picture.jpeg)}-byte JPEG, {state.live.phase}")


if __name__ == "__main__":
    asyncio.run(main())

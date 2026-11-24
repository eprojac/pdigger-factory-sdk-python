"""The low-level tutorial (docs/tutorials/02-low-level.md) in one program: one call per API operation, typed replies,
and the codec for what the replies pack. Every step only reads, or sends a dry run that changes nothing.

Run: `python examples/tutorial_low_level.py` (the unit of `PDIGGER_FACTORY_URL`, its newest frame), or some steps by
name: `python examples/tutorial_low_level.py hands image`.
"""

import asyncio
import sys
from typing import Literal

from pdigger_factory_sdk import FactoryClient, codec
from pdigger_factory_sdk.codec import Skeleton, joints, uv_to_px
from pdigger_factory_sdk.text import clock

Frame = int | Literal["latest"]  # a frame's g, or the newest frame


async def ready(factory: FactoryClient, g: Frame = "latest") -> None:
    """Is it ready, and how fast is it?"""
    api = factory.api
    ready = await api.get_ready()  # while it starts: FactoryError not_ready, and what it waits for
    worker = (await api.get_status()).worker
    yes = str(ready.ready).lower()  # true or false, as the other languages print it
    print(f"ready {yes}, worker {worker.state}: {worker.fps} frames a second, {worker.frame_ms} ms each")


async def model(factory: FactoryClient, g: Frame = "latest") -> None:
    """The model the unit runs, engine by engine, and the topology every drawing is made with."""
    api = factory.api
    model = await api.get_model()
    print(f"checkpoint {model.checkpoint} on {model.backend}: {len(model.engines)} engines")
    topology = await api.get_topology()
    print(f"{topology.joints} joints, {len(topology.bones)} bones, {len(topology.colors)} colours")


async def stone(factory: FactoryClient, g: Frame = "latest") -> None:
    """A stone's track, and the focus on it for every viewer, as a dry run."""
    api = factory.api
    world = await api.get_world()
    if not world.stones:  # between trays, or a camera with nothing in view
        print("no stone in the newest world")
        return
    track = await api.get_stone(world.stones[0].id)  # its path, from frame first_g to last_g
    print(f"stone {track.id}: {len(track.path2d)} points, g {track.first_g} to {track.last_g}")
    reply = await api.put_focus(track.id, dry_run=True)  # the unit checks it; nothing changes
    print(f"focus on stone {track.id}: dry run {str(reply.dry_run).lower()}")


async def frame(factory: FactoryClient, g: Frame = "latest") -> None:
    """A frame with the layers you ask for, and the model's maps of it, decoded."""
    api = factory.api
    frame = await api.get_frame(g, layers=["world", "cards"])
    families = ", ".join(sorted({s.family for s in frame.world.stones})) if frame.world else ""
    print(f"frame g {frame.g} at {clock(frame.t)}: {len(frame.world.stones) if frame.world else 0} stones, {families}")
    packet = await api.raw_packet(frame.g)  # what the model saw, before the world made sense of it
    rows, cols = packet.maps.grid
    tray = codec.decode_zlib_map(packet.maps.tray, rows, cols)  # one byte a cell
    stones = codec.decode_zlib_map(packet.maps.stone, rows, cols)
    print(
        f"maps of g {packet.g}: {rows} x {cols} cells, {tray.count() if tray else 0} of the tray, "
        f"{stones.count() if stones else 0} of stones"
    )


async def draw(factory: FactoryClient, g: Frame = "latest") -> None:
    """Draw lists, ready to paint: the 2-D overlay in video pixels and the 3-D scene."""
    api = factory.api
    at = (await api.get_frame(g, layers=["world"])).g
    overlay = await api.render_overlay2d(g=at, draw="primitives")  # shapes, styled, in video pixels
    p = overlay.primitives
    if p is not None:
        print(
            f"overlay of g {overlay.g}: {len(p.polygons or [])} polygons, {len(p.segments or [])} segments, "
            f"{len(p.circles or [])} circles at {overlay.native_wh[0]} x {overlay.native_wh[1]} px"
        )
    scene = await api.render_scene3d(g=at, surface="compact")  # stones, hands and the work plane, in scene units
    print(f"scene of g {scene.g}: {len(scene.stones)} stones, {len(scene.skeleton.bones or [])} bones")


async def stones(factory: FactoryClient, g: Frame = "latest") -> None:
    """Stone shapes, drawn by hand: each stone's outline is a polygon in video pixels."""
    api = factory.api
    colors = (await api.get_topology()).colors
    frame = await api.get_frame(g, layers=["world"])
    shapes = []
    for stone in frame.world.stones if frame.world else []:
        fill = colors["stones"] if stone.detected else colors["stoneBox"]
        shapes.append((fill, [(x, y) for x, y in stone.outline]))  # hand these to any drawing library
    print(f"g {frame.g}: {len(shapes)} outlines, {sum(len(points) for _, points in shapes)} points")


async def hands(factory: FactoryClient, g: Frame = "latest") -> None:
    """Hands and bones, drawn by hand from the overlay layer's joints and the topology."""
    api = factory.api
    topology = await api.get_topology()
    skeleton = Skeleton(topology)
    frame = await api.get_frame(g, layers=["overlay", "cards"])
    width, height = (await api.get_video(frame.video)).native_wh
    js = joints(frame.overlay.joints if frame.overlay and frame.overlay.joints else [])  # u, v, visibility, x, y, z
    px = {i: uv_to_px(j.u, j.v, width, height) for i, j in enumerate(js) if j}
    bones = [(px[a], px[b]) for a, b in skeleton.visible_bones(js)]  # lines to draw, in video pixels
    print(f"g {frame.g}: {len(bones)} of {len(skeleton.bones)} bones")
    for hand in ("left", "right"):
        card = getattr(frame.cards, hand) if frame.cards else None
        joint = skeleton.wrist_joint(hand)  # None for a topology without that wrist
        wrist = px.get(joint) if joint is not None else None
        where = f"wrist at {wrist[0]:.0f}, {wrist[1]:.0f} px" if wrist else "not in view"
        print(f"{hand} hand: {card.title if card else 'no card'}, {where}")


async def image(factory: FactoryClient, g: Frame = "latest") -> None:
    """A frame's image data: the model's rasters as PNG, and where the camera's picture of the frame is."""
    api = factory.api
    frame = await api.get_frame(g, layers=["world"])
    png = await api.get_frame_raster(frame.g, "regions")  # also "mask" and "depth"
    print(f"regions of g {frame.g}: a {len(png)}-byte PNG")
    video = await api.get_video(frame.video)
    if video.is_live:  # a camera has no video: each frame comes with the JPEG it was interpreted from
        picture = await api.get_frame_picture(frame.g, session=frame.session)
        print(f"picture: a {len(picture.jpeg)}-byte JPEG of g {picture.g}")
    else:  # a recording's picture is its video at frame.t
        print(f"picture: {video.proxy} at {clock(frame.t)}, {video.proxy_wh[0]} x {video.proxy_wh[1]}")


STEPS = {
    "ready": ready,
    "model": model,
    "stone": stone,
    "frame": frame,
    "draw": draw,
    "stones": stones,
    "hands": hands,
    "image": image,
}


async def main(*steps: str, g: Frame = "latest") -> None:
    async with FactoryClient() as factory:  # PDIGGER_FACTORY_URL, else http://factory-orin.local:8120
        for name in steps or STEPS:
            await STEPS[name](factory, g)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))

"""Codec helpers over real frames and renders (fixtures/frames, fixtures/api)."""

from __future__ import annotations

import base64
import math
import zlib

import pytest

from pdigger_factory_sdk import FactoryError
from pdigger_factory_sdk.codec import (
    Grid,
    Joint,
    Skeleton,
    StoneItem,
    decode_bitset,
    decode_depth,
    decode_f32le,
    decode_u8_map,
    decode_u16le,
    decode_zlib_map,
    depth_log_z,
    depth_z,
    joints,
    px_to_uv,
    quat_to_yaw,
    scene_to_three,
    stone_items,
    three_to_scene,
    uv_to_px,
)
from pdigger_factory_sdk.models import Frame, Scene3D, Topology

from .support import api_json, frame_json


@pytest.fixture(scope="module")
def frame() -> Frame:
    return Frame.from_json(frame_json("dry1-carry-full"))


def test_sip1_maps(frame: Frame) -> None:
    assert frame.raw is not None
    rows, cols = frame.raw.maps.grid
    stone = decode_zlib_map(frame.raw.maps.stone, rows, cols)
    assert stone is not None
    assert (stone.rows, stone.cols, len(stone.data)) == (40, 64, 2560)
    assert 0 < stone.count() < 2560
    assert decode_zlib_map(frame.raw.maps.human, rows, cols) is None  # redacted
    assert frame.raw.stones.tray_grid is not None
    tray_rows, tray_cols = frame.raw.stones.tray_grid
    tray = decode_zlib_map(frame.raw.stones.tray, tray_rows, tray_cols)
    assert tray is not None
    assert len(tray.to_lists()) == 80


def test_overlay_maps(frame: Frame) -> None:
    assert frame.overlay is not None
    regions = frame.overlay.regions
    stone = decode_u8_map(regions.stone, regions.rows, regions.cols)
    assert stone is not None
    assert stone.count() > 0
    assert stone.count(0) + sum(1 for b in stone.data if b) == 2560
    assert stone.at(39, 63) == stone.data[-1]
    assert stone.row(1) == stone.data[64:128]
    assert decode_u8_map(None, 40, 64) is None
    with pytest.raises(IndexError):
        stone.at(40, 0)
    with pytest.raises(IndexError):
        stone.row(-1)


def test_surface_bitsets_and_depth(frame: Frame) -> None:
    assert frame.scene is not None
    surface = frame.scene.surface
    flat = decode_bitset(surface.flat, surface.rows * surface.cols)
    assert flat is not None
    assert len(flat) == 2560
    quads = decode_bitset(surface.quads.raw, (surface.rows - 1) * (surface.cols - 1))
    assert quads is not None
    assert any(quads)
    assert decode_bitset(None, 3) is None
    assert surface.q is None  # redacted depth texture
    assert decode_depth(None, surface.lo, surface.hi, surface.a, surface.b) is None
    grid = Grid(rows=1, cols=2, data=bytes([0, 255]))
    z = decode_depth(grid, surface.lo, surface.hi, surface.a, surface.b)
    args = (surface.lo, surface.hi, surface.a, surface.b)
    assert z == [depth_z(0, *args), depth_z(255, *args)]
    assert depth_log_z(255, -1.0, 1.0, 2.0, 0.5) == pytest.approx(2.5)
    assert depth_z(0, -1.0, 1.0, 1.0, 0.0) == pytest.approx(math.exp(-1.0))


def test_render_mesh() -> None:
    scene = Scene3D.from_json(api_json("renderScene3d"))
    mesh = scene.surface
    assert mesh is not None
    positions = decode_f32le(mesh.positions)
    indices = decode_u16le(mesh.indices)
    assert positions is not None
    assert indices is not None
    assert len(positions) == mesh.rows * mesh.cols * 3
    assert len(indices) % 3 == 0
    assert max(indices) < mesh.rows * mesh.cols
    assert mesh.compact() is None
    assert decode_f32le(None) is None
    assert decode_u16le(None) is None


def test_compact_render_depth() -> None:
    mesh = Scene3D.from_json(api_json("renderScene3d-compact")).surface
    assert mesh is not None
    compact = mesh.compact()
    assert compact is not None
    q = decode_u8_map(compact.q, compact.rows, compact.cols)
    z = decode_depth(q, compact.lo, compact.hi, compact.a, compact.b)
    assert z is not None
    assert len(z) == 2560
    assert all(v > 0 for v in z)


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda: decode_u8_map("!!", 1, 1), "u8 map: not base64"),
        (lambda: decode_u8_map(base64.b64encode(b"abc").decode(), 2, 2), "u8 map: 3 bytes for 2 x 2"),
        (lambda: decode_zlib_map(base64.b64encode(b"nope").decode(), 1, 1), "zlib map: "),
        (lambda: decode_zlib_map(base64.b64encode(zlib.compress(b"ab")).decode(), 1, 1), "zlib map: 2 bytes"),
        (lambda: decode_bitset("AA==", 9), "bitset: 1 bytes for 9 bits"),
        (lambda: decode_f32le("AAA="), "f32le: 2 bytes is not a multiple of 4"),
        (lambda: decode_u16le("AAAA"), "u16le: 3 bytes is not a multiple of 2"),
        (lambda: Joint.from_list([1, 2]), "joint: 2 numbers"),
        (lambda: StoneItem.from_list([1.0] * 10), "stone item: 10 numbers"),
    ],
)
def test_decode_errors(call: object, match: str) -> None:
    with pytest.raises(FactoryError, match=match) as info:
        call()  # type: ignore[operator]
    assert info.value.code == "decode"


def test_bitset_order() -> None:
    assert decode_bitset(base64.b64encode(bytes([0b00000101, 0b1])).decode(), 9) == [
        True,
        False,
        True,
        False,
        False,
        False,
        False,
        False,
        True,
    ]


def test_joints_and_stone_items(frame: Frame) -> None:
    assert frame.raw is not None
    assert frame.raw.person.joints is not None
    js = joints(frame.raw.person.joints)
    assert len(js) == 70
    assert js[0] is None  # face keypoints are redacted
    present = [j for j in js if j is not None]
    assert present
    assert 0.0 <= present[0].vis <= 1.0
    assert frame.raw.stones.items is not None
    items = stone_items(frame.raw.stones.items)
    assert len(items) == frame.raw.stones.count
    assert all(0.0 <= s.score <= 1.0 for s in items)
    assert Joint.from_list(None) is None


def test_coordinates() -> None:
    assert uv_to_px(0.5, 0.25, 1920, 1080) == (959.5, 269.5)
    assert px_to_uv(959.5, 269.5, 1920, 1080) == (0.5, 0.25)
    assert scene_to_three([1.0, 2.0, 3.0]) == [1.0, 3.0, -2.0]
    assert three_to_scene(scene_to_three([1.0, 2.0, 3.0])) == [1.0, 2.0, 3.0]
    half = math.sin(math.pi / 4)
    assert quat_to_yaw([0.0, 0.0, half, half]) == pytest.approx(math.pi / 2)
    assert quat_to_yaw([0.0, 0.0, 0.0, 1.0]) == 0.0


def test_skeleton(frame: Frame) -> None:
    sk = Skeleton(Topology.from_json(api_json("getTopology")))
    assert sk.joints_count == 70
    assert sk.wrist_joint("left") == 62
    assert sk.wrist_joint("up") is None
    assert sk.tip_joints("right") == [21, 25, 29, 33, 37]
    assert sk.tip_joints("up") == []
    assert frame.overlay is not None
    visible = sk.visible_bones(frame.overlay.joints)
    assert 0 < len(visible) < len(sk.bones)
    assert sk.visible_bones([]) == []
    assert all(a not in sk.never or b not in sk.never for a, b in visible) or sk.never

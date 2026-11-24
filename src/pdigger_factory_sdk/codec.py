"""Low level: decoders for the layers' packed fields and coordinate helpers (SDK-DESIGN.md section 7).

Pure Python, no NumPy. Maps decode to a `Grid` of u8 cells (row-major); turn one into a NumPy array with
`numpy.frombuffer(grid.data, numpy.uint8).reshape(grid.rows, grid.cols)` if you have NumPy.

Coordinates: native px are pixels of the model crop with pixel centres at integers (`px = u * W - 0.5`); content units
`u, v` are in [0, 1]; the scene world has z up, the board top at z = 0, in the template gauge (not metres); three.js
axes are `(x, y, z) -> (x, z, -y)`.
"""

from __future__ import annotations

import base64
import binascii
import math
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import FactoryError
from .models import Topology

__all__ = [
    "Grid",
    "Joint",
    "Skeleton",
    "StoneItem",
    "decode_bitset",
    "decode_depth",
    "decode_f32le",
    "decode_u8_map",
    "decode_u16le",
    "decode_zlib_map",
    "depth_log_z",
    "depth_z",
    "joints",
    "px_to_uv",
    "quat_to_yaw",
    "scene_to_three",
    "stone_items",
    "three_to_scene",
    "uv_to_px",
]


def _b64(data: str, what: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise FactoryError(code="decode", message=f"{what}: not base64 ({e})") from None


@dataclass(slots=True, frozen=True)
class Grid:
    """A `rows` x `cols` map of u8 cells, row-major."""

    rows: int
    cols: int
    data: bytes

    def at(self, row: int, col: int) -> int:
        """The cell at `row`, `col`."""
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise IndexError(f"cell ({row}, {col}) outside {self.rows} x {self.cols}")
        return self.data[row * self.cols + col]

    def row(self, row: int) -> bytes:
        """One row's cells."""
        if not 0 <= row < self.rows:
            raise IndexError(f"row {row} outside {self.rows}")
        return self.data[row * self.cols : (row + 1) * self.cols]

    def to_lists(self) -> list[list[int]]:
        """Every row as a list of ints."""
        return [list(self.row(r)) for r in range(self.rows)]

    def count(self, predicate: int | None = None) -> int:
        """Cells that are non-zero, or equal to `predicate` when given."""
        if predicate is None:
            return sum(1 for b in self.data if b)
        return self.data.count(bytes([predicate]))


def _grid(raw: bytes, rows: int, cols: int, what: str) -> Grid:
    if len(raw) != rows * cols:
        raise FactoryError(code="decode", message=f"{what}: {len(raw)} bytes for {rows} x {cols}")
    return Grid(rows=rows, cols=cols, data=raw)


def decode_u8_map(b64: str | None, rows: int, cols: int) -> Grid | None:
    """An overlay map (base64 of u8, row-major), or `None` for an absent (redacted) map."""
    if b64 is None:
        return None
    return _grid(_b64(b64, "u8 map"), rows, cols, "u8 map")


def decode_zlib_map(b64: str | None, rows: int, cols: int) -> Grid | None:
    """A `sip1` map (base64 of zlib of u8, row-major), or `None` for an absent (redacted) map."""
    if b64 is None:
        return None
    try:
        raw = zlib.decompress(_b64(b64, "zlib map"))
    except zlib.error as e:
        raise FactoryError(code="decode", message=f"zlib map: {e}") from None
    return _grid(raw, rows, cols, "zlib map")


def decode_bitset(b64: str | None, n: int) -> list[bool] | None:
    """`n` booleans packed LSB-first in each byte (surface `flat`, `quads`), or `None` when absent."""
    if b64 is None:
        return None
    raw = _b64(b64, "bitset")
    if len(raw) * 8 < n:
        raise FactoryError(code="decode", message=f"bitset: {len(raw)} bytes for {n} bits")
    return [bool(raw[i >> 3] >> (i & 7) & 1) for i in range(n)]


def depth_log_z(q: float, lo: float, hi: float, a: float, b: float) -> float:
    """`logZ = a * (lo + q / 255 * (hi - lo)) + b` for a quantised depth `q` (0..255)."""
    return a * (lo + q / 255.0 * (hi - lo)) + b


def depth_z(q: float, lo: float, hi: float, a: float, b: float) -> float:
    """`Z = exp(logZ)`: the depth of a quantised `q`, in the template gauge."""
    return math.exp(depth_log_z(q, lo, hi, a, b))


def decode_depth(grid: Grid | None, lo: float, hi: float, a: float, b: float) -> list[float] | None:
    """Every cell of a quantised depth map as `Z` (row-major), or `None` for an absent (redacted) map."""
    if grid is None:
        return None
    table = [depth_z(q, lo, hi, a, b) for q in range(256)]
    return [table[q] for q in grid.data]


def decode_f32le(b64: str | None) -> list[float] | None:
    """Little-endian float32 values (surface positions: x, y, z triplets), or `None` when absent."""
    if b64 is None:
        return None
    raw = _b64(b64, "f32le")
    if len(raw) % 4:
        raise FactoryError(code="decode", message=f"f32le: {len(raw)} bytes is not a multiple of 4")
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def decode_u16le(b64: str | None) -> list[int] | None:
    """Little-endian uint16 values (surface triangle indices), or `None` when absent."""
    if b64 is None:
        return None
    raw = _b64(b64, "u16le")
    if len(raw) % 2:
        raise FactoryError(code="decode", message=f"u16le: {len(raw)} bytes is not a multiple of 2")
    return list(struct.unpack(f"<{len(raw) // 2}H", raw))


@dataclass(slots=True, frozen=True)
class Joint:
    """A skeleton joint `[u, v, vis, x, y, z]`: content units, visibility, camera-frame template metres."""

    u: float
    v: float
    vis: float
    x: float
    y: float
    z: float

    @classmethod
    def from_list(cls, values: Sequence[float] | None) -> Joint | None:
        """A joint from its six numbers, or `None` for a null (absent or redacted) joint."""
        if values is None:
            return None
        if len(values) < 6:
            raise FactoryError(code="decode", message=f"joint: {len(values)} numbers, expected 6")
        return cls(*(float(x) for x in values[:6]))


def joints(values: Sequence[Sequence[float] | None]) -> list[Joint | None]:
    """Every joint of a `joints` array (null entries stay `None`)."""
    return [Joint.from_list(j) for j in values]


@dataclass(slots=True, frozen=True)
class StoneItem:
    """A `sip1` stone item `[u, v, w, h, score, X, Y, Z, w_m, h_m, yaw]`: its box in content units, the model's score,
    the camera-frame centre, its size in the template gauge and its yaw."""

    u: float
    v: float
    w: float
    h: float
    score: float
    X: float
    Y: float
    Z: float
    w_m: float
    h_m: float
    yaw: float

    @classmethod
    def from_list(cls, values: Sequence[float]) -> StoneItem:
        """A stone item from its eleven numbers."""
        if len(values) < 11:
            raise FactoryError(code="decode", message=f"stone item: {len(values)} numbers, expected 11")
        return cls(*(float(x) for x in values[:11]))


def stone_items(values: Sequence[Sequence[float]]) -> list[StoneItem]:
    """Every item of `raw.stones.items` (or of `overlay.items[k].t`)."""
    return [StoneItem.from_list(v) for v in values]


def uv_to_px(u: float, v: float, width: float, height: float) -> tuple[float, float]:
    """Content units to native px: `px = u * W - 0.5`, `py = v * H - 0.5` (pixel centres at integers)."""
    return u * width - 0.5, v * height - 0.5


def px_to_uv(px: float, py: float, width: float, height: float) -> tuple[float, float]:
    """Native px to content units, the inverse of `uv_to_px`."""
    return (px + 0.5) / width, (py + 0.5) / height


def scene_to_three(p: Sequence[float]) -> list[float]:
    """Scene world `[x, y, z]` (z up) to three.js axes `[x, z, -y]` (y up)."""
    return [p[0], p[2], -p[1]]


def three_to_scene(p: Sequence[float]) -> list[float]:
    """three.js axes `[x, y, z]` back to the scene world `[x, -z, y]`."""
    return [p[0], -p[2], p[1]]


def quat_to_yaw(q: Sequence[float]) -> float:
    """The rotation about z (radians) of a quaternion `[x, y, z, w]`, as `solid.q` gives it."""
    x, y, z, w = q[0], q[1], q[2], q[3]
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class Skeleton:
    """Topology helpers from `getTopology`: bones, wrist and fingertip joints, and the bones some joints can draw."""

    __slots__ = ("bones", "joints_count", "never", "tips", "wrist")

    def __init__(self, topology: Topology) -> None:
        self.joints_count = topology.joints
        self.bones: list[tuple[int, int]] = [(int(a), int(b)) for a, b in topology.bones]
        self.never = frozenset(int(j) for j in topology.never)
        self.wrist = {"left": topology.wrist.left, "right": topology.wrist.right}
        self.tips = {side: [int(j) for j in ids] for side, ids in topology.tips.items()}

    def wrist_joint(self, side: str) -> int | None:
        """The wrist joint index of `left` or `right`."""
        return self.wrist.get(side)

    def tip_joints(self, side: str) -> list[int]:
        """The fingertip joint indices of `left` or `right`."""
        return list(self.tips.get(side, []))

    def visible_bones(self, joints: Sequence[object | None]) -> list[tuple[int, int]]:
        """The bones whose two joints are present (null joints, for example redacted face keypoints, are skipped)."""
        n = len(joints)
        return [(a, b) for a, b in self.bones if a < n and b < n and joints[a] is not None and joints[b] is not None]

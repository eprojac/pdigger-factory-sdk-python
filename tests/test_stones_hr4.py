"""The raw packet's 4x stones block (API 2.2, `Sip1Stones`): exactly the spec's fields typed, every map on its own grid.

`data/sample-packet-hr-dry1.json` is a packet of the 4x stone head (dry1, g 736), redacted as the published recordings
are: no face keypoints, appearance masks or depth texture.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from pdigger_factory_sdk.codec import decode_zlib_map
from pdigger_factory_sdk.models import Sip1Packet, Sip1Stones

from .support import SPEC

PACKET: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent / "data" / "sample-packet-hr-dry1.json").read_text(encoding="utf-8")
)
TYPED = {f.name for f in dataclasses.fields(Sip1Stones)} - {"raw_json"}
# The stones block of API 2.2.0: the base block and the 4x head's source, centres and centre_grid.
BASE = {"present", "count", "box", "rot", "grid", "fg", "tray_grid", "tray", "items"}
STONES = BASE | {"source", "centres", "centre_grid"}


def test_stones_type_is_the_specs_block() -> None:
    spec = json.loads((SPEC / "openapi.json").read_text(encoding="utf-8"))
    assert set(spec["components"]["schemas"]["Sip1Packet"]["properties"]["stones"]["properties"]) == STONES
    assert TYPED == STONES


def test_4x_block_is_typed() -> None:
    stones = Sip1Packet.from_json(PACKET).stones
    assert set(PACKET["stones"]) <= TYPED, set(PACKET["stones"]) - TYPED
    assert (stones.source, stones.centres, stones.centre_grid) == ("hr4", "base", [20, 32])
    assert (stones.grid, stones.tray_grid) == ([160, 256], [80, 128])
    assert stones.items is not None
    assert stones.count == len(stones.items) == 39


def test_4x_maps_decode_on_their_grids() -> None:
    stones = Sip1Packet.from_json(PACKET).stones
    assert stones.grid is not None
    assert stones.tray_grid is not None
    fg = decode_zlib_map(stones.fg, *stones.grid)
    assert fg is not None
    assert (fg.rows, fg.cols, len(fg.data)) == (160, 256, 40960)
    assert 0 < fg.count() < 40960
    tray = decode_zlib_map(stones.tray, *stones.tray_grid)
    assert tray is not None
    assert (tray.rows, tray.cols, len(tray.data)) == (80, 128, 10240)


def test_base_block_has_none_of_the_4x_fields() -> None:
    base = {k: v for k, v in PACKET["stones"].items() if k in BASE}
    assert set(base) == BASE
    stones = Sip1Stones.from_json(base)
    assert (stones.source, stones.centres, stones.centre_grid) == (None, None, None)
    assert stones.raw_json is base

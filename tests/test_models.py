"""Typed models: every schema of the spec parses its own samples, and every recorded reply parses."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from pdigger_factory_sdk import FactoryError, models

from .support import ROOT, SPEC, api_index, api_json, frame_json, recording_frames

SCHEMAS: dict[str, Any] = json.loads((SPEC / "openapi.json").read_text(encoding="utf-8"))["components"]["schemas"]


def sample(schema: dict[str, Any], full: bool) -> Any:
    """A value that fits `schema`: every required field (and with `full` every field), first alternatives."""
    if "$ref" in schema:
        return sample(SCHEMAS[schema["$ref"].split("/")[-1]], full)
    if "allOf" in schema:
        out: dict[str, Any] = {}
        for part in schema["allOf"]:
            out.update(sample(part, full))
        return out
    for key in ("oneOf", "anyOf"):
        if key in schema:
            alts = [a for a in schema[key] if a.get("type") != "null"]
            return sample(alts[0], full)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return next(e for e in schema["enum"] if e is not None)
    t = schema.get("type")
    if isinstance(t, list):
        t = next(x for x in t if x != "null")
    if t == "string":
        return "x"
    if t == "integer":
        return int(schema.get("minimum", 1))
    if t == "number":
        return float(schema.get("minimum", 0.5))
    if t == "boolean":
        return True
    if t == "array":
        n = max(int(schema.get("minItems", 1)), 1)
        n = min(n, int(schema.get("maxItems", n)))
        return [sample(schema["items"], full) for _ in range(n)] if "items" in schema else [1] * n
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        obj = {k: sample(v, full) for k, v in props.items() if full or k in required}
        extra = schema.get("additionalProperties")
        if full and isinstance(extra, dict):
            obj["extra_key"] = sample(extra, full)
        return obj
    return 1


PARSED = sorted(models.SCHEMAS)


@pytest.mark.parametrize("name", PARSED)
@pytest.mark.parametrize("full", [False, True], ids=["required", "full"])
def test_schema_samples_parse(name: str, full: bool) -> None:
    data = sample(SCHEMAS[name], full)
    obj = models.SCHEMAS[name].from_json(data)
    assert obj.raw_json is data


def nullable(schema: dict[str, Any]) -> bool:
    t = schema.get("type")
    alts = schema.get("oneOf") or schema.get("anyOf") or []
    return (isinstance(t, list) and "null" in t) or any(a.get("type") == "null" for a in alts)


def strict_field(name: str) -> str | None:
    """A required field that may not be null (a required but nullable one parses as `None` when absent)."""
    props = SCHEMAS[name].get("properties") or {}
    return next((k for k in sorted(SCHEMAS[name].get("required") or []) if not nullable(props.get(k, {}))), None)


@pytest.mark.parametrize("name", [n for n in PARSED if strict_field(n)])
def test_missing_required_field_is_a_decode_error(name: str) -> None:
    data = sample(SCHEMAS[name], False)
    field = strict_field(name)
    assert field is not None
    del data[field]
    with pytest.raises(FactoryError) as info:
        models.SCHEMAS[name].from_json(data)
    assert info.value.code == "decode"
    assert repr(field) in info.value.message


def test_not_an_object() -> None:
    with pytest.raises(FactoryError, match="State: expected an object, got str"):
        models.State.from_json("nope")


REPLIES: dict[str, Any] = {  # operation reply -> its model class
    "getSession": models.SessionReply,
    "getApiRoot": models.ApiRoot,
    "getModel": models.ModelInfo,
    "getTopology": models.Topology,
    "listVideos": models.VideoList,
    "getVideo": models.Source,
    "getHealth": models.Health,
    "getReady": models.Ready,
    "getStatus": models.Status,
    "getFocus": models.FocusReply,
    "getWorld": models.WorldLayer,
    "getWorldEvents": models.EventLog,
    "getFrame-latest": models.Frame,
    "rawPacket-latest": models.Sip1Packet,
    "pollFrames-world": models.LiveReply,
    "renderOverlay2d": models.Overlay2D,
    "renderOverlay2d-primitives": models.Overlay2D,
    "renderScene3d": models.Scene3D,
    "renderScene3d-compact": models.Scene3D,
    "renderScene3d-none": models.Scene3D,
    "getStone": models.StoneTrack,
    "putFocus-dry": models.ControlReply,
    "sessionPause-dry": models.ControlReply,
    "deleteFocus-dry": models.ControlReply,
}


@pytest.mark.parametrize("key", sorted(REPLIES))
def test_recorded_replies_parse(key: str) -> None:
    data = api_json(key)
    obj = REPLIES[key].from_json(data)
    assert obj.raw_json == data


def test_every_recorded_json_reply_is_checked() -> None:
    index = api_index()
    json_ok = {
        k for k, v in index.items() if v["status"] < 400 and v.get("content_type", "").startswith("application/json")
    }
    assert json_ok <= set(REPLIES)


@pytest.mark.parametrize("name", ["dry1-carry-full", "dry1-plain-full"])
def test_full_frames_parse(name: str) -> None:
    frame = models.Frame.from_json(frame_json(name))
    assert frame.raw is not None
    assert frame.overlay is not None
    assert frame.scene is not None
    assert frame.cards is not None
    assert frame.world is not None
    assert frame.raw.maps.human is None  # redacted fields are optional
    assert frame.missing == []


def test_recording_frames_parse() -> None:
    for data in recording_frames("dry2-0582-0600"):
        frame = models.Frame.from_json(data)
        assert frame.world is not None or frame.missing == ["world"]


def test_sources() -> None:
    catalog = models.VideoList.from_json(api_json("listVideos"))
    assert catalog.videos
    assert [v.is_live for v in catalog.videos] == [False] * 4 + [True] * 6  # the recordings, then six cameras
    live = models.Source.from_json({**api_json("getVideo"), "id": "live", "kind": "camera", "duration": None})
    assert live.is_live
    # a camera field the entry leaves out is filled from the others: `kind: camera` alone is live and not seekable
    assert (live.kind, live.live, live.seekable) == ("camera", True, False)
    alone = models.Source.from_json({**api_json("getVideo"), "live": True})
    assert alone.is_live
    assert (alone.kind, alone.live, alone.seekable) == ("camera", True, False)
    recording = models.Source.from_json(api_json("getVideo"))  # the unit's recordings send none of them
    assert (recording.kind, recording.live, recording.seekable) == ("recording", False, True)
    assert models.Source.from_json({**api_json("getVideo"), "kind": "camera", "live": True, "seekable": True}).seekable
    assert models.VideoEntry is models.Source


def test_generated_models_are_current() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_models.py"), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr

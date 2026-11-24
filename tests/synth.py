"""Synthetic frames and worlds for rule-by-rule tests (the shapes of the `Frame` and `WorldLayer` schemas)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

HEADER = {"recording": "pfr1", "video": "dry1", "build": "synthetic", "layers": ["world"]}


def stone(sid: int, x: float = 0.0, y: float = 0.0) -> dict[str, Any]:
    return {
        "id": sid,
        "mode": "kept",
        "family": "rect",
        "shape": "frozen",
        "detected": True,
        "uncertain": False,
        "side": None,
        "outline": [[10.0, 10.0], [20.0, 10.0], [20.0, 20.0], [10.0, 20.0]],
        "solid": {"pos": [x, y, 0.046], "q": [0.0, 0.0, 0.0, 1.0], "dims": [0.3, 0.2, 0.09]},
    }


def ev(type: str, sid: int | None = None, side: str | None = None, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": type}
    if sid is not None:
        out["id"] = sid
    if side is not None:
        out["side"] = side
    out.update(kw)
    return out


def world(
    g: int,
    t: float,
    *,
    session: int = 1,
    video: str = "dry1",
    stones: Iterable[dict[str, Any]] = (),
    events: Sequence[dict[str, Any]] = (),
    left: str = "free",
    right: str = "free",
    left_stone: int | None = None,
    right_stone: int | None = None,
    rev: int | None = None,
    confirmed: int | None = None,
) -> dict[str, Any]:
    items = list(stones)
    out: dict[str, Any] = {
        "schema": "stw1",
        "session": session,
        "video": video,
        "g": g,
        "t": t,
        "motion": "static",
        "logF": 0.08,
        "stones": items,
        "hands": {"left": {"state": left, "stone": left_stone}, "right": {"state": right, "stone": right_stone}},
        "events": list(events),
        "stats": {"stones": len(items), "confirmed": len(items) if confirmed is None else confirmed},
    }
    if rev is not None:
        out["rev"] = rev
    return out


def frame(
    g: int,
    *,
    t: float | None = None,
    session: int = 1,
    video: str = "dry1",
    cursor: int | None = None,
    missing: Sequence[str] | None = None,
    cards: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
    with_world: bool = True,
    **world_kw: Any,
) -> dict[str, Any]:
    t = round(g * 0.2, 6) if t is None else t
    out: dict[str, Any] = {
        "cursor": g + 1 if cursor is None else cursor,
        "session": session,
        "video": video,
        "g": g,
        "i": g * 2,
        "t": t,
        "past_valid": True,
        "dt_past": 0.6,
    }
    if with_world:
        out["world"] = world(g, t, session=session, video=video, **world_kw)
    if missing:
        out["missing"] = list(missing)
    if cards is not None:
        out["cards"] = cards
    if raw is not None:
        out["raw"] = raw
    return out


def card(title: str) -> dict[str, Any]:
    return {"title": title, "tone": "hold", "visible": True, "fine": []}


def cards(left: str, right: str) -> dict[str, Any]:
    return {"schema": "crd1", "left": card(left), "right": card(right), "numbers": {}}


def clock_line(session: int = 1, video: str = "dry1", revision: int = 1, **kw: Any) -> dict[str, Any]:
    data = {
        "session": session,
        "revision": revision,
        "video": video,
        "playing": True,
        "rate": 0.8,
        "anchor_media": 0.0,
        "anchor_wall": 1000.0,
        "hold_until": 1001.0,
        "duration": 1800.0,
        "changed_by": "viewer",
        "focus": None,
        "media_time": 0.0,
        "server_now": 1000.0,
    }
    data.update(kw)
    return {"sse": "clock", "data": data, "recv": 1000.0}


def worker_line(state: str = "running", reason: str = "") -> dict[str, Any]:
    return {"sse": "worker", "data": {"state": state, "reason": reason}, "recv": 1000.0}

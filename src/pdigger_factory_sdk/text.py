"""Numbers and human-readable text, identical in every pDigger Factory SDK (SDK-DESIGN.md sections 4.1-4.5).

Every function here is pure. `fmt` and `clock` never use Python's float formatting, which rounds ties differently from
the other languages: they compute `floor(x * 10**d + 0.5)` in IEEE doubles and place the decimal point in the integer.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

__all__ = [
    "EVENT_TYPES",
    "HAND_STATES",
    "EventLike",
    "MoveLike",
    "PushLike",
    "TrayFactLike",
    "clock",
    "describe_event",
    "describe_move",
    "describe_push",
    "describe_summary",
    "describe_tray_fact",
    "event_sentence",
    "fmt",
    "hand_word",
    "join_ids",
    "milli",
    "round3",
    "verb",
]

EVENT_TYPES = (
    "new",
    "confirm",
    "freeze",
    "push",
    "lift",
    "hinge",
    "carry",
    "place",
    "drop",
    "gone",
    "delete",
    "split",
    "sheet",
)
"""The world's event types, in the order the summary lists them (`other` counts the rest)."""

HAND_STATES = ("free", "touch", "grasp", "hinge", "carry")
"""The hand states the summary counts."""

DELETE_REASONS = {
    "absent": "no longer seen",
    "overlap": "overlapped another stone",
    "outside": "outside the board",
    "merged": "merged with a neighbour",
    "empty": "its place is empty",
    "hand": "a hand emptied its place",
    "carried-away": "carried away",
    "sheet": "gone from under a sheet",
}
"""The text of a `delete` event's `why`; any other `why` is printed as it is."""

_PLURAL = {
    "pushes": "push",
    "lifts": "lift",
    "tips": "tip",
    "carries": "carry",
    "places": "place",
    "drops": "drop",
    "takes": "take",
    "moves": "move",
    "is": "are",
}


class _RefLike(Protocol):
    @property
    def t(self) -> float: ...


class EventLike(Protocol):
    """What `describe_event` reads (an `interpreter.Event` has it)."""

    @property
    def t(self) -> float: ...
    @property
    def type(self) -> str: ...
    @property
    def stone_id(self) -> int | None: ...
    @property
    def hand(self) -> str | None: ...
    @property
    def why(self) -> str | None: ...
    @property
    def into(self) -> Sequence[int] | None: ...
    @property
    def source(self) -> str | None: ...


class MoveLike(Protocol):
    """What `describe_move` reads (an `interpreter.Move` has it)."""

    @property
    def hand(self) -> str | None: ...
    @property
    def stone_id(self) -> int | None: ...
    @property
    def start(self) -> _RefLike: ...
    @property
    def end(self) -> _RefLike | None: ...
    @property
    def duration(self) -> float | None: ...
    @property
    def outcome(self) -> str | None: ...
    @property
    def carried(self) -> bool: ...
    @property
    def tipped(self) -> bool: ...
    @property
    def partial(self) -> bool: ...
    @property
    def distance(self) -> float | None: ...


class PushLike(Protocol):
    """What `describe_push` reads (an `interpreter.PushBurst` has it)."""

    @property
    def hand(self) -> str | None: ...
    @property
    def start(self) -> _RefLike: ...
    @property
    def duration(self) -> float | None: ...
    @property
    def stones(self) -> Sequence[int]: ...
    @property
    def pushes(self) -> int: ...


class TrayFactLike(Protocol):
    """What `describe_tray_fact` reads (a `trays.TrayFact` has it)."""

    @property
    def t(self) -> float: ...
    @property
    def kind(self) -> str: ...
    @property
    def tray(self) -> int: ...
    @property
    def stone(self) -> str | None: ...
    @property
    def certain(self) -> bool | None: ...
    @property
    def why(self) -> str | None: ...
    @property
    def stones(self) -> int | None: ...
    @property
    def sure(self) -> int | None: ...
    @property
    def added(self) -> int | None: ...
    @property
    def removed(self) -> int | None: ...
    @property
    def unclear(self) -> int | None: ...
    @property
    def code(self) -> str | None: ...
    @property
    def world_id(self) -> int | None: ...
    @property
    def score(self) -> int | None: ...
    @property
    def opened(self) -> PointLike | None: ...


class PointLike(Protocol):
    """A point with a media time (a `trays.TrayPoint` has it)."""

    @property
    def t(self) -> float: ...


def milli(p: int) -> str:
    """An integer in permille with three decimals, from integers only: 853 is `0.853`, 1000 is `1.000`, -5 is
    `-0.005` (SDK-DESIGN.md 4.6.8)."""
    if p < 0:
        return "-" + milli(-p)
    return f"{p // 1000}.{p % 1000:03d}"


def fmt(x: float, d: int) -> str:
    """`x` with `d` decimals: `n = floor(x * 10**d + 0.5)`, then the decimal point placed in `n` (a negative `x`
    prints `-` and `fmt(-x, d)`)."""
    if x < 0:
        return "-" + fmt(-x, d)
    s = str(math.floor(x * (10**d) + 0.5))
    if d == 0:
        return s
    s = s.rjust(d + 1, "0")
    return s[:-d] + "." + s[-d:]


def round3(x: float) -> float:
    """`floor(x * 1000 + 0.5) / 1000`, the rounding of the JSON summary."""
    return math.floor(x * 1000 + 0.5) / 1000


def clock(t: float) -> str:
    """Media time as `M:SS.s` below one hour (`0:05.2`, `21:17.8`), else `H:MM:SS.s` (`1:02:03.4`)."""
    n = math.floor(t * 10 + 0.5)
    h, m, s, d = n // 36000, (n // 600) % 60, (n // 10) % 60, n % 10
    return f"{m}:{s:02d}.{d}" if h == 0 else f"{h}:{m:02d}:{s:02d}.{d}"


def hand_word(side: str | None) -> str:
    """`Left hand`, `Right hand`, `Both hands`, or `A hand` for an event without a side."""
    return {"left": "Left hand", "right": "Right hand", "both": "Both hands"}.get(side or "", "A hand")


def verb(side: str | None, word: str) -> str:
    """The verb agreeing with the hand: the plural (`push`) for both hands, else as given (`pushes`)."""
    return _PLURAL[word] if side == "both" else word


def join_ids(ids: Sequence[int | str | None]) -> str:
    """`3`, `3 and 5`, `3, 5 and 9` (a missing id prints `?`, no ids print `?`)."""
    parts = [_id(i) for i in ids] or ["?"]
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _id(i: int | str | None) -> str:
    return "?" if i is None else str(i)


def _stones(ids: Sequence[int | str]) -> str:
    if len(ids) <= 1:
        return f"stone {join_ids(ids)}"
    if len(ids) <= 6:
        return "stones " + join_ids(ids)
    return "stones " + ", ".join(str(i) for i in ids[:6]) + f" and {len(ids) - 6} more"


def event_sentence(
    type: str,
    stone_id: int | None,
    hand: str | None = None,
    why: str | None = None,
    into: Sequence[int] | None = None,
    source: str | None = None,
    ids: Sequence[int] | None = None,
) -> str:
    """The sentence of an event (section 4.1), without its clock."""
    i = _id(stone_id)
    h = hand_word(hand)
    if type == "new":
        return f"stone {i} appears" + (" (found in the image)" if source == "image" else "")
    if type == "confirm":
        return f"stone {i} is confirmed"
    if type == "freeze":
        return f"stone {i} gets its final shape"
    if type == "push":
        if why == "course":
            return f"stone {i} is pushed along its row"
        if why == "reseat":
            return f"stone {i} is reseated"
        if hand:
            return f"{h} {verb(hand, 'pushes')} stone {i}"
        return f"stone {i} is pushed" + (f" ({why})" if why else "")
    if type == "lift":
        return f"{h} {verb(hand, 'lifts')} stone {i}"
    if type == "hinge":
        return f"{h} {verb(hand, 'tips')} stone {i} up on its edge"
    if type == "carry":
        return f"{h} {verb(hand, 'carries')} stone {i}"
    if type == "place":
        return f"{h} {verb(hand, 'places')} stone {i}"
    if type == "drop":
        return f"{h} {verb(hand, 'drops')} stone {i}"
    if type == "gone":
        return f"{h} {verb(hand, 'takes')} stone {i} out of view"
    if type == "delete":
        return f"stone {i} is removed" + ("" if why is None else f" ({DELETE_REASONS.get(why, why)})")
    if type == "split":
        if not into:
            return f"stone {i} splits"
        if len(into) == 1:
            return f"stone {i} is trimmed"
        return f"stone {i} splits into {join_ids(into)}"
    if type == "sheet":
        if why in ("laid", "lifted"):
            prep = "over" if why == "laid" else "off"
            return f"a sheet is {why}" + (f" {prep} {_stones(ids)}" if ids else "")
        return "a sheet event" + (f" ({why})" if why else "")
    return f"stone {i}: {type}"


def describe_event(event: EventLike) -> str:
    """`"{clock} {sentence}"`, for example `21:17.8 Right hand lifts stone 2660`."""
    sentence = event_sentence(
        event.type, event.stone_id, event.hand, event.why, event.into, event.source, getattr(event, "ids", None)
    )
    return f"{clock(event.t)} {sentence}"


def describe_push(burst: PushLike) -> str:
    """A push burst (section 4.2), for example `0:20.5 Both hands push stones 86, 90 and 94 (6 pushes in 0.6 s)`."""
    side = burst.hand
    what = _stones(burst.stones)
    n = burst.pushes
    tail = "(1 push)" if n == 1 else f"({n} pushes in {fmt(burst.duration or 0.0, 1)} s)"
    return f"{clock(burst.start.t)} {hand_word(side)} {verb(side, 'pushes')} {what} {tail}"


def describe_move(move: MoveLike) -> str:
    """A move (section 4.2), for example `0:50.5 Left hand drops stone 128 after 0.2 s`."""
    side = move.hand
    h = hand_word(side)
    i = _id(move.stone_id)
    dur = fmt(move.duration or 0.0, 1)
    out = move.outcome
    if out == "placed":
        if move.carried:
            if move.distance is None:
                s = f"{h} {verb(side, 'moves')} stone {i} in {dur} s"
            else:
                s = f"{h} {verb(side, 'moves')} stone {i} {fmt(move.distance, 2)} units in {dur} s"
        elif move.tipped:
            s = f"{h} {verb(side, 'tips')} stone {i} and sets it down ({dur} s)"
        else:
            s = f"{h} {verb(side, 'lifts')} stone {i} and sets it down ({dur} s)"
    elif out == "dropped":
        s = f"{h} {verb(side, 'drops')} stone {i} after {dur} s"
    elif out == "carried_away":
        s = f"{h} {verb(side, 'takes')} stone {i} away ({dur} s)"
    elif out == "removed":
        s = f"{h} {verb(side, 'lifts')} stone {i}; it is removed after {dur} s"
    else:
        s = f"{h} {verb(side, 'is')} still holding stone {i} ({dur} s so far)"
    end = move.end
    if move.partial and end is not None:
        return f"{clock(end.t)} {s} (joined mid-move)"
    return f"{clock(move.start.t)} {s}"


def describe_tray_fact(fact: TrayFactLike) -> str:
    """A tray fact (section 4.6), for example `0:45.5 tray 1: stone 20261122T023847.0Z-1-117 was removed`."""
    c, n, k, i = clock(fact.t), fact.tray, fact.kind, fact.stone
    if k == "arrived":
        return f"{c} tray {n} arrived: {fact.stones} stones"
    if k == "sheet":
        action = "laid over" if fact.code == "sheet_laid" else "lifted off"
        return f"{c} tray {n}: a sheet was {action} {_stones(getattr(fact, 'tray_stones', ()))}"
    if k == "leaving":
        under = getattr(fact, "under_sheet", 0)
        return (
            f"{c} tray {n} leaving: {fact.stones} stones ({fact.sure} sure), {fact.added} added, "
            f"{fact.removed} removed, {fact.unclear} unclear" + (f", {under} under a sheet" if under else "")
        )
    if k == "unsure":
        if fact.opened is not None:
            return f"{c} tray {n}: stone {i} is not sure any more ({fact.why}; open since {clock(fact.opened.t)})"
        return f"{c} tray {n}: stone {i} is not sure any more ({fact.why})"
    if k == "identified":
        score = "-" if fact.score is None else milli(fact.score)
        if fact.code in ("speckle_same", "speckle_moved", "speckle_found"):
            how = {"speckle_same": "confirmed", "speckle_moved": "recognised", "speckle_found": "found again"}[
                fact.code
            ]
            return f"{c} tray {n}: stone {i} {how} by its speckle on world stone {fact.world_id} ({score})"
        how = "found again" if fact.code == "look_found" else "recognised"
        return f"{c} tray {n}: stone {i} {how} by its look on world stone {fact.world_id} ({score})"
    if k == "removed":
        if fact.certain:
            return f"{c} tray {n}: stone {i} was removed" + (" (carried away)" if fact.why == "carried away" else "")
        return f"{c} tray {n}: stone {i} not found (unclear)"
    if fact.certain:
        return f"{c} tray {n}: stone {i} was set down"
    return f"{c} tray {n}: stone {i} not recognised (unclear)"


_NO_TRAYS = dict.fromkeys(
    ("total", "arrived", "stones", "added", "added_unclear", "removed", "removed_unclear", "unsure"), 0
)


def describe_summary(summary: Mapping[str, Any]) -> str:
    """The text of a summary's JSON form (section 4.4), `\\n` after every line."""

    def d(x: Any) -> str:
        return "-" if x is None else str(x)

    m, p, e, hands = summary["moves"], summary["push_bursts"], summary["events"], summary["hands"]
    stones, trays = summary["stones"], summary.get("trays") or _NO_TRAYS  # a summary from before trays: none
    mean_d = "-" if m["mean_duration"] is None else fmt(m["mean_duration"], 1)
    mean_u = "-" if m["mean_distance"] is None else fmt(m["mean_distance"], 2)
    lines = [
        f"frames: {summary['frames']} (with world {summary['frames_with_world']}, "
        f"without {summary['missing_world']}, revisions {summary['revisions']})",
        f"media: {clock(summary['media_seconds'])}, sessions {summary['sessions']}, gaps {summary['gaps']}",
        f"stones: last {d(stones['last'])}, max {stones['max']}, confirmed {d(stones['confirmed_last'])}",
        f"moves: {m['total']} (placed {m['placed']}, dropped {m['dropped']}, carried away {m['carried_away']}, "
        f"removed {m['removed']}, interrupted {m['interrupted']}, partial {m['partial']}, carried {m['carried']}), "
        f"mean {mean_d} s, {mean_u} units",
        f"push bursts: {p['total']} (left {p['left']}, right {p['right']}, both {p['both']}), pushes {p['pushes']}",
        "events: " + ", ".join(f"{k} {e[k]}" for k in (*EVENT_TYPES, "other") if k != "sheet" or e.get(k)),
        f"trays: {trays['total']} (arrived {trays['arrived']}), stones at arrival {trays['stones']}, "
        f"added {trays['added']} (+{trays['added_unclear']} unclear), removed {trays['removed']} "
        f"(+{trays['removed_unclear']} unclear), not sure {trays['unsure']}"
        + (
            f", sheets laid {trays.get('sheets', 0)}, under a sheet {trays.get('under_sheet', 0)}"
            if trays.get("sheets") or trays.get("under_sheet")
            else ""
        ),
    ]
    for side in ("left", "right"):
        lines.append(f"{side} hand: " + ", ".join(f"{k} {clock(hands[side][k])}" for k in HAND_STATES))
    return "\n".join(lines) + "\n"

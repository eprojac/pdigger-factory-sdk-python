"""Trays and tray stones: the SDK's own stone ids, one per stone, never reused (SDK-DESIGN.md 4.6).

The world's stone ids are the engine tracker's: it renames a stone it lost and found again, and it can keep an id on
another stone (a stone set down where a removed one lay, a neighbour pushed into its place, a row of equal stones pushed
one place along while the ids stay on the places). The interpreter follows each tray from the moment it has arrived and
settled to the moment it leaves, and keeps its own stones on it, the tray stones. A tray stone says whether its identity
is still **sure**; a fact about it is **certain** or **unclear**: an identity stays sure, and a fact is certain, only
when what the world shows at calm moments leaves no other reading. A certain fact is never revised.

    for report in Replay.open("fixtures/recordings/dry1-0018-0153.ndjson.gz").tray_reports():
        print(report.describe())  # tray 1 (2026-11-22T02:38:47.0Z), 0:22.5 to 2:24.0: 11 removed, 13 added, ...

A tray's report (`Tray.report()`, section 4.7) is simple mode: which stones were removed, added and moved, when and
where, and whether that is the whole account. The facts below it say why, stone by stone:

    for fact in Replay.open("fixtures/recordings/dry1-0018-0153.ndjson.gz").tray_facts():
        print(fact.describe())    # 0:45.5 tray 1: stone 20261122T023847.0Z-1-117 was removed

A stone without looks of its own (`TrayStone.looks` empty) that the world stops detecting while nothing could have
changed it is not given up at once: its doubt is a *question* (a presence question, 4.6.1: the stone is not sure
meanwhile, and no fact is given), which seeing it again where it was settles when no hand was at work, no push came on
the tray, no frame went unread and no other stone of its size stood at its place while it was out of sight.

When the world carries looks of the stones' faces (`StoneView.look`, 4.6.2), the rules also follow each stone that has a
look by it: a doubt a later look may settle is first a *question* too (a stone with looks that is not seen at two calm
moments included: only a look settles its question); a look settles it, or links a stone again (fact `identified`),
only above a bar the tray's own looks set; and a tray's certain facts are the first of those the rules make without
looks (once a look changes a link, doubts a stone they trust or keeps one from their certain removal, the tray makes no
more). Without usable looks the rules are those of the first version (4.6.1, presence questions
included), line for line. When a tray leaves, its open account (`Tray.open`) lists every stone the rules leave open
with the readings the evidence leaves.

What the SDK can not see: a box the model sees on an empty slot when the tray arrives becomes a tray stone, and a stone
set into that slot later keeps its id.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import text
from .looks import Cells, pair_cells, permille, usable_look
from .models import Rect, StoneView, WorldEvent, WorldLayer, WorldSpeckle

__all__ = [
    "Tray",
    "TrayCandidate",
    "TrayChange",
    "TrayCounts",
    "TrayFact",
    "TrayOpen",
    "TrayPoint",
    "TrayQuestion",
    "TrayReport",
    "TrayStone",
]

STOP_MIN = 2.0
"""A static stop of at least this many seconds is a tray visit."""
CALM = 3
"""A calm moment: this many frames in a row with both hands free, no hand event and no stone new or gone."""
QUIET = 5
"""Frames since the last `new` before a tray can arrive."""
RENAME_T = 3.0
"""A world rename: an unidentified stone first seen within this many seconds of the loss ..."""
RENAME_D = 0.15
"""... and this close to the lost stone's place."""
SIZE_TOL = 0.25
"""Two sizes are compatible when each side differs by at most this share of the larger."""
NEAR = 0.12
"""An unidentified stone at the same place at two calm moments: within this distance."""
HAND_R = 0.40
""""Next to": hand work, a pushed neighbour, a stone that appeared."""
LEAVE_D = 1.0
"""When a tray leaves, what is left is paired within this distance."""
GHOST = 2
"""Calm moments in a row a stone's world stone may go unseen before the stone is not sure."""
OVERLAP = 0.30
"""Share of a stone's box another world box may cover before the stone is not sure."""
MARGIN = 0.03
"""Margin of the empty-footprint check of a removal."""
# by look (4.6.11); scores are permille integers
T_ID_START = 900
"""The tray's bar while its gauge holds fewer than GAUGE_MIN pairs."""
T_ID_MIN = 750
"""The lowest bar: `T_id = max(T_ID_MIN, alike + GAP)`."""
GAP = 100
"""How far the bar lies above the most alike pair of two different stones of the tray."""
GAUGE_MIN = 20
"""Gauge pairs a tray needs before its bar can drop."""
GAUGE_K = 2
"""Gauge partners each taken look is scored with, both ways."""
T_FIRM = 800
"""The bar for a link change, or with a stone without a chain near."""
M_ID = 150
"""The margin a decision needs over every scored rival."""
N_ID = 100
"""Paired cells a decision or a doubt by look needs."""
T_DIFF = 400
"""Below this an untouched stone's own look changed."""
D_MATCH = 1.0
"""How far a pair reaches."""
D_NEIGH = 0.40
"""Neighbours of a column, and stones without a chain near."""
K_PAIR = 4
"""Pairs per row."""
K_NEIGH = 4
"""Neighbours per column."""
N_TAKE = 16
"""Looks taken per look work."""

_HAND_EVENTS = frozenset({"lift", "hinge", "carry", "place", "drop", "gone"})
_HELD = frozenset({"carry", "hinge"})
_HANDLED = frozenset({"lift", "carry", "hinge", "place"})
_ENDS = frozenset({"gone", "drop", "delete"})
_CHANGES = frozenset({"new", "delete", "split"})
_LOOK_CLEARERS = frozenset({"push", "lift", "hinge", "carry", "place", "drop", "gone", "delete", "split"})


def _hand_touch(e: WorldEvent) -> bool:
    """A hand touched the event's stone: a hand event, a `push` with a `side`, or a `delete` with why `hand` or
    `carried-away` (4.6.1, certain removals)."""
    return (
        e.type in _HAND_EVENTS
        or (e.type == "push" and bool(e.side))
        or (e.type == "delete" and e.why in ("hand", "carried-away"))
    )


UNSURE_CODES = (
    "overlap",
    "unseen",
    "pushed",
    "neighbour_pushed",
    "appeared",
    "size_changed",
    "found_again",
    "matched_at_leaving",
    "look_changed",
)
"""The codes of `unsure` facts (4.6.9), which are also the causes of open `unsure` entries."""
REMOVED_CAUSES = (
    "look_lost",
    "by_look",
    "twins",
    "relinked",
    "lost_late",
    "covered",
    "rivals",
    "no_hand",
    "unchecked",
)
"""Why an open `removed` entry was not removed for certain (4.6.8)."""
ADDED_CAUSES = ("freed", "size_changed", "relinked", "doubtful", "seen_once", "unchecked")
"""Why an open `added` entry was not added for certain (4.6.8)."""
LOOK_CODES = {
    "unsure": ("none", "late", "box", "stale", "tie", "low"),
    "removed": ("none", "late", "box", "stale", "tie", "low"),
    "added": ("none", "tie", "new"),
}
"""Why the looks could not settle an open entry, by kind (4.6.8)."""

Place = tuple[float, float]
Size = tuple[float, float]


@dataclass(slots=True, frozen=True)
class TrayPoint:
    """A point of a tray: grid frame, media time and server wall time (`None` before a clock state is known)."""

    g: int
    t: float
    wall: float | None = None

    @property
    def clock(self) -> str:
        """The media time as text (`2:24.0`)."""
        return text.clock(self.t)

    def to_dict(self) -> dict[str, Any]:
        return {"g": self.g, "t": self.t, "wall": self.wall}


@dataclass(slots=True, frozen=True)
class TrayQuestion:
    """A doubt held open for a look, or for the stone's next sight (a presence question, 4.6.1), to settle (4.6.4): its
    cause code (`pushed`, `unseen`, ...), why, `since` (the first frame `g` whose looks count for it) and where it
    opened."""

    cause: str
    why: str
    since: int
    opened: TrayPoint

    def to_dict(self) -> dict[str, Any]:
        return {"cause": self.cause, "why": self.why, "since": self.since, "opened": self.opened.to_dict()}


@dataclass(slots=True, frozen=True)
class TrayCandidate:
    """One reading the evidence leaves for an open stone (4.6.8): `world` (a world stone, with the tray stone linked to
    it, if any), `stone` (a tray stone), `gone` (with why: `carried_away`, `emptied`, `dropped` or `hand`), `unseen`
    or `new`. `score` is a look score in permille, when the last look run had one."""

    kind: str
    world_id: int | None = None
    stone: str | None = None
    score: int | None = None
    why: str | None = None

    def describe(self) -> str:
        """`world 95 0.248`, `stone 20261122T023847.0Z-1-87`, `gone (hand)`, `unseen` or `new`."""
        if self.kind in ("world", "stone"):
            head = f"world {self.world_id}" if self.kind == "world" else f"stone {self.stone}"
            return head if self.score is None else f"{head} {text.milli(self.score)}"
        if self.kind == "gone":
            return "gone" if self.why is None else f"gone ({self.why})"
        return self.kind

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "world_id": self.world_id, "stone": self.stone, "score": self.score, "why": self.why}


@dataclass(slots=True, frozen=True)
class TrayOpen:
    """A stone the rules leave open when its tray leaves (4.6.8): `unsure` (on the tray, not sure), `removed` (removed
    unclear) or `added` (added unclear), with the cause, why the looks could not settle it (`look`, with its `score`
    in permille), and every reading the evidence leaves (`candidates`, ranked)."""

    stone: str
    world_id: int | None
    kind: str
    cause: str
    look: str
    score: int | None
    hand_near: bool | None
    seen_at_arrival: bool | None
    candidates: tuple[TrayCandidate, ...]

    def describe(self) -> str:
        """`stone 20261122T023847.0Z-1-91: unsure neighbour_pushed, look low 0.248: world 91 or world 95 0.248 or
        ... or gone`."""
        s = f"stone {self.stone}: {self.kind} {self.cause}, look {self.look}"
        if self.score is not None:
            s += f" {text.milli(self.score)}"
        if self.hand_near:
            s += ", hand near"
        if self.seen_at_arrival:
            s += ", seen at arrival"
        return s + ": " + " or ".join(c.describe() for c in self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stone": self.stone,
            "world_id": self.world_id,
            "kind": self.kind,
            "cause": self.cause,
            "look": self.look,
            "score": self.score,
            "hand_near": self.hand_near,
            "seen_at_arrival": self.seen_at_arrival,
            "candidates": [c.to_dict() for c in self.candidates],
        }


SPECKLE_CLEAN = 300
"""A clean speckle find: its score at least this (permille) and at least twice its second (4.6.12)."""
SPECKLE_NEAR = 2
"""Frames between an answer's reference and its tray's arrival within which the arrival's links stand (4.6.12)."""

SPECKLE_CODES = ("speckle_same", "speckle_moved", "speckle_found")
"""The codes of the `identified` facts of the leaving's speckle answer (4.6.13)."""

CONFIDENCE: dict[tuple[str, bool, str], float] = {
    ("removed", True, "not_found"): 0.98,
    ("removed", True, "undecided"): 0.50,
    ("removed", True, "found"): 0.25,
    ("removed", False, "not_found"): 0.71,
    ("removed", False, "undecided"): 0.58,
    ("removed", False, "found"): 0.09,
    ("added", True, "not_found"): 0.96,
    ("added", True, "undecided"): 0.52,
    ("added", True, "found"): 0.02,
    ("added", False, "not_found"): 0.96,
    ("added", False, "undecided"): 0.52,
    ("added", False, "found"): 0.02,
}
"""The confidence of a removal or an addition (4.6.13): (kind, certain, the stone's `evidence`) -> the probability that
the change happened, measured by the maintainers on the four recordings against their look tracker and the camera."""

MOVED_CONFIDENCE = 0.87
"""The confidence of a move (4.6.13): 19 of 21 paired moves right."""


@dataclass(slots=True, frozen=True)
class StoneSpeckle:
    """A tray stone in one of the unit's speckle answers (4.6.12): `side` `arrival` (the stone as it was at the
    answer's reference, checked at its later moment) or `later` (the stone at the later moment, checked at the
    reference); `score` and `second` as the unit sent them (`None`: not checked); `clean` when the score is at least
    0.300 and twice the second; `found_world` the world stone at the other moment its best place lies on, `found` the
    tray stone that stood for it there, and `pair` the tray stone the answer pairs it with (`None` when there is
    none)."""

    stone: str
    side: str
    score: float | None
    second: float | None
    clean: bool
    found: str | None
    found_world: int | None
    pair: str | None

    @property
    def same(self) -> bool:
        """The answer pairs the stone with itself: the same stone at both moments."""
        return self.pair == self.stone

    def to_dict(self) -> dict[str, Any]:
        return {
            "stone": self.stone,
            "side": self.side,
            "score": self.score,
            "second": self.second,
            "clean": self.clean,
            "found": self.found,
            "found_world": self.found_world,
            "pair": self.pair,
        }


@dataclass(slots=True, frozen=True)
class TraySpeckle:
    """One of the unit's speckle answers, read as a tray's (4.6.12): `tray` the SDK's tray number, `g`, `t` and `wall`
    the frame it came in, `unit_tray` the unit's own count, `ref` and `at` the two moments compared (`g`, `t`), the
    unit's `device`, `gpu_ms` and `wall_ms`, and `stones` the tray stones in it, in tray order."""

    tray: int
    session: int
    video: str
    g: int
    t: float
    wall: float | None
    unit_tray: int
    ref: TrayPoint
    at: TrayPoint
    device: str
    gpu_ms: float
    wall_ms: float
    stones: tuple[StoneSpeckle, ...] = ()

    def stone(self, stone_id: str) -> StoneSpeckle | None:
        """The answer for that tray stone."""
        return next((s for s in self.stones if s.stone == stone_id), None)

    def describe(self) -> str:
        """`tray 2: speckle of 0:22.5 and 2:24.2: 42 stones, 33 clean, 26 paired, 24 the same`."""
        st = self.stones
        return (
            f"tray {self.tray}: speckle of {text.clock(self.ref.t)} and {text.clock(self.at.t)}: {len(st)} stones, "
            f"{sum(1 for e in st if e.clean)} clean, {sum(1 for e in st if e.pair is not None)} paired, "
            f"{sum(1 for e in st if e.same)} the same"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tray": self.tray,
            "session": self.session,
            "video": self.video,
            "g": self.g,
            "t": self.t,
            "wall": self.wall,
            "unit_tray": self.unit_tray,
            "ref": {"g": self.ref.g, "t": self.ref.t},
            "at": {"g": self.at.g, "t": self.at.t},
            "device": self.device,
            "gpu_ms": self.gpu_ms,
            "wall_ms": self.wall_ms,
            "stones": [s.to_dict() for s in self.stones],
        }


def describe_speckle_txt(answers: Iterable[TraySpeckle]) -> str:
    """The golden `speckle.txt` form (4.6.12): for every answer `{g} {clock} ` and its describe(), then a line for each
    of its stones: two spaces, the stone, its side, score and second to 3 places (`-` when not checked), ` clean`,
    ` found {tray stone}` (or ` found world {id}`), ` pair {tray stone}`; `\\n` after every line."""
    out: list[str] = []
    for a in answers:
        out.append(f"{a.g} {text.clock(a.t)} {a.describe()}\n")
        for e in a.stones:
            sc = "-" if e.score is None or e.second is None else f"{text.fmt(e.score, 3)} {text.fmt(e.second, 3)}"
            line = f"  {e.stone} {e.side} {sc}"
            if e.clean:
                line += " clean"
            if e.found is not None:
                line += f" found {e.found}"
            elif e.found_world is not None:
                line += f" found world {e.found_world}"
            if e.pair is not None:
                line += f" pair {e.pair}"
            out.append(line + "\n")
    return "".join(out)


@dataclass(slots=True, frozen=True)
class TrayStone:
    """A stone of a tray, with the SDK's own id: `{when first seen}-{tray}-{world id}` (`20261122T023847.0Z-2-84`;
    without a clock `s{session}t{media seconds}-{tray}-{world id}`; a second stone first seen on the tray in the same
    tenth of a second under the same world id takes `-2` at the end, a third `-3`). An id is never given to two stones.

    `world_id` is the world stone it stands for now (`None` while lost or once removed); `status` is `on`, `lost` or
    `removed`; `sure` says its identity is sure now (false while a `question` is open, and for good once the stone is
    not sure). `added` marks a stone first recorded after arrival. A certain addition was set down. An unclear addition
    is an occupant whose arrival identity or placement could not be established. `certain` describes the removal for a
    removed stone, the addition for an added stone still on the tray, and is `None` otherwise. Count confirmed additions
    from their facts. `place` (x, y) and `size` (long, short side) are as last known (for a stone counted from the
    leaving point's world, boxed or kept there, as that world has it), in scene units; `first_place` is its place when
    the tray
    stone was made (at the arrival for the stones the tray arrived with), `left_place` its place at the tray's leaving
    point when it was seen there (`None` otherwise, and while the tray is on), and `touched` the frame `g` where
    something last touched it on this tray (a hand, a push on it or next to it, a link to another world stone; `None`
    when nothing did): the report reads these. With looks: `looks` are the frame `g`s of its anchor and latest look,
    `by_look` that a look decision linked it, `look_lost` that a look decision ended its link, and `open` its open entry
    once the tray has left. `speckle` is the stone in the newest speckle answer of the unit read while its tray was on
    (4.6.12), `None` without one. `by_speckle` says the tray's leaving answer paired it (4.6.13: confirmed it on its
    world stone or linked it to another), and `evidence`, set as the tray leaves, what that answer found of it at the
    other moment (`found`, `not_found` or `undecided`, 4.6.13; `None` while the tray is on).
    """

    id: str
    world_id: int | None
    status: str
    sure: bool
    added: bool
    certain: bool | None
    born: TrayPoint
    place: tuple[float, float]
    size: tuple[float, float]
    question: TrayQuestion | None = None
    looks: tuple[int, ...] = ()
    by_look: bool = False
    look_lost: bool = False
    open: TrayOpen | None = None
    first_place: tuple[float, float] = (0.0, 0.0)
    left_place: tuple[float, float] | None = None
    touched: int | None = None
    speckle: StoneSpeckle | None = None
    by_speckle: bool = False
    evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "world_id": self.world_id,
            "status": self.status,
            "sure": self.sure,
            "added": self.added,
            "certain": self.certain,
            "born": self.born.to_dict(),
            "place": list(self.place),
            "size": list(self.size),
            "question": None if self.question is None else self.question.to_dict(),
            "looks": list(self.looks),
            "by_look": self.by_look,
            "look_lost": self.look_lost,
            "open": None if self.open is None else self.open.to_dict(),
            "first_place": list(self.first_place),
            "left_place": None if self.left_place is None else list(self.left_place),
            "touched": self.touched,
            "speckle": None if self.speckle is None else self.speckle.to_dict(),
            "by_speckle": self.by_speckle,
            "evidence": self.evidence,
        }


@dataclass(slots=True, frozen=True)
class TrayFact:
    """Something the interpreter says about a tray: `arrived`, `unsure`, `identified`, `removed`, `added` or
    `leaving`.

    `certain` (removed and added) says the fact is certain; an unclear one is reported as such, never as a fact.
    `code` is the code of its why (`pushed`, `look_found`, `carried_away`, ...). `arrived` has `stones`; `leaving` has
    `stones` (on the tray), `sure`, `added` and `removed` (its certain facts), `unclear` (its unclear facts), `open`
    (the open account) and `t_id` (the tray's bar, permille, when it took a look). An `unsure` fact that ends a
    question has `opened`; an `identified` fact (a look linked the stone to `world_id`) has `from_world`, `score`,
    `margin` and `bar` (permille). The leaving facts carry the leaving point.
    """

    kind: str
    tray: int
    session: int
    video: str
    g: int
    t: float
    wall: float | None
    stone: str | None = None
    world_id: int | None = None
    certain: bool | None = None
    why: str | None = None
    code: str | None = None
    stones: int | None = None
    sure: int | None = None
    added: int | None = None
    removed: int | None = None
    unclear: int | None = None
    opened: TrayPoint | None = None
    from_world: int | None = None
    score: int | None = None
    margin: int | None = None
    bar: int | None = None
    t_id: int | None = None
    open: tuple[TrayOpen, ...] | None = None

    def describe(self) -> str:
        """The fact as one line: `0:45.5 tray 1: stone 20261122T023847.0Z-1-117 was removed`."""
        return text.describe_tray_fact(self)

    @property
    def clock(self) -> str:
        """The media time as text."""
        return text.clock(self.t)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tray": self.tray,
            "session": self.session,
            "video": self.video,
            "stone": self.stone,
            "world_id": self.world_id,
            "certain": self.certain,
            "why": self.why,
            "code": self.code,
            "g": self.g,
            "t": self.t,
            "wall": self.wall,
            "stones": self.stones,
            "sure": self.sure,
            "added": self.added,
            "removed": self.removed,
            "unclear": self.unclear,
            "opened": None if self.opened is None else self.opened.to_dict(),
            "from_world": self.from_world,
            "score": self.score,
            "margin": self.margin,
            "bar": self.bar,
            "t_id": self.t_id,
            "open": None if self.open is None else [o.to_dict() for o in self.open],
        }


@dataclass(slots=True, frozen=True)
class Tray:
    """A tray: `arrived` (it has arrived and settled; `None` until then) and `leaving` (just before it moved out; `None`
    while it is on, and for a tray that never arrived), its stones in tray order and every fact about it. `open` is the
    open account of its leaving (`None` until it has left) and `t_id` its bar by look now (permille; `None` while it
    has taken no look)."""

    session: int
    video: str
    n: int
    arrived: TrayPoint | None
    leaving: TrayPoint | None
    stones: tuple[TrayStone, ...] = ()
    facts: tuple[TrayFact, ...] = ()
    open: tuple[TrayOpen, ...] | None = None
    t_id: int | None = None

    def window(self) -> tuple[float, float] | None:
        """`(arrived.t, leaving.t)`: the media span to replay or to show for this tray (`None` until it has left)."""
        if self.arrived is None or self.leaving is None:
            return None
        return (self.arrived.t, self.leaving.t)

    def stone(self, stone_id: str) -> TrayStone | None:
        """The tray stone with that id."""
        return next((s for s in self.stones if s.id == stone_id), None)

    def check(self) -> list[str]:
        """The self-check of a tray that has left (4.6.10): the codes (`C1` ... `C9`) it finds broken in the tray's
        facts and final tray stones, in order; an empty list means the account is consistent. A tray that never
        arrived passes; one that has not left yet breaks C1 (it has no `leaving` fact)."""
        return check_tray(self)

    def report(self) -> TrayReport | None:
        """The tray's report (4.7): which stones were removed, added and moved, and whether that is its whole account.
        `None` until the tray has left, and for a tray that never arrived."""
        return tray_report(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "video": self.video,
            "n": self.n,
            "arrived": None if self.arrived is None else self.arrived.to_dict(),
            "leaving": None if self.leaving is None else self.leaving.to_dict(),
            "stones": [s.to_dict() for s in self.stones],
            "facts": [f.to_dict() for f in self.facts],
            "open": None if self.open is None else [o.to_dict() for o in self.open],
            "t_id": self.t_id,
        }


@dataclass(slots=True)
class TrayCounts:
    """The `trays` counts of the summary: trays opened and arrived, tray stones at the arrivals, certain and unclear
    facts, and tray stones that stopped being sure."""

    total: int = 0
    arrived: int = 0
    stones: int = 0
    added: int = 0
    added_unclear: int = 0
    removed: int = 0
    removed_unclear: int = 0
    unsure: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "arrived": self.arrived,
            "stones": self.stones,
            "added": self.added,
            "added_unclear": self.added_unclear,
            "removed": self.removed,
            "removed_unclear": self.removed_unclear,
            "unsure": self.unsure,
        }


MOVED = 0.75
"""A stone the tray arrived with, that the SDK is sure of, saw at the leaving point and that something touched, moved
when, once the tray's own shift is taken out, its place there is more than this many times its short side from where
it was first seen (4.7)."""


@dataclass(slots=True, frozen=True)
class TrayChange:
    """One stone of a tray report (4.7): `kind` is `removed`, `added` or `moved`; `stone` the tray stone's id.

    For removed and added, `at`, `certain` and `code` are its fact's (`carried_away`, `missing`, `not_found`;
    `set_down`, `not_recognised`); for moved, `at` and `code` are `None` and `certain` is true: only a stone the SDK is
    sure of, seen at the leaving point and touched on the tray is reported moved (one that is not sure may be another
    stone at that place). `place` is the stone's final place on the tray, in the world's scene units (x right and y up
    as the camera sees the tray), for matching a report with another system that looks at the trays: for a removal,
    where the stone was last seen before it was taken; for an addition or a move, its place as the tray left (its
    `left_place` when it was seen at the leaving point, else where it was last seen or, for a stone counted because the
    leaving point's world kept it, its place in that world). `size` is its long and short side, as last known (for a
    stone counted from the leaving point's world, boxed or kept there, as that world has it); `distance` is how far a
    moved stone moved, once the tray's own shift is taken out (`None` for the others); `confidence` the probability
    that the change happened (4.6.13: by its kind, certainty and the stone's `evidence`), for a threshold of your own.
    An `added` change with `certain=False` and `code="not_recognised"` records an occupant of unknown origin."""

    kind: str
    stone: str
    at: TrayPoint | None
    certain: bool
    code: str | None
    place: tuple[float, float]
    size: tuple[float, float]
    distance: float | None = None
    confidence: float = 0.0

    def describe(self) -> str:
        """One line: for removed and added, the clock of its fact, the kind, the stone and its final place (`from` it
        for a removal, `at` it for an addition), then its confidence, ` (0.98)`, or ` (unclear, 0.71)` when it is not
        certain, as in `0:45.5 removed 20261122T023847.0Z-1-117 from (0.68, -1.06) (0.98)`; for moved, `moved`, the
        stone, the distance, `to` its place and its confidence."""

        def coord(v: float) -> str:
            s = text.fmt(v, 2)
            return "0.00" if s == "-0.00" else s

        where = f"({coord(self.place[0])}, {coord(self.place[1])})"
        conf = text.fmt(self.confidence, 2)
        if self.kind == "moved":
            return f"moved {self.stone} {text.fmt(self.distance or 0.0, 2)} to {where} ({conf})"
        at = "" if self.at is None else f"{self.at.clock} "
        prep = "from" if self.kind == "removed" else "at"
        return f"{at}{self.kind} {self.stone} {prep} {where}" + (
            f" ({conf})" if self.certain else f" (unclear, {conf})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "stone": self.stone,
            "at": None if self.at is None else self.at.to_dict(),
            "certain": self.certain,
            "code": self.code,
            "place": list(self.place),
            "size": list(self.size),
            "distance": self.distance,
            "confidence": self.confidence,
        }


@dataclass(slots=True, frozen=True)
class TrayReport:
    """A tray's report, the SDK's simple mode (4.7): on tray `tray`, from `arrived` to `left`, which stones were
    `removed`, `added` and `moved`.

    `complete` says that is the tray's whole account: every change is certain and no stone is open. Otherwise `unclear`
    counts the changes that are not certain and `unsure` the stones on the tray as it left whose identity is not sure
    (those added unclear aside); the tray's facts and its open account (`Tray.open`) say why, stone by stone. `stones`
    is how many stones the tray arrived with. `arrived.wall` and `left.wall` are the unit's wall clock (UTC epoch
    seconds) when it is known."""

    session: int
    video: str
    tray: int
    arrived: TrayPoint
    left: TrayPoint
    stones: int
    removed: tuple[TrayChange, ...]
    added: tuple[TrayChange, ...]
    moved: tuple[TrayChange, ...]
    unclear: int
    unsure: int
    complete: bool

    def describe(self) -> str:
        """`tray 1 (2026-11-22T02:38:47.0Z), 0:22.5 to 2:24.0: 11 removed, 13 added, 8 moved, not complete:
        18 unclear, 1 not sure`, then a line for each change, indented two spaces, ending with its confidence."""
        wall = "" if self.arrived.wall is None else f" ({iso(self.arrived.wall)})"
        head = (
            f"tray {self.tray}{wall}, {self.arrived.clock} to {self.left.clock}: {len(self.removed)} removed, "
            f"{len(self.added)} added, {len(self.moved)} moved"
        )
        if not self.complete:
            parts = [f"{self.unclear} unclear"] * (self.unclear > 0) + [f"{self.unsure} not sure"] * (self.unsure > 0)
            head += ", not complete: " + ", ".join(parts)
        return "\n".join([head, *("  " + c.describe() for c in (*self.removed, *self.added, *self.moved))])

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "video": self.video,
            "tray": self.tray,
            "arrived": self.arrived.to_dict(),
            "left": self.left.to_dict(),
            "stones": self.stones,
            "removed": [c.to_dict() for c in self.removed],
            "added": [c.to_dict() for c in self.added],
            "moved": [c.to_dict() for c in self.moved],
            "unclear": self.unclear,
            "unsure": self.unsure,
            "complete": self.complete,
        }


def _median(xs: Sequence[float]) -> float:
    v = sorted(xs)
    k = len(v) // 2
    return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2


def tray_report(tray: Tray) -> TrayReport | None:
    """The report of a tray that has left (4.7), from its facts and its final tray stones; `None` for any other."""
    facts = tray.facts
    if tray.arrived is None or tray.leaving is None or not facts or facts[-1].kind != "leaving":
        return None
    arrival, leaving = facts[0], facts[-1]
    by_id = {s.id: s for s in tray.stones}

    def change(kind: str, s: TrayStone, x: TrayFact) -> TrayChange:
        point = TrayPoint(x.g, x.t, x.wall)
        # its final place: where it was last seen before it was taken, or as the tray left
        place = s.place if kind == "removed" or s.left_place is None else s.left_place
        certain = bool(x.certain)
        conf = CONFIDENCE[(kind, certain, s.evidence or "undecided")]
        return TrayChange(kind, s.id, point, certain, x.code, place, s.size, None, conf)

    removed = tuple(change("removed", by_id[x.stone], x) for x in facts if x.kind == "removed" and x.stone in by_id)
    added = tuple(change("added", by_id[x.stone], x) for x in facts if x.kind == "added" and x.stone in by_id)
    # moved: only the stones it arrived with that are sure (a stone not sure counts as not sure, not as moved), at
    # their place at the leaving point (where they were seen), and only those something touched
    kept = [(s, s.left_place) for s in tray.stones if not s.added and s.status == "on" and s.sure and s.left_place]
    moved: list[TrayChange] = []
    if kept:
        # the tray's own shift: the median over the half of them that moved least (ties in tray order)
        order = sorted(range(len(kept)), key=lambda i: (dist(kept[i][0].first_place, kept[i][1]), i))
        least = [kept[i] for i in order[: (len(kept) + 1) // 2]]
        mx = _median([p[0] - s.first_place[0] for s, p in least])
        my = _median([p[1] - s.first_place[1] for s, p in least])
        for s, p in kept:
            d = dist((s.first_place[0] + mx, s.first_place[1] + my), p)
            if s.touched is not None and d > MOVED * s.size[1]:
                moved.append(TrayChange("moved", s.id, None, True, None, p, s.size, text.round3(d), MOVED_CONFIDENCE))
    unsure = sum(1 for s in tray.stones if s.status == "on" and not s.sure and not (s.added and s.certain is False))
    return TrayReport(
        session=tray.session,
        video=tray.video,
        tray=tray.n,
        arrived=TrayPoint(arrival.g, arrival.t, arrival.wall),
        left=TrayPoint(leaving.g, leaving.t, leaving.wall),
        stones=arrival.stones or 0,
        removed=removed,
        added=added,
        moved=tuple(moved),
        unclear=leaving.unclear or 0,
        unsure=unsure,
        complete=(leaving.unclear or 0) == 0 and leaving.sure == leaving.stones,
    )


def check_tray(tray: Tray) -> list[str]:
    """`Tray.check()`: the broken codes of the self-check (4.6.10), sorted. A tray that never arrived has nothing to
    check; one that has not left yet breaks C1."""
    bad: set[str] = set()
    facts = tray.facts
    if tray.arrived is None and not facts and not tray.stones:
        return []
    if not facts or facts[0].kind != "arrived" or facts[-1].kind != "leaving":
        bad.add("C1")
    if any(x.kind == "leaving" for x in facts[:-1]):
        bad.add("C1")
    ids = [s.id for s in tray.stones]
    by_id = {s.id: s for s in tray.stones}
    if len(set(ids)) != len(ids) or any(x.stone is not None and x.stone not in by_id for x in facts):
        bad.add("C2")
    life: dict[str, list[TrayFact]] = {}
    for x in facts:
        if x.stone is not None:
            life.setdefault(x.stone, []).append(x)
    for xs in life.values():
        kinds = [x.kind for x in xs]
        if "added" in kinds[1:] or kinds.count("unsure") > 1 or kinds.count("removed") > 1:
            bad.add("C3")
        for a, x in enumerate(xs):
            after = xs[a + 1 :]
            speckle = x.kind == "identified" and x.code in SPECKLE_CODES
            if x.kind == "unsure" and any(
                y.kind != "removed" and not (y.kind == "identified" and y.code in SPECKLE_CODES) for y in after
            ):
                bad.add("C3")
            if x.kind == "removed" and after:
                bad.add("C3")
            if x.kind == "identified" and ("removed" in kinds[:a] or (not speckle and "unsure" in kinds[:a])):
                bad.add("C3")
            if speckle and any(y.kind == "identified" and y.code in SPECKLE_CODES for y in xs[:a]):
                bad.add("C3")
    for s in tray.stones:
        xs = life.get(s.id, [])
        rem = [x for x in xs if x.kind == "removed"]
        if (s.status == "removed") != bool(rem) or (rem and rem[0].certain != s.certain):
            bad.add("C4")
        last = max((a for a, x in enumerate(xs) if x.kind == "identified" and x.code in SPECKLE_CODES), default=-1)
        if s.status == "on" and s.sure != (
            not any(x.kind == "unsure" for x in xs[last + 1 :]) and not (s.added and not s.certain)
        ):
            bad.add("C4")
        if s.question is not None:
            bad.add("C4")
    on = [s for s in tray.stones if s.status == "on"]
    if facts and facts[0].kind == "arrived" and facts[0].stones != sum(1 for s in tray.stones if not s.added):
        bad.add("C5")
    leave = facts[-1] if facts else None
    if leave is not None and leave.kind == "leaving":
        want = (
            len(on),
            sum(1 for s in on if s.sure),
            sum(1 for x in facts if x.kind == "added" and x.certain is True),
            sum(1 for x in facts if x.kind == "removed" and x.certain is True),
            sum(1 for x in facts if x.kind in ("removed", "added") and not x.certain),
        )
        if want != (leave.stones, leave.sure, leave.added, leave.removed, leave.unclear):
            bad.add("C5")
        entries = leave.open or ()
        # one entry per open stone, in tray order (each stone settled or not by itself, as 4.6.8 defines them)
        open_ids = [
            s.id for s in tray.stones if not ((s.status == "on" and s.sure) or (s.status == "removed" and s.certain))
        ]
        if [o.stone for o in entries] != open_ids:
            bad.add("C5")
        sure_world = {s.world_id for s in on if s.sure}
        for o in entries:
            c = o.candidates
            keys = [(x.kind, x.world_id, x.stone) for x in c]
            codes = UNSURE_CODES if o.kind == "unsure" else REMOVED_CAUSES if o.kind == "removed" else ADDED_CAUSES
            if o.cause not in codes or o.look not in LOOK_CODES.get(o.kind, ()) or not c or len(set(keys)) != len(keys):
                bad.add("C7")
            if not c:  # the checks below read the first and last candidate; C8 has nothing to check
                continue
            if o.kind == "unsure" and (c[0].kind != "world" or c[0].world_id != o.world_id):
                bad.add("C7")
            if o.kind != "added" and not any(x.kind == "gone" for x in c):
                bad.add("C7")
            if (o.kind == "removed" and c[-1].kind != "unseen") or (o.kind == "added" and c[-1].kind != "new"):
                bad.add("C7")
            for cand in c:
                z = by_id.get(cand.stone) if cand.kind == "stone" and cand.stone is not None else None
                if (z is not None and ((z.status == "on" and z.sure) or (z.status == "removed" and z.certain))) or (
                    cand.kind == "world" and cand.world_id in sure_world
                ):
                    bad.add("C8")
    if len({s.world_id for s in on}) != len(on) or any(s.world_id is None for s in on):
        bad.add("C6")
    for x in facts:
        if x.kind != "identified":
            continue
        if x.code in SPECKLE_CODES:
            if not (
                x.score is not None and x.bar is not None and x.score >= x.bar == SPECKLE_CLEAN and x.certain is None
            ):
                bad.add("C9")
        elif not (
            x.score is not None
            and x.bar is not None
            and x.score >= x.bar >= T_ID_MIN
            and x.certain is None
            and (x.margin is None or x.margin >= M_ID)
        ):
            bad.add("C9")
    return sorted(bad)


# ---- geometry (all doubles, the same in every SDK) -------------------------------------------------------------------
def dist(a: Place, b: Place) -> float:
    dx, dy = a[0] - b[0], a[1] - b[1]
    return math.sqrt(dx * dx + dy * dy)


def size_of(ws: StoneView) -> Size:
    d = ws.solid.dims
    return (max(d[0], d[1]), min(d[0], d[1]))


def place_of(ws: StoneView) -> Place:
    p = ws.solid.pos
    return (p[0], p[1])


def usable(ws: StoneView) -> bool:
    """A world stone the tray rules can use: a solid with at least 2 numbers in pos and dims and 4 in q."""
    sol = ws.solid
    return len(sol.pos) >= 2 and len(sol.dims) >= 2 and len(sol.q) >= 4


def compatible(a: Size, b: Size) -> bool:
    """Each side differs by at most SIZE_TOL of the larger."""
    return abs(a[0] - b[0]) <= SIZE_TOL * max(a[0], b[0]) and abs(a[1] - b[1]) <= SIZE_TOL * max(a[1], b[1])


def turn(ws: StoneView) -> tuple[float, float]:
    """cos and sin of the stone's turn about the vertical, from its quaternion [x, y, z, w], without trigonometry."""
    q = ws.solid.q
    z, w = q[2], q[3]
    n = w * w + z * z
    if n == 0:
        return 1.0, 0.0
    return (w * w - z * z) / n, (2 * z * w) / n


def covers(ws: StoneView, p: Place, m: float) -> bool:
    """The stone's box, grown by `m`, covers point `p`."""
    c, s = turn(ws)
    pos, d = ws.solid.pos, ws.solid.dims
    dx, dy = p[0] - pos[0], p[1] - pos[1]
    return abs(c * dx + s * dy) <= d[0] / 2 + m and abs(-s * dx + c * dy) <= d[1] / 2 + m


def inside_board(b: Rect | None, p: Place) -> bool:
    if b is None:
        return True
    c, s = math.cos(b.theta), math.sin(b.theta)
    dx, dy = p[0] - b.cx, p[1] - b.cy
    return abs(c * dx + s * dy) <= b.hw * 1.05 + 0.02 and abs(-s * dx + c * dy) <= b.hh * 1.05 + 0.02


def overlap_share(ws: StoneView, world: Sequence[StoneView]) -> float:
    """The largest share of this world stone's box (5 x 5 points) that one other world stone's box covers."""
    c, s = turn(ws)
    d = ws.solid.dims
    x0, y0 = ws.solid.pos[0], ws.solid.pos[1]
    pts = []
    for i in range(5):
        for j in range(5):
            u = (i + 0.5) / 5 * d[0] - d[0] / 2
            v = (j + 0.5) / 5 * d[1] - d[1] / 2
            pts.append((x0 + c * u - s * v, y0 + s * u + c * v))
    reach = max(d[0], d[1])
    best = 0
    for o in world:
        if o.id == ws.id:
            continue
        od = o.solid.dims
        if dist(place_of(o), (x0, y0)) > (reach + max(od[0], od[1])) / 2:
            continue
        best = max(best, sum(1 for p in pts if covers(o, p, 0.0)))
    return best / 25


def _civil_from_days(z: int) -> tuple[int, int, int]:
    z += 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return yoe + era * 400 + (1 if m <= 2 else 0), m, d


def stamp(wall: float | None, t: float, session: int) -> str:
    """When a stone was first seen: UTC wall time to the tenth (`20261122T023847.0Z`), else the session and the media
    seconds (`s1795314878t22.5`: world ids start again in a new session, and a seek can show the same time again)."""
    if wall is None:
        return f"s{session}t" + text.fmt(t, 1)
    n = math.floor(wall * 10 + 0.5)
    secs, tenth = n // 10, n % 10
    y, m, d = _civil_from_days(secs // 86400)
    r = secs % 86400
    return f"{y:04d}{m:02d}{d:02d}T{r // 3600:02d}{r % 3600 // 60:02d}{r % 60:02d}.{tenth}Z"


def iso(wall: float) -> str:
    """A wall time (UTC epoch seconds) to the tenth of a second, rounded as `stamp()` rounds it:
    `2026-11-22T02:38:47.0Z`."""
    n = math.floor(wall * 10 + 0.5)
    secs, tenth = n // 10, n % 10
    y, m, d = _civil_from_days(secs // 86400)
    r = secs % 86400
    return f"{y:04d}-{m:02d}-{d:02d}T{r // 3600:02d}:{r % 3600 // 60:02d}:{r % 60:02d}.{tenth}Z"


# ---- the rules -------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class _BoundarySight:
    """A positive sight beside a tray's leaving point. Its origin is still unknown."""

    anchor_g: int
    t: float
    stone: StoneView
    latest: StoneView
    neighbours: set[int]


@dataclass(slots=True)
class _Unid:
    """A world stone no tray stone stands for (yet)."""

    born: TrayPoint
    place: Place
    size: Size
    seen: int = 0
    last: Place | None = None
    freed: bool = False
    """A look decision unlinked it (4.6.5): never added for certain."""
    resized: bool = False
    """Made by a size change of a linked world stone (4.6.1 step 4): never added for certain."""
    detections: int = 0
    """Prior positive frames in this ID's uninterrupted, unidentified run, capped at two."""
    boundary: _BoundarySight | None = None


@dataclass(slots=True)
class _Blink:
    """A silent omission: the first missing frame and the last real observation."""

    first: TrayPoint
    previous: StoneView
    unid: _Unid | None
    neighbours: set[int]
    other_sources: tuple[tuple[Place, Size], ...]

    def original_place_is_nearest(self, stone: StoneView) -> bool:
        place, size = place_of(stone), size_of(stone)
        own_distance = dist(place_of(self.previous), place)
        return own_distance <= RENAME_D and all(
            own_distance < dist(other, place)
            for other, other_size in self.other_sources
            if compatible(size, other_size)
        )


@dataclass(slots=True, eq=False)
class _Look:
    """A stored look of a world stone (4.6.3): the frame it came on, the stone's place and size there."""

    wid: int
    cells: Cells
    g: int
    t: float
    place: Place
    size: Size
    taken: bool = False
    detected: bool = False
    """The world stone was detected on the look's frame: only such a look is an anchor."""


@dataclass(slots=True, eq=False)
class _Again:
    """An unseen run's first sight (4.6.1, presence questions): when and where its stone was first detected after the
    run's first calm moment, whether a busy frame or a push frame came from its last sight up to then, and whether
    another world stone of its size stood within NEAR of the place where it was last seen."""

    t: float
    place: Place
    busy: bool
    push: bool
    crowd: bool


@dataclass(slots=True, eq=False)
class _Watch:
    """A stone's unseen run (4.6.1, presence questions): its world id, the place and the time it was last seen
    (`s1`), the time of the run's first calm moment (`c1`), at which its world stone was not detected, and its first
    sight after that calm moment."""

    wid: int
    place: Place
    seen_t: float
    c1_t: float
    again: _Again | None = None
    spoilt: bool = False
    """A busy or push frame later than `c1` came while its world stone was not detected, or frames the rules could not
    read came (4.6.1)."""


@dataclass(slots=True, eq=False)
class _Question:
    cause: str
    why: str
    since: int
    opened: TrayPoint
    watch: _Watch | None = None
    """A presence question (4.6.1): the unseen run it was opened with, kept until the question is settled or ends;
    seeing the stone again settles it."""


Pair = tuple[int, int | None, int | None]
"""A pair of the look step: (world id or tray index, permille score or None, its pair count or None)."""


@dataclass(slots=True, eq=False)
class _Best:
    row: int
    wid: int
    score: int
    margin: int | None
    bar: int


@dataclass(slots=True, eq=False)
class _Run:
    """One run of the look step (4.6.5): rows (tray index, since), columns (world ids), each row's pairs, each pair's
    (bar, best rival), and the decided pair with the highest score."""

    rows: list[tuple[int, int]]
    cols: list[int]
    pairs: dict[int, list[Pair]]
    info: dict[tuple[int, int], tuple[int, int | None]]
    best: _Best | None


@dataclass(slots=True, eq=False)
class _Stone:
    id: str
    index: int
    world: int | None
    world_t: float
    size: Size
    frozen: bool
    place: Place
    seen_t: float
    born: TrayPoint
    first_place: Place = (0.0, 0.0)
    status: str = "on"
    sure: bool = True
    """Not given up: a stone in question keeps it (its view's `sure` is false meanwhile)."""
    added: bool = False
    certain: bool | None = None
    lost_t: float = 0.0
    lost_k: int = 0
    carried_away: bool = False
    dropped: bool = False
    emptied: bool = False
    handled: bool = False
    pushed: bool = False
    ghost: int = 0
    at_k: int = -1
    """The snapshot the stone was last on the tray at, and its place there (`at`)."""
    at: Place = (0.0, 0.0)
    # by look (4.6.3-4.6.8)
    chain: list[_Look] = field(default_factory=list)
    question: _Question | None = None
    doubted: bool = False
    by_look: bool = False
    look_lost: bool = False
    lost_by_look: bool = False
    lifted: bool = False
    near_push: bool = False
    disturbed: int | None = None
    lost_g: int = 0
    late: bool = False
    box: bool = False
    unsure_code: str | None = None
    unsure_from_t: float = 0.0
    unid_rec: _Unid | None = None
    left_now: bool = False
    added_now: bool = False
    open: TrayOpen | None = None
    freed_push: bool = False
    """A world stone a look unlinked was pushed next to it since the last snapshot (doubted as a neighbour)."""
    calm_place: Place | None = None
    """Its place at the latest calm frame, if it was on the tray and seen in it (4.7)."""
    left_place: Place | None = None
    """Its place at the leaving point, if it was on the tray and seen there (4.7)."""
    touched: int | None = None
    """The frame where something last touched it: a disturbance, or a link to another world stone (4.7)."""
    run1: _Watch | None = None
    """Its current unseen run (4.6.1, presence questions); it ends when the stone is seen at a calm moment or lost."""
    once: bool = False
    """Its current unseen run is on probation: it began at a calm moment that settled the stone's presence question
    without showing it, so the calm moment that shows it again must settle that run too (4.6.1)."""
    handled_t: float | None = None
    """The latest time it was handled (a `lift`, `carry`, `hinge` or `place` on its link) or held (4.6.1)."""
    hand_t: float | None = None
    """The latest time a hand touched its link: a hand event, a `push` with a `side`, or a `delete` with why `hand` or
    `carried-away` on its world id (4.6.1, certain removals)."""
    lost_calm_t: float = 0.0
    """The time of the last calm moment before its latest loss (4.6.1, certain removals)."""
    speckle: StoneSpeckle | None = None
    """The stone in the newest speckle answer read while the tray was on (4.6.12)."""
    first_world: int | None = None
    """The world id it was made with (its arrival world id for a stone the tray arrived with)."""
    by_speckle: bool = False
    """The leaving's speckle answer linked or confirmed it (4.6.13)."""
    evidence: str | None = None
    """What the leaving's answer found of it at the other moment, set as the tray leaves (4.6.13)."""

    def view(self) -> TrayStone:
        q = self.question
        return TrayStone(
            id=self.id,
            world_id=self.world,
            status=self.status,
            sure=self.sure and q is None,
            added=self.added,
            certain=self.certain,
            born=self.born,
            place=self.place,
            size=self.size,
            question=None if q is None else TrayQuestion(q.cause, q.why, q.since, q.opened),
            looks=tuple(e.g for e in self.chain),
            by_look=self.by_look,
            look_lost=self.look_lost,
            open=self.open,
            first_place=self.first_place,
            left_place=self.left_place,
            touched=self.touched,
            speckle=self.speckle,
            by_speckle=self.by_speckle,
            evidence=self.evidence,
        )


@dataclass(slots=True, eq=False)
class _Tray:
    """The open tray's working state (dropped when it leaves; what is kept is its `Tray`)."""

    n: int
    ids: set[int] = field(default_factory=set)
    arrived: TrayPoint | None = None
    leaving: TrayPoint | None = None
    board: Rect | None = None
    stones: list[_Stone] = field(default_factory=list)
    link: dict[int, _Stone] = field(default_factory=dict)
    unid: dict[int, _Unid] = field(default_factory=dict)
    blinks: dict[int, _Blink] = field(default_factory=dict)
    missing_frames: dict[int, int] = field(default_factory=dict)
    last_undisturbed: bool = False
    marks: list[tuple[float, Place]] = field(default_factory=list)
    k: int = -1
    settle_t: float = 0.0
    calm_ref: TrayPoint | None = None
    calm_world: WorldLayer | None = None
    point: dict[int, StoneView] = field(default_factory=dict)
    """The leaving point's world stones, by id (set as the tray leaves: its positive evidence at departure)."""
    worked: bool = False
    last: tuple[WorldLayer, TrayPoint] | None = None
    unclear: int = 0
    certain_added: int = 0
    """The tray's certain additions (its leaving counts them, also a stone removed unclear later)."""
    facts: list[TrayFact] = field(default_factory=list)
    # by look (4.6.3-4.6.8): the look store, the gauge, twins, a memo of scores, the leaving's last run
    store: dict[int, _Look] = field(default_factory=dict)
    alike: int | None = None
    alike_n: int = 0
    took: bool = False
    twins: set[str] = field(default_factory=set)
    memo: dict[tuple[int, int, int, int], tuple[int, int] | None] = field(default_factory=dict)
    ids_at_arrival: set[int] = field(default_factory=set)
    last_run: _Run | None = None
    open: list[TrayOpen] | None = None
    relinked: bool = False
    """A look changed a link, doubted a stone v1 trusts, or kept a twin from v1's certain removal: links and trust may
    differ from v1's from here on, so no certain removal or addition after it (4.6.7)."""
    last_mark_t: float | None = None
    """The time of the tray's latest hand mark."""
    busy_t: float | None = None
    """The time of the latest busy frame: a hand not free or flagged `untrusted`, a hand event, a held world stone, or
    a `delete` why `hand` or `carried-away` (4.6.1, presence questions); a frame after frames the rules could not read
    is one too."""
    push_t: float | None = None
    """The time of the latest push frame: a `push` event (any id, with a side or not), or a frame after frames the
    rules could not read (4.6.1)."""
    settle_g: int | None = None
    """The frame `g` of the last snapshot."""
    arrival_links: dict[int, str] = field(default_factory=dict)
    """The links as the tray arrived (world id -> tray stone id), for the speckle answers (4.6.12)."""
    answers: list[WorldSpeckle] = field(default_factory=list)
    """The unit's speckle answers read for the tray while it was on, oldest first (4.6.13)."""
    leaving_answer: WorldSpeckle | None = None
    """The leaving's answer (4.6.13), once the tray has left."""


@dataclass(slots=True)
class _VisitFrame:
    """A fully readable frame in the current stop and the linked stones actually observed there."""

    world: WorldLayer
    point: TrayPoint
    places: dict[str, tuple[int, Place]]


def _unique_id(tr: _Tray, sid: str) -> str:
    """The tray stone id `sid`, or, when the tray already has a stone with it (a stone and its world id seen again
    within the same tenth of a second), the first of `sid`-2, `sid`-3, ... that the tray does not have (4.6)."""
    taken = {s.id for s in tr.stones}
    uid, k = sid, 2
    while uid in taken:
        uid, k = f"{sid}-{k}", k + 1
    return uid


class TrayLedger:
    """The rules of section 4.6 for one session: `feed` it the worlds of the session's frames in order (not revisions),
    tell it where frames it could not read came (`skipped`), `close()` it when the session ends. Each tray that leaves
    goes to `on_close` as its `Tray`. Used by `InterpreterCore`; pure, no I/O."""

    def __init__(self, counts: TrayCounts, session: int, video: str, on_close: Callable[[Tray], None]) -> None:
        self._session = session
        self._video = video
        self._c = counts
        self._on_close = on_close
        self._n = 0
        self._tray: _Tray | None = None
        self._stop: list[tuple[WorldLayer, TrayPoint, bool]] = []
        self._on = False
        self._calm = 0
        self._calm_g = 0
        self._quiet = 0
        self._visit_start = 0
        self._ordinary_calm = 0
        self._quiet_epoch = 0
        self._clean_candidate: _VisitFrame | None = None
        self._visit_fallback: _VisitFrame | None = None
        self._partial_unseen: dict[int, tuple[int, int, int]] = {}
        self._hole = False  # frames the rules could not read came before the next world fed (4.6.1)
        self._last_closed: _Tray | None = None  # a speckle answer after the tray left is for it (4.6.12)

    # ---- outputs ----
    def links(self) -> dict[int, str]:
        """The open tray's links now: world id -> tray stone id (empty between trays)."""
        tr = self._tray
        return {} if tr is None else {wid: s.id for wid, s in tr.link.items()}

    def current(self) -> Tray | None:
        """The open tray (`None` between trays)."""
        return None if self._tray is None else self._view(self._tray)

    def _view(self, tr: _Tray) -> Tray:
        return Tray(
            session=self._session,
            video=self._video,
            n=tr.n,
            arrived=tr.arrived,
            leaving=tr.leaving,
            stones=tuple(s.view() for s in tr.stones),
            facts=tuple(tr.facts),
            open=None if tr.open is None else tuple(tr.open),
            t_id=self._t_id(tr) if tr.took else None,
        )

    # ---- inputs ----
    def skipped(self) -> None:
        """Frames the rules could not read came here (a frame without a world, a `g` gap, a `gap` or `reset` side
        event, a revision): the next world fed counts as a busy and a push frame (4.6.1)."""
        self._hole = True

    def feed(self, w: WorldLayer, ref: TrayPoint) -> list[TrayFact]:
        """The world of the session's next frame. Returns the tray facts it made."""
        out: list[TrayFact] = []
        hole, self._hole = self._hole, False
        keep: list[StoneView] = []
        seen: set[int] = set()
        for s in w.stones:  # the rules leave out a stone without a usable solid, and a world id's later stones
            if usable(s) and s.id not in seen:
                keep.append(s)
            seen.add(s.id)
        if len(keep) != len(w.stones):
            w = dataclasses.replace(w, stones=keep)
        if w.motion != "static":
            self._stop, self._on = [], False
            return out
        if self._on:
            self._frame(w, ref, out, hole)
            return out
        self._stop.append((w, ref, hole))
        if self._stop[-1][1].t - self._stop[0][1].t < STOP_MIN:
            return out
        self._begin(out)
        for ws, r, h in self._stop:
            self._frame(ws, r, out, h)
        self._stop = []
        return out

    def close(self) -> list[TrayFact]:
        """The session ended (or the replay or stream): the open tray leaves."""
        out: list[TrayFact] = []
        self._close(out)
        self._stop, self._on = [], False
        return out

    # ---- rules ----
    def _fact(self, kind: str, tr: _Tray, s: _Stone | None, ref: TrayPoint, out: list[TrayFact], **kw: Any) -> None:
        f = TrayFact(
            kind=kind,
            tray=tr.n,
            session=self._session,
            video=self._video,
            g=ref.g,
            t=ref.t,
            wall=ref.wall,
            stone=None if s is None else s.id,
            world_id=None if s is None else s.world,
            **kw,
        )
        tr.facts.append(f)
        out.append(f)

    def _begin(self, out: list[TrayFact]) -> None:
        unheld = {s.id for s in self._stop[0][0].stones if s.mode not in _HELD}
        ids = {s.id for s in self._stop[0][0].stones if s.mode not in _HELD and s.detected}
        tr = self._tray
        # a visit continues the open tray only on the camera's word (4.6.1): at least one of its first frame's detected
        # stones was seen on the tray, and nine tenths of those detected stones were, or nine tenths of all its stones
        # that no hand holds (a jolt can leave most of a board's stones only remembered and rename one of the few the
        # camera sees). A new board under the camera may carry many of the last board's world ids, and a board that
        # drove off may leave its stones remembered (not detected) where it stood.
        if not (
            tr is not None
            and len(ids & tr.ids) > 0
            and (10 * len(ids & tr.ids) >= 9 * len(ids) or 10 * len(unheld & tr.ids) >= 9 * len(unheld))
        ):
            self._close(out)
            self._n += 1
            self._c.total += 1
            self._tray = _Tray(n=self._n)
        elif tr.arrived is not None:
            # the tray goes on after moving frames the rules never read: the stop's first frame counts as a busy and a
            # push frame, so no unseen run from before them is settled by a sight after them (4.6.1)
            self._unread(tr, self._stop[0][1].t)
            self._finish_blinks(tr)
            tr.last = None
            tr.missing_frames.clear()
        self._on = True
        self._calm = self._quiet = 0
        self._visit_start = self._stop[0][1].g
        self._ordinary_calm = self._quiet_epoch = 0
        self._clean_candidate = self._visit_fallback = None
        self._partial_unseen.clear()

    def _frame(self, w: WorldLayer, ref: TrayPoint, out: list[TrayFact], hole: bool) -> None:
        tr = self._tray
        assert tr is not None
        t = ref.t
        by_id = {s.id: s for s in w.stones}
        tr.ids |= set(by_id)
        events = w.events
        self._store_looks(tr, w, by_id, ref)
        hand = any(e.type in _HAND_EVENTS or (e.type == "push" and e.side) for e in events)
        change = any(e.type in _CHANGES for e in events)
        self._quiet = 0 if any(e.type == "new" for e in events) else self._quiet + 1
        free = all(h is None or h.state == "free" for h in (w.hands.left, w.hands.right))
        untrusted = any(h is not None and bool(h.untrusted) for h in (w.hands.left, w.hands.right))
        held = any(ws.mode in _HELD for ws in w.stones)
        took = any(e.type == "delete" and e.why in ("hand", "carried-away") for e in events)
        busy = not free or hand or untrusted or held or took
        push = any(e.type == "push" for e in events)
        undisturbed = not busy and not push
        incomplete = self._inventory_gap(tr, ref, by_id, undisturbed and not events, hole)
        self._blinks(tr, w, ref, by_id, undisturbed, hole)
        continuous = tr.last is not None and tr.last[1].g + 1 == ref.g and not hole
        anchor = None
        if (
            continuous
            and tr.last is not None
            and tr.blinks
            and not events
            and undisturbed
            and tr.last_undisturbed
            and tr.calm_ref == tr.last[1]
        ):
            anchor = tr.last[0]
        anchor_stones = {} if anchor is None else {ws.id: ws for ws in anchor.stones}
        incomplete = incomplete or bool(tr.blinks)
        tr.last_undisturbed = undisturbed
        tr.last = (w, ref)
        quiet_frame = undisturbed and not change and not hole
        self._ordinary_calm = self._ordinary_calm + 1 if quiet_frame else 0
        if not quiet_frame:
            self._quiet_epoch += 1
            self._clean_candidate = None
        previous_visit = tr.calm_ref is not None and tr.calm_ref.g < self._visit_start
        if previous_visit and incomplete and self._ordinary_calm >= CALM and self._clean_candidate is not None:
            self._visit_fallback = self._clean_candidate
        if not incomplete:
            self._calm = self._calm + 1 if (free and not hand and not change) else 0
            if self._calm == 1:
                self._calm_g = ref.g
        if busy:
            # a busy frame (4.6.1, presence questions): a hand at work, an untrusted hand, a held world stone, or a
            # stone a hand took away
            tr.busy_t = t
        if push:
            tr.push_t = t
        if hole:  # frames the rules could not read came before this one (4.6.1)
            self._unread(tr, t)
        if tr.arrived is None:
            if not incomplete and self._calm >= CALM and self._quiet >= QUIET and w.stats.confirmed == w.stats.stones:
                # looks read before the calm run that ends here may show another stone: hands worked since
                for wid in [wid for wid, e in tr.store.items() if e.g < self._calm_g]:
                    del tr.store[wid]
                tr.calm_world = w
                self._arrive(tr, w, ref, out)
                tr.arrival_links = {wid: s.id for wid, s in tr.link.items()}  # 4.6.12
                tr.ids_at_arrival = set(tr.ids)
                self._look_work(tr, w, ref, by_id, out)
            return
        # 1. where hands worked
        for e in events:
            if e.type in _HAND_EVENTS or (e.type == "push" and e.side) or (e.type == "delete" and e.why == "hand"):
                if e.id in by_id:
                    tr.marks.append((t, place_of(by_id[e.id])))
                elif e.id in tr.link:
                    tr.marks.append((t, tr.link[e.id].place))
                else:
                    continue
                tr.last_mark_t = t
        # 2. the leaving point
        if hand:
            tr.worked = True
        if not incomplete and self._calm >= CALM:
            tr.calm_ref, tr.worked, tr.calm_world = ref, False, w
        # 3. the world's events on linked stones (and the disturbances and lifts the look rules read)
        ends: list[int] = []
        late: list[int] = []
        for e in events:
            eid = e.id
            s = None if eid is None else tr.link.get(eid)
            if s is None or eid is None:
                if e.type == "push" and eid is not None:
                    late.append(eid)
                    u = tr.unid.get(eid)
                    if u is not None and u.freed:
                        self._freed_push(tr, u, ref)
                continue
            if _hand_touch(e):
                s.hand_t = t
            if e.type in _HANDLED:
                s.handled = s.lifted = True
                s.disturbed = s.touched = ref.g
                s.handled_t = t
            elif e.type == "push":
                self._push(tr, s, ref)
            elif e.type in _ENDS and eid not in ends:
                ends.append(eid)
        for wid in ends:
            s = tr.link.pop(wid)
            for e in events:
                if e.id == wid:
                    s.carried_away |= e.type == "gone"
                    s.dropped |= e.type == "drop"
                    s.emptied |= e.type == "delete" and e.why == "hand"
            self._lose(tr, s, ref)
        # a busy frame or a push frame spoils the unseen run of a linked stone not detected on it (4.6.1)
        if tr.busy_t == t or tr.push_t == t:
            for wid in sorted(tr.link):
                s = tr.link[wid]
                ws = by_id.get(wid)
                if ws is not None and not ws.detected:
                    for rec in (s.run1, None if s.question is None else s.question.watch):
                        if rec is not None and t > rec.seen_t:
                            rec.spoilt = True
        # 4. linked stones in this frame's world
        for wid in sorted(tr.link):
            s = tr.link[wid]
            ws = by_id.get(wid)
            if ws is None:
                if wid in tr.blinks:
                    continue
                del tr.link[wid]
                self._lose(tr, s, ref)
                continue
            if ws.mode in _HELD:
                s.handled = s.lifted = True
                s.disturbed = s.touched = ref.g
                s.handled_t = t
                continue
            if ws.shape == "frozen":
                if s.frozen and not compatible(size_of(ws), s.size):
                    del tr.link[wid]
                    self._doubt(tr, s, "its world stone changed size", "size_changed", ref, out, ref.g + 1)
                    self._lose(tr, s, ref)
                    tr.unid[wid] = _Unid(born=ref, place=place_of(ws), size=size_of(ws), resized=True)
                    continue
                if not s.frozen:
                    s.size, s.frozen = size_of(ws), True
            if ws.detected:
                s.place, s.seen_t = place_of(ws), t
                for rec in (s.run1, None if s.question is None else s.question.watch):  # the run's first sight (4.6.1)
                    if rec is not None and rec.again is None and t > rec.c1_t and not rec.spoilt:
                        rec.again = _Again(
                            t,
                            s.place,
                            tr.busy_t is not None and tr.busy_t >= rec.seen_t,
                            tr.push_t is not None and tr.push_t >= rec.seen_t,
                            self._crowded(s, rec, by_id),
                        )
        # 5. world stones no tray stone stands for
        for ws in w.stones:
            if ws.id in tr.link or ws.id in tr.unid or ws.mode in _HELD:
                continue
            if inside_board(tr.board, place_of(ws)):
                tr.unid[ws.id] = _Unid(born=ref, place=place_of(ws), size=size_of(ws))
        for wid in sorted(tr.unid):
            found = by_id.get(wid)
            if found is None:
                del tr.unid[wid]
                continue
            u = tr.unid[wid]
            if not continuous:
                u.detections = 0
            if u.boundary is not None and not (
                continuous
                and undisturbed
                and tr.calm_ref is not None
                and tr.calm_ref.g == u.boundary.anchor_g
                and 0 <= t - u.boundary.t <= RENAME_T
                and found.shape == "frozen"
                and compatible(size_of(found), size_of(u.boundary.stone))
                and dist(place_of(found), place_of(u.boundary.stone)) <= RENAME_D
                and overlap_share(found, w.stones) < OVERLAP
                and all(e.type in ("confirm", "freeze") for e in events if e.id == wid)
                and self._blink_neighbours(found, w.stones, include_kept=True) <= u.boundary.neighbours
            ):
                u.boundary = None
            if found.detected:
                if u.boundary is not None:
                    u.boundary.latest = found
                old = anchor_stones.get(wid)
                if (
                    anchor is not None
                    and old is not None
                    and not old.detected
                    and u.detections >= 2
                    and u.born.g < ref.g - 1
                    and old.shape == found.shape == "frozen"
                    and compatible(size_of(found), size_of(old))
                    and dist(place_of(found), place_of(old)) <= NEAR
                    and overlap_share(found, w.stones) < OVERLAP
                ):
                    neighbourhood = self._blink_neighbours(old, anchor.stones, include_kept=True)
                    if self._blink_neighbours(found, w.stones, include_kept=True) <= neighbourhood:
                        u.boundary = _BoundarySight(ref.g - 1, t, found, found, neighbourhood)
                u.detections = min(2, u.detections + 1)
                u.place = place_of(found)
            u.size = size_of(found)
        # 6. renames, 7. the settle at a calm moment, the look work in a later calm frame
        self._renames(tr, by_id, ref, out)
        # a push on an id linked only now (lost at step 3, renamed at step 6): read as step 3 reads it
        for wid in dict.fromkeys(late):
            if wid in tr.link:
                self._push(tr, tr.link[wid], ref)
        if incomplete:
            if self._ordinary_calm == CALM:
                for s in tr.stones:
                    observed = by_id.get(s.world) if s.world is not None else None
                    if (
                        s.status == "on"
                        and s.sure
                        and s.world is not None
                        and observed is not None
                        and not observed.detected
                    ):
                        prior = self._partial_unseen.get(s.index)
                        if prior is not None and prior[0] == s.world and prior[2] != self._quiet_epoch:
                            self._doubt(tr, s, "not seen at two calm moments", "unseen", ref, out, ref.g)
                        self._partial_unseen[s.index] = (s.world, ref.g, self._quiet_epoch)
            return
        if self._calm == CALM:
            self._settle(tr, w, ref, by_id, out)
        elif self._calm > CALM:
            self._look_work(tr, w, ref, by_id, out)
        if previous_visit and quiet_frame and tr.calm_ref is not None and tr.calm_ref.g < self._visit_start:
            places = {
                s.id: (s.world, place)
                for s in tr.stones
                if s.world is not None and (place := self._observed_place(s, by_id)) is not None
            }
            self._clean_candidate = _VisitFrame(w, ref, places)
        if tr.calm_ref is ref:  # a calm frame: each stone's place in it, if it was seen in it (4.7)
            for s in tr.stones:
                s.calm_place = self._observed_place(s, by_id)

    @staticmethod
    def _inventory_gap(tr: _Tray, ref: TrayPoint, by_id: dict[int, StoneView], quiet: bool, hole: bool) -> bool:
        """Veto incomplete calm frames independently of whether an absent stone can keep its identity."""
        missing = tr.missing_frames
        if not quiet or hole or tr.last is None or tr.last[1].g + 1 != ref.g:
            missing.clear()
            return False
        for wid, first in list(missing.items()):
            if wid in by_id or ref.g - first >= 2:
                del missing[wid]
        for ws in tr.last[0].stones:
            if ws.id not in by_id:
                missing[ws.id] = ref.g
        return bool(missing)

    @staticmethod
    def _blink_neighbours(anchor: StoneView, stones: Sequence[StoneView], *, include_kept: bool = False) -> set[int]:
        """Other nearby compatible observations. A boundary sight also accounts for retained neighbours."""
        size, place = size_of(anchor), place_of(anchor)
        return {
            ws.id
            for ws in stones
            if ws.id != anchor.id
            and (ws.detected or include_kept)
            and ws.mode not in _HELD
            and compatible(size_of(ws), size)
            and dist(place_of(ws), place) <= RENAME_D
        }

    def _blinks(
        self, tr: _Tray, w: WorldLayer, ref: TrayPoint, by_id: dict[int, StoneView], undisturbed: bool, hole: bool
    ) -> None:
        if tr.arrived is None or tr.last is None:
            return
        previous, last = tr.last
        continuous = last.g + 1 == ref.g and not hole
        silent = continuous and undisturbed and not w.events
        for wid, blink in list(tr.blinks.items()):
            ws = by_id.get(wid)
            near = self._blink_neighbours(blink.previous, w.stones)
            no_new_rival = near <= blink.neighbours
            if ws is None and silent and no_new_rival and ref.g - blink.first.g < 2:
                blink.neighbours = near
                continue
            del tr.blinks[wid]
            events = [e.type for e in w.events if e.id == wid]
            if (
                ws is not None
                and continuous
                and undisturbed
                and no_new_rival
                and ws.detected
                and all(e in ("confirm", "freeze") for e in events)
                and compatible(size_of(ws), size_of(blink.previous))
                and blink.original_place_is_nearest(ws)
            ):
                if blink.unid is not None:
                    tr.unid[wid] = blink.unid
            elif wid in tr.link and not any(e in _ENDS for e in events):
                self._lose(tr, tr.link.pop(wid), blink.first)
        if silent and tr.last_undisturbed:
            for ws in previous.stones:
                wid = ws.id
                if wid not in by_id and ws.detected and ws.mode not in _HELD and (wid in tr.link or wid in tr.unid):
                    near = self._blink_neighbours(ws, w.stones)
                    neighbours = self._blink_neighbours(ws, previous.stones)
                    if near <= neighbours:
                        unid = tr.unid.pop(wid, None)
                        if unid is not None:
                            unid.detections, unid.boundary = 0, None
                        others = tuple(
                            (place_of(other), size_of(other))
                            for other in previous.stones
                            if other.id != wid and other.detected and other.mode not in _HELD
                        )
                        tr.blinks[wid] = _Blink(ref, ws, unid, near, others)

    @staticmethod
    def _push(tr: _Tray, s: _Stone, ref: TrayPoint) -> None:
        """A push on tray stone s's world stone: s is pushed, and every tray stone of a compatible size next to it in
        the last snapshot is disturbed."""
        s.pushed = True
        s.disturbed = s.touched = ref.g
        if s.at_k == tr.k:
            for o in tr.stones:
                if o is not s and o.at_k == tr.k and compatible(o.size, s.size) and dist(o.at, s.at) <= HAND_R:
                    o.near_push = True
                    o.disturbed = o.touched = ref.g

    @staticmethod
    def _freed_push(tr: _Tray, u: _Unid, ref: TrayPoint) -> None:
        """A push on a world stone a look unlinked (freed): every tray stone of a compatible size next to it in the
        last snapshot is doubted at the settle as a pushed stone's neighbour is (by looks only)."""
        for o in tr.stones:
            if o.at_k == tr.k and compatible(o.size, u.size) and dist(o.at, u.place) <= HAND_R:
                o.freed_push = o.near_push = True
                o.disturbed = o.touched = ref.g

    def _arrive(self, tr: _Tray, w: WorldLayer, ref: TrayPoint, out: list[TrayFact]) -> None:
        tr.arrived = ref
        tr.board = None if w.draw is None else w.draw.board
        st = stamp(ref.wall, ref.t, self._session)
        for ws in w.stones:
            if ws.mode in _HELD or not inside_board(tr.board, place_of(ws)):
                continue
            s = self._new(tr, f"{st}-{tr.n}-{ws.id}", ws, ref)
            s.calm_place = s.place if ws.detected else None
            tr.stones.append(s)
            tr.link[ws.id] = s
        self._c.arrived += 1
        self._c.stones += len(tr.stones)
        self._fact("arrived", tr, None, ref, out, stones=len(tr.stones))
        self._snapshot(tr, ref)

    @staticmethod
    def _new(tr: _Tray, sid: str, ws: StoneView, born: TrayPoint) -> _Stone:
        return _Stone(
            id=_unique_id(tr, sid),
            index=len(tr.stones),
            world=ws.id,
            world_t=born.t,
            size=size_of(ws),
            frozen=ws.shape == "frozen",
            place=place_of(ws),
            seen_t=born.t,
            born=born,
            first_place=place_of(ws),
            first_world=ws.id,
        )

    @staticmethod
    def _snapshot(tr: _Tray, ref: TrayPoint) -> None:
        tr.k += 1
        for s in tr.stones:
            s.handled = s.pushed = s.near_push = s.freed_push = False
            s.disturbed = None
            if s.status == "on":
                s.at_k, s.at = tr.k, s.place
        tr.settle_t = ref.t
        tr.settle_g = ref.g
        tr.calm_ref, tr.worked = ref, False

    @staticmethod
    def _lose(tr: _Tray, s: _Stone, ref: TrayPoint) -> None:
        s.run1 = None  # a lost stone's unseen run ends (4.6.1): never spoilt while unlinked; on probation, it fails
        s.world, s.status, s.lost_t, s.lost_k = None, "lost", ref.t, tr.k
        s.lost_g, s.lost_by_look = ref.g, False
        s.lost_calm_t = tr.settle_t

    @staticmethod
    def _hand_took(s: _Stone) -> bool:
        """A hand touched the lost stone's link from its last calm moment (or 3.0 s before it was last seen, if that is
        earlier) up to its loss: a stone does not leave a tray by itself (4.6.1, certain removals)."""
        return s.hand_t is not None and s.hand_t >= min(s.lost_calm_t, s.seen_t - RENAME_T)

    # ---- doubts and questions (4.6.4) ----
    @staticmethod
    def _trusted(s: _Stone) -> bool:
        """On the tray and sure, never in question or not sure on this tray, and not linked by a look: without looks
        exactly v1's "on the tray and sure"."""
        return s.status == "on" and s.sure and not s.doubted and not s.by_look

    def _doubt(
        self,
        tr: _Tray,
        s: _Stone,
        why: str,
        cause: str,
        ref: TrayPoint,
        out: list[TrayFact],
        since: int,
        watch: _Watch | None = None,
    ) -> None:
        """Where a reason of 4.6.1 holds: a question for a stone with a chain (not for a box doubt), else `unsure`; an
        `unseen` doubt with a watch (a stone without a chain that nothing could have changed since it was last seen) is
        a presence question (4.6.1, 4.6.4). A later `unseen` doubt changes nothing in a presence question; a doubt of
        any other cause ends it."""
        if not s.sure:
            return
        s.doubted = True
        q = s.question
        if q is not None and q.watch is not None:
            if cause == "unseen":  # still not seen: nothing new
                return
            q.watch = None  # any other doubt: seeing it again no longer settles it
        if (s.chain and cause != "overlap") or (watch is not None and q is None):
            if q is None:
                s.question = _Question(cause, why, since, ref, watch)
            elif since > q.since:
                q.since = since
            return
        if cause == "overlap":
            s.box = True
        self._unsure(tr, s, why, cause, ref, out)

    def _unsure(self, tr: _Tray, s: _Stone, why: str, cause: str, ref: TrayPoint, out: list[TrayFact]) -> None:
        """The fact `unsure`: the stone is not sure for good; a question ends undecided with its own why and code."""
        q = s.question
        s.sure, s.question = False, None
        self._c.unsure += 1
        opened = None
        if q is not None:
            why, cause, opened = q.why, q.cause, q.opened
        s.unsure_code = cause
        s.unsure_from_t = ref.t if opened is None else opened.t
        self._fact("unsure", tr, s, ref, out, why=why, code=cause, opened=opened)

    def _end_question(self, tr: _Tray, s: _Stone, ref: TrayPoint, out: list[TrayFact]) -> None:
        """A question ends undecided (before a certain removal, or when the tray leaves)."""
        q = s.question
        if q is not None:
            self._unsure(tr, s, q.why, q.cause, ref, out)

    @staticmethod
    def _since_of(s: _Stone, ref: TrayPoint) -> int:
        """The first frame whose looks count for a hand doubt: after the stone's latest disturbance (4.6.4)."""
        return s.disturbed + 1 if s.disturbed is not None else ref.g

    @staticmethod
    def _unread(tr: _Tray, t: float) -> None:
        """Frames the rules could not read came before the frame at t: it counts as a busy and a push frame, and every
        open unseen run (a stone's, and a presence question's) is spoilt, its stone detected or not (4.6.1)."""
        tr.busy_t = t if tr.busy_t is None else max(tr.busy_t, t)
        tr.push_t = t if tr.push_t is None else max(tr.push_t, t)
        for s in tr.stones:
            for rec in (s.run1, None if s.question is None else s.question.watch):
                if rec is not None:
                    rec.spoilt = True

    @staticmethod
    def _crowded(s: _Stone, w: _Watch, by_id: dict[int, StoneView]) -> bool:
        """Another world stone of the stone's size within NEAR of the place where it was last seen before its unseen
        run: a sight there can not tell the two apart (4.6.1)."""
        return any(
            ws.id != w.wid
            and ws.mode not in _HELD
            and compatible(size_of(ws), s.size)
            and dist(place_of(ws), w.place) <= NEAR
            for ws in by_id.values()
        )

    def _look_against(self, tr: _Tray, s: _Stone, w: _Watch, by_id: dict[int, StoneView]) -> bool:
        """A look of the stone's world stone read since it was last seen, still fresh, that reaches the bar against the
        chain of another tray stone the look step pairs looks with (lost, or on the tray in question): that stone may
        be on this world stone, so no sight settles the question (4.6.1)."""
        e = tr.store.get(w.wid)
        if e is None or e.t <= w.seen_t or not self._fresh(tr, e, by_id):
            return False
        bar = self._t_id(tr)
        for o in tr.stones:
            if (
                o is s
                or not o.chain
                or not o.sure
                or not compatible(o.size, e.size)
                or not (o.status == "lost" or (o.status == "on" and o.question is not None))
            ):
                continue
            sc = self._chain_score(tr, o, e)
            if sc is not None and sc[0] >= bar and sc[1] >= N_ID:
                return True
        return False

    def _watch_open(self, tr: _Tray, s: _Stone, w: _Watch, by_id: dict[int, StoneView]) -> bool:
        """Nothing could have changed the stone since it was last seen (4.6.1): its run is not spoilt, it was not
        handled or held since, no hand mark since lies within HAND_R of its last place or of its place now, and no look
        of its world stone since shows another open stone (`_look_against`)."""
        if s.handled_t is not None and s.handled_t >= w.seen_t:
            return False
        if w.spoilt:
            return False
        if self._hands_near(tr, w.seen_t, (w.place, s.place)):
            return False
        return not self._look_against(tr, s, w, by_id)

    def _seen_again(self, s: _Stone, w: _Watch, by_id: dict[int, StoneView]) -> bool:
        """The sight half of a settle (4.6.1, presence questions): linked to the run's world stone; first seen after
        the run's first calm moment within NEAR of where it was last seen before the run, with no busy or push frame
        from that last sight up to then and no other stone of its size within NEAR of that place then; seen within NEAR
        of that place last, after that calm moment; and no other stone of its size within NEAR of it now."""
        a = w.again
        if a is None or dist(a.place, w.place) > NEAR or a.busy or a.push or a.crowd:
            return False
        return (
            s.status == "on"
            and s.world == w.wid
            and s.seen_t > w.c1_t
            and dist(s.place, w.place) <= NEAR
            and not self._crowded(s, w, by_id)
        )

    def _clean_since(self, tr: _Tray, s: _Stone, by_id: dict[int, StoneView]) -> bool:
        """The leaving may leave the last snapshot's own frame uncounted for this stone (4.6.1, Leaving): its unseen run
        is open (`_watch_open`), and no busy frame and no push frame came since it was last seen."""
        r = s.run1
        if r is None or not self._watch_open(tr, s, r, by_id):
            return False
        return not (
            (tr.busy_t is not None and tr.busy_t >= r.seen_t) or (tr.push_t is not None and tr.push_t >= r.seen_t)
        )

    def _watch_of(
        self, tr: _Tray, s: _Stone, k: int, births: list[tuple[Place, Size]], by_id: dict[int, StoneView]
    ) -> _Watch | None:
        """The watch of an unseen doubt (a copy of the stone's unseen run): none for a stone with a chain (only a look
        settles its question), when another cause of the settle's step 1 holds now, or when something could have
        changed the stone since it was last seen (4.6.1)."""
        r = s.run1
        if s.chain or r is None or r.wid != s.world or r.spoilt or s.pushed or s.freed_push:
            return None
        if any(
            o is not s and o.pushed and o.at_k == k and compatible(o.size, s.size) and dist(o.at, s.at) <= HAND_R
            for o in tr.stones
        ):
            return None
        if s.handled and any(
            compatible(size, s.size) and min(dist(place, s.place), dist(place, s.at)) <= HAND_R
            for place, size in births
        ):
            return None
        w = _Watch(r.wid, r.place, r.seen_t, r.c1_t, r.again)
        return w if self._watch_open(tr, s, w, by_id) else None

    @staticmethod
    def _hands_near(tr: _Tray, since: float, places: Iterable[Place]) -> bool:
        pl = list(places)
        for mt, mp in reversed(tr.marks):
            if mt < since:
                break
            if any(dist(mp, q) <= HAND_R for q in pl):
                return True
        return False

    def _relink(
        self,
        tr: _Tray,
        s: _Stone,
        wid: int,
        by_id: dict[int, StoneView],
        sure: bool,
        why: str,
        cause: str,
        ref: TrayPoint,
        out: list[TrayFact],
    ) -> None:
        u = tr.unid.pop(wid)
        s.world, s.status = wid, "on"
        s.touched = ref.g
        s.world_t = u.born.t
        s.carried_away = s.dropped = s.emptied = False
        ws = by_id[wid]
        s.place = place_of(ws)
        if ws.detected:
            s.seen_t = ref.t
        tr.link[wid] = s
        if sure:
            return
        if cause == "matched_at_leaving":  # never a question
            if s.sure:
                s.doubted = True
                self._unsure(tr, s, why, cause, ref, out)
        else:
            self._doubt(tr, s, why, cause, ref, out, ref.g + 1)

    def _renames(self, tr: _Tray, by_id: dict[int, StoneView], ref: TrayPoint, out: list[TrayFact]) -> None:
        """A lost stone and one unidentified stone at its place, within RENAME_T s of the loss: the world renamed it."""
        lost = [s for s in tr.stones if s.status == "lost" and not s.dropped]
        if not lost or not tr.unid:
            return
        for s in lost:
            near = [
                wid
                for wid in sorted(tr.unid)
                if abs(tr.unid[wid].born.t - s.lost_t) <= RENAME_T
                and compatible(tr.unid[wid].size, s.size)
                and dist(tr.unid[wid].place, s.place) <= RENAME_D
            ]
            if len(near) != 1:
                continue
            wid = near[0]
            u = tr.unid[wid]
            if any(
                o is not s
                and o.status == "lost"
                and compatible(u.size, o.size)
                and abs(u.born.t - o.lost_t) <= RENAME_T
                and dist(o.place, u.place) <= RENAME_D
                for o in lost
            ):
                continue
            sure = (
                s.sure
                and s.question is None
                and s.lost_k == tr.k
                and not s.carried_away
                and not s.emptied
                and not self._hands_near(tr, min(s.lost_t, u.born.t) - RENAME_T, (s.place, u.place))
            )
            self._relink(tr, s, wid, by_id, sure, "found again, not for sure", "found_again", ref, out)

    def _rivals(self, tr: _Tray, s: _Stone) -> bool:
        """A world stone that could be this lost stone: unidentified, or standing for a tray stone that is not trusted,
        of a compatible size, first seen after it was last seen (less RENAME_T) or at its place."""
        for u in tr.unid.values():
            if compatible(u.size, s.size) and (u.born.t >= s.seen_t - RENAME_T or dist(u.place, s.place) <= RENAME_D):
                return True
        return any(
            o.status == "on"
            and not self._trusted(o)
            and compatible(o.size, s.size)
            and (o.world_t >= s.seen_t - RENAME_T or dist(o.place, s.place) <= RENAME_D)
            for o in tr.stones
        )

    def _settle(
        self,
        tr: _Tray,
        w: WorldLayer,
        ref: TrayPoint,
        by_id: dict[int, StoneView],
        out: list[TrayFact],
        final: bool = False,
    ) -> None:
        t, k = ref.t, tr.k
        world = [ws for ws in w.stones if ws.mode not in _HELD]
        births: list[tuple[Place, Size]] = [
            (u.place, u.size) for _, u in sorted(tr.unid.items()) if u.born.t > tr.settle_t
        ]
        births += [
            (s.place, s.size)
            for s in tr.stones
            if s.status == "on" and not self._trusted(s) and s.world_t > tr.settle_t
        ]
        # 1. sure stones (and stones in question), one by one in tray order: is the world still showing them?
        # the leaving tests the last frame again only when it is not the last snapshot's own frame, or when something
        # could have changed the stone since it was last seen (4.6.1, Leaving)
        same_frame = final and tr.last is not None and tr.last[1].g == tr.settle_g
        # at the leaving, the stones its speckle answer pairs are not tested: the answer settles them (4.6.13)
        sp = self._leaving_answer(tr, ref) if final else None
        paired = set() if sp is None else {t[0].index for t in self._speckle_targets(tr, sp, by_id)}
        for s in tr.stones:
            if s.status != "on" or not s.sure or s.at_k != k or s.index in paired:
                continue
            ws = by_id.get(s.world) if s.world is not None else None
            partial = self._partial_unseen.get(s.index)
            if (
                partial is not None
                and tr.last is not None
                and partial[1] != tr.last[1].g
                and (final or partial[2] != self._quiet_epoch)
            ):
                del self._partial_unseen[s.index]
                if partial[0] == s.world and ws is not None and not ws.detected:
                    self._doubt(tr, s, "not seen at two calm moments", "unseen", ref, out, ref.g)
                    continue
            q, settled = s.question, False
            if q is not None and q.watch is not None:  # a presence question (4.6.1)
                if not self._watch_open(tr, s, q.watch, by_id):  # something could have changed it: it ends
                    self._unsure(tr, s, q.why, q.cause, ref, out)
                    continue
                if self._seen_again(s, q.watch, by_id):  # seen again: settled, sure again, no fact
                    s.question, s.ghost, s.run1, settled = None, 0, None, True
            if ws is not None and overlap_share(ws, world) >= OVERLAP:
                self._doubt(tr, s, "its box overlaps another stone", "overlap", ref, out, ref.g)
                continue
            if ws is not None and not ws.detected:
                if same_frame and self._clean_since(tr, s, by_id):
                    pass  # the leaving on the last snapshot's own frame, nothing since its last sight: tested there
                else:
                    s.ghost += 1
                    if s.ghost == 1:
                        s.run1 = _Watch(ws.id, s.place, s.seen_t, t)
                        if settled:  # settled on a calm moment that does not show it: its next run is on probation
                            s.once = True
                    if s.ghost >= GHOST:
                        q = s.question
                        if q is not None and q.watch is not None:  # still watched, still not seen
                            self._doubt(tr, s, "not seen at two calm moments", "unseen", ref, out, ref.g)
                        else:
                            wt = self._watch_of(tr, s, k, births, by_id)
                            s.once = False
                            self._doubt(tr, s, "not seen at two calm moments", "unseen", ref, out, ref.g, wt)
                            q = s.question
                            if wt is not None and q is not None and q.watch is wt and self._seen_again(s, wt, by_id):
                                # seen since the first unseen calm moment: settled; not detected now, so this calm
                                # moment starts its next unseen run, on probation
                                s.question, s.ghost, s.once = None, 1, True
                                s.run1 = _Watch(ws.id, s.place, s.seen_t, t)
                            continue
            else:
                r, once = s.run1, s.once
                s.ghost, s.run1, s.once = 0, None, False
                if once and not (r is not None and self._watch_open(tr, s, r, by_id) and self._seen_again(s, r, by_id)):
                    # its run on probation ends with a sight that does not settle it: something could have changed the
                    # stone while it was out of sight (4.6.1)
                    self._doubt(tr, s, "not seen at two calm moments", "unseen", ref, out, ref.g)
                    continue
            if s.pushed:
                self._doubt(tr, s, "it was pushed", "pushed", ref, out, self._since_of(s, ref))
                continue
            if any(
                o is not s and o.pushed and o.at_k == k and compatible(o.size, s.size) and dist(o.at, s.at) <= HAND_R
                for o in tr.stones
            ):
                self._doubt(
                    tr, s, "a stone next to it was pushed", "neighbour_pushed", ref, out, self._since_of(s, ref)
                )
                continue
            if s.freed_push:
                self._doubt(
                    tr, s, "a stone next to it was pushed", "neighbour_pushed", ref, out, self._since_of(s, ref)
                )
                continue
            if s.handled and any(
                compatible(size, s.size) and min(dist(place, s.place), dist(place, s.at)) <= HAND_R
                for place, size in births
            ):
                why = "a stone appeared next to it while a hand held it"
                self._doubt(tr, s, why, "appeared", ref, out, self._since_of(s, ref))
        # 2. the look work
        self._look_work(tr, w, ref, by_id, out)
        if final:
            # the leaving's speckle answer (4.6.13): the unit's pairs confirm, relink and find stones again
            self._speckle_leaving(tr, ref, by_id, out)
            self._leave(tr, w, by_id, ref, out)
            return
        # 3. certain removals, on a tray that is not relinked: v1's rule; a twin keeps a stone v1 removes, and relinks
        #    the tray
        for s in tr.stones:
            if tr.relinked:
                break
            if s.status != "lost" or (s.lost_k >= k and t - s.lost_t < RENAME_T):
                continue
            if any(covers(ws, s.place, MARGIN) for ws in world) or self._rivals(tr, s) or not self._hand_took(s):
                continue
            if s.id in tr.twins:
                tr.relinked = True
                break
            self._end_question(tr, s, ref, out)
            s.status, s.certain = "removed", True
            self._c.removed += 1
            code = "carried_away" if s.carried_away else "missing"
            self._fact("removed", tr, s, ref, out, certain=True, why=code.replace("_", " "), code=code)
        # 4. certain additions
        doubtful = [s.size for s in tr.stones if s.status == "lost" or (s.status == "on" and not self._trusted(s))]
        for wid in sorted(tr.unid):
            u, found = tr.unid[wid], by_id.get(wid)
            if found is None or not found.detected:
                u.seen, u.last = 0, None
                continue
            u.seen = u.seen + 1 if (u.last is not None and dist(u.last, u.place) <= NEAR) else 1
            u.last = u.place
            if (
                u.seen >= 2
                and not u.freed
                and not u.resized
                and not tr.relinked
                and not any(compatible(u.size, z) for z in doubtful)
            ):
                self._mint(tr, wid, u, found, True, ref, out)
        # 5. the snapshot
        self._snapshot(tr, ref)

    # ---- the leaving's speckle answer (4.6.13) ----
    @staticmethod
    def _leaving_answer(tr: _Tray, ref: TrayPoint) -> WorldSpeckle | None:
        """The newest answer read while the tray was on whose later moment is within SPECKLE_NEAR frames of the
        leaving point, else `None`."""
        for sp in reversed(tr.answers):
            if abs(sp.at.g - ref.g) <= SPECKLE_NEAR:
                return sp
        return None

    def _speckle_targets(
        self, tr: _Tray, sp: WorldSpeckle, by_id: dict[int, StoneView]
    ) -> list[tuple[_Stone, int, Any, str, int | None]]:
        """The leaving answer's pairs that link a tray stone, in the answer's order: (stone, later world id, the
        pair's clean find, the stone's status and world id before). A pair with no score in its finds is left out
        unless its stone is sure and already on its world stone (no fact needed)."""
        by_tray = {s.id: s for s in tr.stones}
        refs = {x.id: x for x in sp.refs}
        later = {x.id: x for x in sp.stones}
        targets: list[tuple[_Stone, int, Any, str, int | None]] = []
        claimed: set[int] = set()
        used: set[int] = set()
        for c, r in sp.pairs:
            sid = tr.arrival_links.get(r)
            s = by_tray.get(sid) if sid is not None else None
            ws = tr.point.get(c, by_id.get(c))
            if (
                s is None
                or s.added
                or s.status not in ("on", "lost")
                or ws is None
                or ws.mode in _HELD
                or c in claimed
                or s.index in used
            ):
                continue
            # a stone nothing disturbed keeps its world stone, and keeps it from others: on look-alike pavers a pair
            # can join two neighbours, while an undisturbed stone has not moved
            y = tr.link.get(c)
            if (self._trusted(s) and s.world != c) or (y is not None and y is not s and self._trusted(y)):
                continue
            x = refs.get(r)
            if x is None or not _speckle_clean(x):
                x = later.get(c)
            # a pair whose finds give no score is skipped where it would need a fact: unless its stone is sure and
            # already on c (confirmed, no fact)
            if (x is None or x.score is None) and not (s.status == "on" and s.world == c and s.sure):
                continue
            targets.append((s, c, x, s.status, s.world))
            claimed.add(c)
            used.add(s.index)
        return targets

    def _speckle_leaving(self, tr: _Tray, ref: TrayPoint, by_id: dict[int, StoneView], out: list[TrayFact]) -> None:
        """The unit's pairs at the leaving (4.6.13): a stone already on its pair's world stone is confirmed (its
        question ends; one not sure is sure again, `speckle_same`); any other is linked to it, sure (`speckle_moved`
        or `speckle_found`): the stone that stood there is lost, and the moved stone's world stone is unidentified
        again when it is on the board, not held and in no pair."""
        sp = self._leaving_answer(tr, ref)
        tr.leaving_answer = sp
        if sp is None:
            return
        targets = self._speckle_targets(tr, sp, by_id)
        claimed = {c for _, c, _, _, _ in targets}
        for s, c, _, status, w0 in targets:
            if status == "on" and w0 == c:
                continue
            y = tr.link.pop(c, None)
            if y is not None and y is not s:
                self._lose(tr, y, ref)
            u = tr.unid.pop(c, None)
            if s.status == "on" and s.world is not None and tr.link.get(s.world) is s:
                old = s.world
                del tr.link[old]
                ws0 = by_id.get(old)
                if (
                    old not in claimed
                    and ws0 is not None
                    and ws0.mode not in _HELD
                    and inside_board(tr.board, place_of(ws0))
                ):
                    tr.unid[old] = _Unid(born=ref, place=place_of(ws0), size=size_of(ws0), freed=True)
            ws = tr.point.get(c, by_id.get(c))
            assert ws is not None
            s.world_t = u.born.t if u is not None else (y.world_t if y is not None else ref.t)
            s.world, s.status = c, "on"
            s.touched = ref.g
            s.carried_away = s.dropped = s.emptied = False
            s.place = place_of(ws)
            if ws.detected:
                s.seen_t = ref.t
            tr.link[c] = s
            s.at_k, s.at = tr.k, s.place
        for s, c, x, status, w0 in targets:
            s.question, s.by_speckle = None, True
            if status == "on" and w0 == c:
                if s.sure:
                    continue  # sure, or a question settled: no fact
                code, why, frm = "speckle_same", "confirmed by its speckle", None
            elif status == "lost":
                code, why, frm = "speckle_found", "found again by its speckle", None
            else:
                code, why, frm = "speckle_moved", "recognised by its speckle", w0
            s.sure = True
            p, q = permille(x.score), permille(x.second)
            assert p is not None
            self._fact(
                "identified",
                tr,
                s,
                ref,
                out,
                why=why,
                code=code,
                from_world=frm,
                score=p,
                margin=None if q is None else p - q,
                bar=SPECKLE_CLEAN,
            )

    def _leave(
        self, tr: _Tray, w: WorldLayer, by_id: dict[int, StoneView], ref: TrayPoint, out: list[TrayFact]
    ) -> None:
        """The rest of the leaving (4.6.7): questions end, v1's pairing (only pairs with evidence), unclear removals
        and additions, the open account, the snapshot."""
        for s in tr.stones:
            self._end_question(tr, s, ref, out)
        lost = [s for s in tr.stones if s.status == "lost"]
        pairs = sorted(
            (dist(u.place, s.place), s.index, wid)
            for s in lost
            for wid, u in tr.unid.items()
            if compatible(u.size, s.size) and dist(u.place, s.place) <= LEAVE_D and self._same_stone(tr, s, wid, u)
        )
        used_s: set[int] = set()
        used_w: set[int] = set()
        for _, i, wid in pairs:
            if i in used_s or wid in used_w:
                continue
            used_s.add(i)
            used_w.add(wid)
            why = "matched when the tray left"
            self._relink(tr, tr.stones[i], wid, by_id, False, why, "matched_at_leaving", ref, out)
        for s in tr.stones:
            if s.status == "lost":
                s.status, s.certain, s.left_now = "removed", False, True
                tr.unclear += 1
                self._c.removed_unclear += 1
                why = "not found when the tray left"
                self._fact("removed", tr, s, ref, out, certain=False, why=why, code="not_found")
        for wid in sorted(tr.unid):
            found = by_id.get(wid)
            sight = tr.unid[wid].boundary
            if found is not None and not found.detected and sight is not None and sight.anchor_g == ref.g:
                found = sight.latest
            kept = False
            if found is not None and not found.detected:
                # detected, not held, in the leaving point's world: positive evidence too (no hand worked since)
                at = tr.point.get(wid)
                if at is not None and at.detected and at.mode not in _HELD:
                    found = at
                elif (
                    at is not None
                    and at.mode not in _HELD
                    and overlap_share(at, [o for o in tr.point.values() if o.mode not in _HELD]) < OVERLAP
                ):
                    # kept, not held, in the leaving point's world, less than OVERLAP of its box under another unheld
                    # stone's: the world kept it on the tray although the model did not box it there (version 7)
                    found, kept = at, True
            if found is not None and (found.detected or kept):
                self._mint(tr, wid, tr.unid[wid], found, False, ref, out)
        tr.open = self._open_account(tr, w)
        tr.unid = {}
        self._snapshot(tr, ref)

    def _same_stone(self, tr: _Tray, s: _Stone, wid: int, u: _Unid) -> bool:
        """The leaving's evidence (4.6.1, Leaving; 4.6.7, step 4) that the unidentified world stone W is the lost tray
        stone S, which a pair needs: their looks agree (S's chain against W's stored look: at least the tray's bar over
        at least N_ID cells); or, when the looks do not disagree (below T_DIFF over at least N_ID cells), nothing could
        have put another stone there: W was first seen at or after S was last seen, less RENAME_T, and no hand mark
        from the earliest of S's last sight, S's loss and W's first sight, less RENAME_T, lies within HAND_R of S's
        place or of W's."""
        e = tr.store.get(wid)
        x = self._chain_score(tr, s, e) if e is not None and s.chain else None
        bar = self._t_id(tr)
        if x is not None and x[1] >= N_ID and not T_DIFF <= x[0] < bar:
            return x[0] >= bar  # the looks agree, or they disagree
        return u.born.t >= s.seen_t - RENAME_T and not self._hands_near(
            tr, min(s.seen_t, s.lost_t, u.born.t) - RENAME_T, (s.place, u.place)
        )

    def _mint(
        self, tr: _Tray, wid: int, u: _Unid, ws: StoneView, certain: bool, ref: TrayPoint, out: list[TrayFact]
    ) -> None:
        s = self._new(tr, f"{stamp(u.born.wall, u.born.t, self._session)}-{tr.n}-{wid}", ws, u.born)
        s.sure, s.added, s.certain = certain, True, certain
        s.doubted, s.unid_rec, s.added_now = not certain, u, not certain
        tr.stones.append(s)
        tr.link[wid] = s
        del tr.unid[wid]
        if certain:
            tr.certain_added += 1
            self._c.added += 1
        else:
            tr.unclear += 1
            self._c.added_unclear += 1
        why = "set down" if certain else "not recognised when the tray left"
        code = "set_down" if certain else "not_recognised"
        self._fact("added", tr, s, ref, out, certain=certain, why=why, code=code)

    # ---- looks on the tray (4.6.3) ----
    @staticmethod
    def _store_looks(tr: _Tray, w: WorldLayer, by_id: dict[int, StoneView], ref: TrayPoint) -> None:
        """The look store: forget the ids of this frame's look clearers and of stones gone, then keep this frame's
        usable looks of stones not held inside the board (any place before the arrival)."""
        store = tr.store
        for e in w.events:
            if e.type in _LOOK_CLEARERS and e.id is not None:
                store.pop(e.id, None)
        for wid in [wid for wid in store if wid not in by_id]:
            del store[wid]
        for ws in w.stones:
            if ws.mode in _HELD or not inside_board(tr.board, place_of(ws)):
                continue
            c = usable_look(ws.look)
            if c is not None:
                store[ws.id] = _Look(ws.id, c, ref.g, ref.t, place_of(ws), size_of(ws), detected=ws.detected)

    @staticmethod
    def _fresh(tr: _Tray, e: _Look, by_id: dict[int, StoneView]) -> bool:
        """A stored look still describes its stone: the stone is here, not held, within NEAR of where the look was
        read, and no hand mark after the look lies within HAND_R of that place."""
        ws = by_id.get(e.wid)
        if ws is None or ws.mode in _HELD or dist(place_of(ws), e.place) > NEAR:
            return False
        for mt, mp in reversed(tr.marks):
            if mt <= e.t:
                break
            if dist(mp, e.place) <= HAND_R:
                return False
        return True

    @staticmethod
    def _t_id(tr: _Tray) -> int:
        if tr.alike_n < GAUGE_MIN or tr.alike is None:
            return T_ID_START
        return max(T_ID_MIN, tr.alike + GAP)

    @staticmethod
    def _p(tr: _Tray, a: _Look, b: _Look) -> tuple[int, int] | None:
        """(permille score, pairs) of look a against look b, or None; each pair of looks is scored once a tray."""
        key = (a.wid, a.g, b.wid, b.g)
        if key not in tr.memo:
            r = pair_cells(a.cells, b.cells)
            tr.memo[key] = None if r is None else (math.floor(r[0] * 1000 + 0.5), r[1])
        return tr.memo[key]

    def _chain_score(self, tr: _Tray, s: _Stone, look: _Look) -> tuple[int, int] | None:
        """A tray stone's chain against a look L: against its anchor, or its latest when that scores higher; a chain
        look that is L itself (the same world stone and frame) is not scored."""
        best = None
        for c in s.chain:
            if c.wid == look.wid and c.g == look.g:
                continue
            x = self._p(tr, look, c)
            if x is not None and (best is None or x[0] > best[0]):
                best = x
        return best

    @staticmethod
    def _set_latest(s: _Stone, look: _Look) -> None:
        s.chain = [s.chain[0], look] if s.chain else [look]

    # ---- the look work (4.6.5) ----
    def _look_work(
        self, tr: _Tray, w: WorldLayer, ref: TrayPoint, by_id: dict[int, StoneView], out: list[TrayFact]
    ) -> None:
        self._take_looks(tr, by_id, ref, out)
        self._anchors(tr, by_id)
        while True:
            run = self._look_run(tr, w, by_id)
            tr.last_run = run
            if run.best is None:
                return
            self._apply(tr, run.best, by_id, ref, out)

    def _take_looks(self, tr: _Tray, by_id: dict[int, StoneView], ref: TrayPoint, out: list[TrayFact]) -> None:
        store = tr.store
        take = [wid for wid in sorted(store) if not store[wid].taken and self._fresh(tr, store[wid], by_id)]
        for wid in take[:N_TAKE]:
            e = store[wid]
            e.taken = tr.took = True
            ws = by_id[wid]
            here, size = place_of(ws), size_of(ws)
            # the gauge: how alike two different stones of this tray look, both ways
            partners: list[tuple[float, int]] = []
            for o in by_id.values():
                x = store.get(o.id)
                if (
                    o.id == wid
                    or x is None
                    or not x.taken
                    or not self._fresh(tr, x, by_id)
                    or not inside_board(tr.board, place_of(o))
                    or not compatible(size_of(o), size)
                ):
                    continue
                d = dist(place_of(o), here)
                if d >= 0.5 * size[0]:
                    partners.append((d, o.id))
            for _, oid in sorted(partners)[:GAUGE_K]:
                v = [x[0] for x in (self._p(tr, e, store[oid]), self._p(tr, store[oid], e)) if x is not None]
                if v:
                    tr.alike = max(v) if tr.alike is None else max(tr.alike, max(v))
                    tr.alike_n += 1
            # the own compare of a sure stone with a chain
            s = tr.link.get(wid)
            if s is not None and s.chain and self._untouched(s):
                sc = self._chain_score(tr, s, e)
                if sc is not None and sc[0] >= self._t_id(tr) and sc[1] >= N_ID:
                    self._set_latest(s, e)
                elif sc is not None and sc[0] < T_DIFF and sc[1] >= N_ID:
                    if self._trusted(s):  # v1 trusts it: from here on what the tray trusts may differ from v1's
                        tr.relinked = True
                    self._doubt(tr, s, "its look changed", "look_changed", ref, out, e.g)

    def _anchors(self, tr: _Tray, by_id: dict[int, StoneView]) -> None:
        for s in tr.stones:
            if s.status != "on" or s.chain or s.world is None:
                continue
            e = tr.store.get(s.world)
            if e is None or not e.taken or not self._fresh(tr, e, by_id):
                continue
            if self._untouched(s) and e.detected and s.ghost == 0:
                s.chain = [e]
            else:
                s.late = True

    @staticmethod
    def _untouched(s: _Stone) -> bool:
        """Sure, not in question, never lifted on this tray and not disturbed since the last snapshot."""
        return s.sure and s.question is None and not s.lifted and s.disturbed is None

    def _look_run(self, tr: _Tray, w: WorldLayer, by_id: dict[int, StoneView]) -> _Run:
        """One run of the look step: rows (open tray stones with a chain), columns (open world stones with a fresh
        taken look), their pairs, neighbours, hits and twins, and the decided pair with the highest score."""
        tid, store = self._t_id(tr), tr.store
        rows: list[tuple[_Stone, int]] = []
        for s in tr.stones:
            if not s.chain or not s.sure:
                continue
            q = s.question
            if s.status == "on" and q is not None:
                rows.append((s, q.since))
            elif s.status == "lost":
                if s.lost_by_look and q is not None:
                    rows.append((s, q.since))
                else:
                    rows.append((s, s.lost_g + 1 if q is None else max(s.lost_g + 1, q.since)))
        cols: list[StoneView] = []
        for ws in w.stones:
            wid = ws.id
            if ws.mode in _HELD or not inside_board(tr.board, place_of(ws)):
                continue
            e = store.get(wid)
            if e is None or not e.taken or not self._fresh(tr, e, by_id):
                continue
            y = tr.link.get(wid)
            if (y is None and wid not in tr.unid) or (y is not None and y.sure and y.question is None):
                continue
            cols.append(ws)
        pairs: dict[int, list[Pair]] = {}  # row index -> [(world id, S, n)] nearest first
        last_mark = tr.last_mark_t
        for s, since in rows:
            near: list[tuple[float, int]] = []
            gone = s.status == "lost" and (s.carried_away or s.emptied)
            for ws in cols:
                d, e = dist(s.place, place_of(ws)), store[ws.id]
                if (
                    compatible(s.size, size_of(ws))
                    and d <= D_MATCH
                    and e.g >= since
                    and (last_mark is None or e.t >= last_mark)
                    and not (gone and self._first_seen(tr, ws.id) <= s.lost_t)
                ):
                    near.append((d, ws.id))
            pairs[s.index] = []
            for _, wid in sorted(near)[:K_PAIR]:
                x = self._chain_score(tr, s, store[wid])
                pairs[s.index].append((wid, None if x is None else x[0], None if x is None else x[1]))
        neigh: dict[int, list[Pair]] = {}  # world id -> [(tray index, S, n)] nearest first
        hits: dict[int, set[int]] = {}
        for ws in cols:
            wid, here, size = ws.id, place_of(ws), size_of(ws)
            close = sorted(
                (dist(o.place, here), o.index)
                for o in tr.stones
                if o.status == "on" and o.chain and compatible(o.size, size) and dist(o.place, here) <= D_NEIGH
            )
            neigh[wid] = []
            for _, i in close[:K_NEIGH]:
                x = self._chain_score(tr, tr.stones[i], store[wid])
                neigh[wid].append((i, None if x is None else x[0], None if x is None else x[1]))
            h = {i for i, sc, n in neigh[wid] if sc is not None and n is not None and sc >= tid and n >= N_ID}
            h |= {
                i
                for i, ps in pairs.items()
                for w2, sc, n in ps
                if w2 == wid and sc is not None and n is not None and sc >= tid and n >= N_ID
            }
            hits[wid] = h
        twins = {wid for wid, h in hits.items() if len(h) >= 2}
        for wid in twins:
            tr.twins |= {tr.stones[i].id for i in hits[wid]}
        # decided pairs
        decided: list[tuple[int, int, int, int, int | None, int]] = []
        info: dict[tuple[int, int], tuple[int, int | None]] = {}  # (row, world id) -> (bar, best rival)
        for s, _ in rows:
            i = s.index
            for wid, sc, n in pairs[i]:
                ws = by_id[wid]
                here, size = place_of(ws), size_of(ws)
                bar = tid
                if not (s.status == "on" and s.world == wid) or any(
                    o is not s
                    and o.status != "removed"
                    and not o.chain
                    and compatible(o.size, size)
                    and dist(o.place, here) <= D_NEIGH
                    for o in tr.stones
                ):
                    bar = max(tid, T_FIRM)
                rivals = [s2 for w2, s2, _ in pairs[i] if w2 != wid and s2 is not None]
                rivals += [s2 for j, ps in pairs.items() if j != i for w2, s2, _ in ps if w2 == wid and s2 is not None]
                rivals += [s2 for j, s2, _ in neigh[wid] if j != i and s2 is not None]
                top = max(rivals) if rivals else None
                info[(i, wid)] = (bar, top)
                if (
                    wid not in twins
                    and sc is not None
                    and n is not None
                    and n >= N_ID
                    and sc >= bar
                    and all(sc - r >= M_ID for r in rivals)
                    and not self._unscored(tr, s, wid, here, size)
                ):
                    decided.append((-sc, i, wid, sc, None if top is None else sc - top, bar))
        best = None
        if decided:
            _, i, wid, sc, margin, bar = min(decided)
            best = _Best(i, wid, sc, margin, bar)
        return _Run([(s.index, since) for s, since in rows], [ws.id for ws in cols], pairs, info, best)

    @staticmethod
    def _first_seen(tr: _Tray, wid: int) -> float:
        """When an open world stone was first seen on this tray: its unidentified record, or its tray stone's link."""
        u = tr.unid.get(wid)
        return u.born.t if u is not None else tr.link[wid].world_t

    @staticmethod
    def _unscored(tr: _Tray, s: _Stone, wid: int, here: Place, size: Size) -> bool:
        """A tray stone without a chain that could be on world stone wid: linked to it, within D_NEIGH of it, or
        disturbed since the last snapshot within D_MATCH of it. No score rules it out, so no decision on wid."""
        return any(
            o is not s
            and o.status != "removed"
            and not o.chain
            and compatible(o.size, size)
            and (
                (o.status == "on" and o.world == wid)
                or dist(o.place, here) <= D_NEIGH
                or (o.disturbed is not None and dist(o.place, here) <= D_MATCH)
            )
            for o in tr.stones
        )

    def _apply(self, tr: _Tray, best: _Best, by_id: dict[int, StoneView], ref: TrayPoint, out: list[TrayFact]) -> None:
        """A decided pair: a question settled, or a stone linked by its look (fact `identified`)."""
        s, wid = tr.stones[best.row], best.wid
        look = tr.store[wid]
        if s.status == "on" and s.world == wid:
            s.question = None
            self._set_latest(s, look)
            return
        was_lost = s.status == "lost"
        y = tr.link.pop(wid, None)
        if y is not None:
            self._lose(tr, y, ref)
            y.lost_by_look = y.look_lost = True
        w0 = None
        if s.status == "on" and s.world is not None:
            w0 = s.world
            tr.link.pop(w0, None)
            ws0 = by_id.get(w0)
            if ws0 is not None and ws0.mode not in _HELD and inside_board(tr.board, place_of(ws0)):
                tr.unid[w0] = _Unid(born=ref, place=place_of(ws0), size=size_of(ws0), freed=True)
        u = tr.unid.pop(wid, None)
        ws = by_id[wid]
        s.world, s.status = wid, "on"
        s.touched = ref.g
        if u is not None:
            s.world_t = u.born.t
        else:  # a column is unidentified or linked
            assert y is not None
            s.world_t = y.world_t
        s.carried_away = s.dropped = s.emptied = False
        s.place = place_of(ws)
        if ws.detected:
            s.seen_t = ref.t
        tr.link[wid] = s
        s.question = None
        s.by_look = True
        s.at_k, s.at = tr.k, s.place  # its place in the last snapshot: where the look found it
        tr.relinked = True
        self._set_latest(s, look)
        self._fact(
            "identified",
            tr,
            s,
            ref,
            out,
            why="found again by its look" if was_lost else "recognised by its look",
            code="look_found" if was_lost else "look_moved",
            from_world=w0,
            score=best.score,
            margin=best.margin,
            bar=best.bar,
        )

    # ---- the open account (4.6.8) ----
    def _open_account(self, tr: _Tray, w: WorldLayer) -> list[TrayOpen]:
        run = tr.last_run
        assert run is not None  # the leaving's look work ran
        rows = {i for i, _ in run.rows}
        sure_world = {s.world for s in tr.stones if s.status == "on" and s.sure}
        free = [
            ws
            for ws in w.stones
            if ws.mode not in _HELD and inside_board(tr.board, place_of(ws)) and ws.id not in sure_world
        ]
        stone_on = {s.world: s.id for s in tr.stones if s.status == "on"}
        entries: list[TrayOpen] = []
        for s in tr.stones:
            if s.added_now:
                o = self._open_added(tr, s, run)
            elif s.left_now:
                o = self._open_stone(tr, s, "removed", run, rows, free, stone_on, w)
            elif s.status == "on" and not s.sure:
                o = self._open_stone(tr, s, "unsure", run, rows, free, stone_on, w)
            else:
                continue
            s.open = o
            entries.append(o)
        return entries

    def _open_stone(
        self,
        tr: _Tray,
        s: _Stone,
        kind: str,
        run: _Run,
        rows: set[int],
        free: list[StoneView],
        stone_on: dict[int | None, str],
        w: WorldLayer,
    ) -> TrayOpen:
        i = s.index
        if kind == "unsure":
            cause = s.unsure_code or "unchecked"
        elif s.look_lost:
            cause = "look_lost"
        elif s.by_look:
            cause = "by_look"
        elif s.id in tr.twins:
            cause = "twins"
        elif tr.relinked:
            cause = "relinked"
        elif s.lost_k == tr.k:
            cause = "lost_late"
        elif any(ws.mode not in _HELD and covers(ws, s.place, MARGIN) for ws in w.stones):
            cause = "covered"
        elif self._rivals(tr, s):
            cause = "rivals"
        elif not self._hand_took(s):
            cause = "no_hand"
        else:
            cause = "unchecked"
        ps = run.pairs.get(i, [])
        score: int | None = None
        if not s.chain:
            look = "late" if s.late else "none"
        elif s.box:
            look = "box"
        elif i not in rows or not ps:
            look = "stale"
        else:
            ties = [
                sc
                for wid, sc, n in ps
                if sc is not None and n is not None and n >= N_ID and sc >= run.info[(i, wid)][0]
            ]
            if ties:
                look, score = "tie", max(ties)
            else:
                scored = [sc for _, sc, _ in ps if sc is not None]
                look, score = "low", (max(scored) if scored else None)
        scores = {wid: sc for wid, sc, _ in ps}
        cands: list[TrayCandidate] = []
        if s.status == "on":
            cands.append(TrayCandidate("world", s.world, s.id, scores.get(s.world) if s.world is not None else None))
        near = [
            ws
            for ws in free
            if ws.id != s.world and compatible(s.size, size_of(ws)) and dist(s.place, place_of(ws)) <= LEAVE_D
        ]
        ranked: list[tuple[float, int]] = sorted(
            (-float(sc), ws.id) for ws in near if (sc := scores.get(ws.id)) is not None
        )
        ranked += sorted((dist(s.place, place_of(ws)), ws.id) for ws in near if scores.get(ws.id) is None)
        for _, wid in ranked:
            cands.append(TrayCandidate("world", wid, stone_on.get(wid), scores.get(wid)))
        why = None
        if kind == "removed" and (s.carried_away or s.emptied or s.dropped):
            why = "carried_away" if s.carried_away else "emptied" if s.emptied else "dropped"
        else:
            since = s.lost_t if kind == "removed" else s.unsure_from_t
            if any(mt >= since and dist(mp, s.place) <= HAND_R for mt, mp in tr.marks):
                why = "hand"
        cands.append(TrayCandidate("gone", why=why))
        if kind == "removed":
            cands.append(TrayCandidate("unseen"))
        return TrayOpen(s.id, s.world, kind, cause, look, score, None, None, tuple(cands))

    def _open_added(self, tr: _Tray, s: _Stone, run: _Run) -> TrayOpen:
        u, wid = s.unid_rec, s.world
        # an unclear addition is linked, with its unidentified record
        assert u is not None
        assert wid is not None
        if u.freed:
            cause = "freed"
        elif u.resized:
            cause = "size_changed"
        elif tr.relinked:
            cause = "relinked"
        elif any(
            z is not s
            and not z.added_now
            and compatible(z.size, s.size)
            and (z.left_now or (z.status == "on" and not self._trusted(z)))
            for z in tr.stones
        ):
            cause = "doubtful"
        elif u.seen < 2:
            cause = "seen_once"
        else:
            cause = "unchecked"
        mine = [(i, sc, n) for i, ps in sorted(run.pairs.items()) for w2, sc, n in ps if w2 == wid]
        score: int | None = None
        if wid not in run.cols:
            look = "none"
        else:
            ties = [
                sc
                for i, sc, n in mine
                if sc is not None and n is not None and n >= N_ID and sc >= run.info[(i, wid)][0]
            ]
            if ties:
                look, score = "tie", max(ties)
            else:
                scored = [sc for _, sc, _ in mine if sc is not None]
                look, score = "new", (max(scored) if scored else None)
        hand_near = any(u.born.t - RENAME_T <= mt <= u.born.t and dist(mp, u.place) <= HAND_R for mt, mp in tr.marks)
        scores = {i: sc for i, sc, _ in mine}
        near = [
            z
            for z in tr.stones
            if z is not s
            and not z.added_now
            and (z.left_now or (z.status == "on" and not z.sure))
            and compatible(z.size, s.size)
            and dist(z.place, u.place) <= LEAVE_D
        ]
        ranked: list[tuple[float, int]] = sorted(
            (-float(sc), z.index) for z in near if (sc := scores.get(z.index)) is not None
        )
        ranked += sorted((dist(z.place, u.place), z.index) for z in near if scores.get(z.index) is None)
        cands = [TrayCandidate("stone", None, tr.stones[i].id, scores.get(i)) for _, i in ranked]
        cands.append(TrayCandidate("new"))
        return TrayOpen(s.id, wid, "added", cause, look, score, hand_near, wid in tr.ids_at_arrival, tuple(cands))

    @staticmethod
    def _observed_place(s: _Stone, by_id: dict[int, StoneView]) -> Place | None:
        ws = by_id.get(s.world) if s.world is not None else None
        return place_of(ws) if s.status == "on" and ws is not None and ws.detected and ws.mode not in _HELD else None

    def _finish_blinks(self, tr: _Tray) -> None:
        for wid, blink in tr.blinks.items():
            if wid in tr.link:
                self._lose(tr, tr.link.pop(wid), blink.first)
        tr.blinks.clear()

    def _close(self, out: list[TrayFact]) -> None:
        """The open tray leaves: its final account (section 4.6, Leaving), then it goes to `on_close`."""
        tr = self._tray
        if tr is None:
            return
        self._tray = None
        if tr.arrived is None or tr.last is None:  # it never arrived
            self._on_close(self._view(tr))
            return
        self._finish_blinks(tr)
        w, last = tr.last
        fallback = self._visit_fallback if not tr.worked and tr.calm_ref and tr.calm_ref.g < self._visit_start else None
        leave = tr.calm_ref if (tr.calm_ref is not None and not tr.worked) else last
        if fallback is not None:
            leave = fallback.point
        tr.leaving = leave
        by_id = {ws.id: ws for ws in w.stones}
        for s in tr.stones:  # its place at the leaving point, if it was seen there (4.7), before the last settle
            if fallback is not None:
                match = fallback.places.get(s.id)
                s.left_place = match[1] if match is not None and match[0] == s.world else None
            elif leave is tr.calm_ref:
                s.left_place = s.calm_place
            else:
                s.left_place = self._observed_place(s, by_id)
        # a stone first seen after the leaving point is not on the tray that leaves (the next tray's, as it comes in)
        tr.unid = {wid: u for wid, u in tr.unid.items() if wid in by_id and u.born.g <= leave.g}
        links = {s.id: s.world for s in tr.stones if s.status == "on"}
        point_world = fallback.world if fallback is not None else (tr.calm_world if leave is tr.calm_ref else w)
        assert point_world is not None
        places = {ws.id: place_of(ws) for ws in point_world.stones if ws.detected and ws.mode not in _HELD}
        tr.point = {ws.id: ws for ws in point_world.stones}
        self._settle(tr, w, leave, by_id, out, final=True)
        on = [s for s in tr.stones if s.status == "on"]
        for s in on:
            if s.world is not None and links.get(s.id) != s.world:
                s.left_place = places.get(s.world)
        for s in tr.stones:
            s.evidence = _stone_evidence(tr, s)
        self._fact(
            "leaving",
            tr,
            None,
            leave,
            out,
            stones=len(on),
            sure=sum(1 for s in on if s.sure),
            added=tr.certain_added,  # a stone added for certain stays counted when it is removed unclear later
            removed=sum(1 for s in tr.stones if s.status == "removed" and s.certain),
            unclear=tr.unclear,
            open=tuple(tr.open or ()),
            t_id=self._t_id(tr) if tr.took else None,
        )
        self._last_closed = tr
        self._on_close(self._view(tr))

    # ---- the unit's speckle answers (4.6.12) ----
    def speckle(
        self, sp: WorldSpeckle, here: TrayPoint, links_before: Callable[[int], dict[int, str]]
    ) -> TraySpeckle | None:
        """Read one of the unit's speckle answers, from the world of frame `here` (fed already): its tray and the tray
        stones in it, or `None` when it is no tray's. `links_before(g)` gives the tray links before frame `g` (the
        core keeps them for revisions). An open tray also keeps the answer: the tray rules use the newest one near its
        leaving point as it leaves (4.6.13)."""
        rg, ag = sp.ref.g, sp.at.g
        tr = self._tray
        if tr is None or tr.arrived is None or rg < tr.arrived.g - SPECKLE_NEAR:
            tr = self._last_closed
            if tr is None or tr.arrived is None or tr.leaving is None:
                return None
            if not tr.arrived.g - SPECKLE_NEAR <= rg <= tr.leaving.g:
                return None
        assert tr.arrived is not None
        closed = tr is not self._tray
        mine = {s.id for s in tr.stones}
        ref_links = tr.arrival_links if abs(rg - tr.arrived.g) <= SPECKLE_NEAR else links_before(rg)
        at_links = links_before(ag)
        ref_links = {wid: sid for wid, sid in ref_links.items() if sid in mine}
        at_links = {wid: sid for wid, sid in at_links.items() if sid in mine}
        by_ref: dict[int, int] = {}
        by_at: dict[int, int] = {}
        for c, r in sp.pairs:
            by_at[c], by_ref[r] = r, c

        def entry(sid: str, side: str, x: Any, other: dict[int, str], pair_world: int | None) -> StoneSpeckle:
            s, q = permille(x.score), permille(x.second)
            return StoneSpeckle(
                stone=sid,
                side=side,
                score=x.score,
                second=x.second,
                clean=s is not None and q is not None and s >= SPECKLE_CLEAN and s >= 2 * q,
                found=None if x.at is None else other.get(x.at),
                found_world=x.at,
                pair=None if pair_world is None else other.get(pair_world),
            )

        got: dict[str, StoneSpeckle] = {}
        for x in sp.refs:  # a stone as it was at the reference, checked at the later moment
            sid = ref_links.get(x.id)
            if sid is not None and sid not in got:
                got[sid] = entry(sid, "arrival", x, at_links, by_ref.get(x.id))
        for x in sp.stones:  # a stone at the later moment, checked at the reference
            sid = at_links.get(x.id)
            if sid is not None and sid not in got:
                got[sid] = entry(sid, "later", x, ref_links, by_at.get(x.id))
        if not closed:
            tr.answers.append(sp)
            for st in tr.stones:
                if st.id in got:
                    st.speckle = got[st.id]
        return TraySpeckle(
            tray=tr.n,
            session=self._session,
            video=self._video,
            g=here.g,
            t=here.t,
            wall=here.wall,
            unit_tray=sp.tray,
            ref=TrayPoint(rg, sp.ref.t),
            at=TrayPoint(ag, sp.at.t),
            device=sp.device,
            gpu_ms=sp.gpu_ms,
            wall_ms=sp.wall_ms,
            stones=tuple(got[s.id] for s in tr.stones if s.id in got),
        )


def _speckle_clean(x: Any) -> bool:
    """A clean find (4.6.12): p(score) >= SPECKLE_CLEAN and >= twice p(second)."""
    p, q = permille(x.score), permille(x.second)
    return p is not None and q is not None and p >= SPECKLE_CLEAN and p >= 2 * q


def _speckle_find(sp: WorldSpeckle | None, side: str, wid: int | None) -> str:
    """What the leaving's answer found of one world id (4.6.13): `paired`, `clean`, `alike`, `none` or `absent`."""
    if sp is None or wid is None:
        return "absent"
    finds = sp.refs if side == "arrival" else sp.stones
    x = next((x for x in finds if x.id == wid), None)
    if x is None or x.score is None:
        return "absent"
    if any((r if side == "arrival" else c) == wid for c, r in sp.pairs):
        return "paired"
    if _speckle_clean(x):
        return "clean"
    p = permille(x.score)
    return "alike" if p is not None and p >= SPECKLE_CLEAN else "none"


_EVIDENCE = {"none": "not_found", "alike": "undecided", "absent": "undecided", "clean": "found", "paired": "found"}


def _stone_evidence(tr: _Tray, s: _Stone) -> str:
    """`TrayStone.evidence` (4.6.13): a stone the tray arrived with by its arrival world id's find, one added and on
    the tray by its world id's; an added stone removed again is `undecided`."""
    sp = tr.leaving_answer
    if not s.added:
        return _EVIDENCE[_speckle_find(sp, "arrival", s.first_world)]
    if s.status != "on":
        return "undecided"
    return _EVIDENCE[_speckle_find(sp, "later", s.world)]


def describe_open_txt(facts: Iterable[TrayFact]) -> str:
    """The golden `open.txt` form (4.6.9): every `leaving` fact's describe() line (with ` (bar 0.xxx)` when the tray
    took a look), then its open entries indented two spaces, `\\n` after every line."""
    lines: list[str] = []
    for x in facts:
        if x.kind == "leaving":
            lines.append(x.describe() + ("" if x.t_id is None else f" (bar {text.milli(x.t_id)})"))
            lines += ["  " + o.describe() for o in x.open or ()]
    return "".join(line + "\n" for line in lines)

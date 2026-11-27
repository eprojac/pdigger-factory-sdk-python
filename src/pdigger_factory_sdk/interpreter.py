"""High level: the Interpreter turns frames into state, typed events, activities and text (SDK-DESIGN.md 2.4, 4).

`InterpreterCore` is a pure synchronous state machine: `feed(frame) -> Update`, `finish() -> [Activity]`. Every rule
lives there and is golden-tested; `Interpreter` feeds it from the client's frames stream and `Replay` from a recording.

    async with FactoryClient("http://factory-orin.local:8120") as factory:
        async for event in factory.interpreter().events():
            print(event.describe())          # 21:17.8 Right hand lifts stone 2660
"""

from __future__ import annotations

import asyncio
import bisect
import dataclasses
import math
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Literal

from . import text
from ._stream import AsyncStream
from .api import Layers
from .errors import FactoryError
from .models import CardsLayer, Frame, Rect, State, StoneView, WorldEvent, WorldHands, WorldLayer, WorldStats
from .streams import ClockItem, FrameItem, GapItem, ResetItem, Streams
from .text import EVENT_TYPES, HAND_STATES, round3
from .trays import Tray, TrayCounts, TrayFact, TrayLedger, TrayPoint, TrayReport, TraySpeckle

__all__ = [
    "BURST_GAP",
    "TRAYS_KEPT",
    "Activity",
    "Event",
    "EventKey",
    "FrameRef",
    "HandView",
    "Hands",
    "Interpreter",
    "InterpreterCore",
    "InterpreterState",
    "Move",
    "MoveCounts",
    "PushBurst",
    "PushCounts",
    "StoneCounts",
    "Summary",
    "TrackPoint",
    "Update",
]

BURST_GAP = 1.0
"""Seconds between two pushes of one hand that still belong to the same push burst."""
TRACK_POINTS = 512
"""Points of a stone's track the core keeps (the server keeps 512 per path)."""
TRACK_STONES = 4096
"""Stones whose tracks the core keeps (the most recently moved ones)."""
GRID_SECONDS = 0.2
"""Media seconds of one grid frame of a recorded video (5 per second)."""
GRID_MS = 200
"""Milliseconds of one grid frame of a recorded video: the step of a session's first frame (SDK-DESIGN.md 4.5)."""
TRAYS_KEPT = 256
"""Trays that have left that `InterpreterCore.trays()` keeps (the newest; `Replay.trays()` keeps every one)."""
SPECKLE_KEPT = 1024
"""The unit's speckle answers that `InterpreterCore.speckle()` keeps (the newest)."""

EventKey = tuple[str, int | None, str | None, tuple[int, ...] | None]
"""Events use (type, id, side, into). Sheet events use ('sheet', None, action, sorted ids)."""

_BURST_SIDES = {"left": "left", "right": "right", "both": "both"}
_OUTCOMES = {"place": "placed", "drop": "dropped", "gone": "carried_away", "delete": "removed"}
_HAND_CLOSERS = frozenset({"place", "drop", "gone"})


@dataclass(slots=True, frozen=True)
class TrackPoint:
    """A point of a stone's track: where its solid was at a grid frame (scene x, y, z, in template gauge units)."""

    g: int
    t: float
    pos: tuple[float, float, float]


@dataclass(slots=True, frozen=True)
class FrameRef:
    """A point of an activity: grid frame, media time and server wall time (`None` before a clock state is known)."""

    g: int
    t: float
    wall: float | None = None

    @property
    def clock(self) -> str:
        """The media time as text (`1:49.3`)."""
        return text.clock(self.t)

    def to_dict(self) -> dict[str, Any]:
        return {"g": self.g, "t": self.t, "wall": self.wall}


@dataclass(slots=True, kw_only=True)
class Event:
    """Something the world says happened in a frame: a stone appears, a hand lifts it, carries it, puts it down...

    `type` is the wire name (`new`, `confirm`, `freeze`, `push`, `lift`, `hinge`, `carry`, `place`, `drop`, `gone`,
    `delete`, `split`, or a newer one), `hand` is the side (`left`, `right`, `both` or `None`), `stone` the stone's
    snapshot in this frame's world (`None` when it is not in it, for example after a delete).

    `stone_id` is the world's id, which the engine's tracker can rename and reuse; `tray_stone` is the SDK's own id of
    the stone it stood for on the open tray before this frame (`None` when it stood for none): see `trays`.
    """

    type: str
    g: int
    t: float
    wall: float | None
    session: int
    video: str
    stone_id: int | None = None
    hand: str | None = None
    why: str | None = None
    into: list[int] | None = None
    ids: list[int] | None = None
    source: str | None = None
    tray_stone: str | None = None
    stone: StoneView | None = field(default=None, compare=False, repr=False)

    def describe(self) -> str:
        """The event as one human-readable line: `21:17.8 Right hand lifts stone 2660`."""
        return text.describe_event(self)

    @property
    def clock(self) -> str:
        """The media time as text."""
        return text.clock(self.t)

    @property
    def key(self) -> EventKey:
        return _key(self.type, self.stone_id, self.hand, self.into, self.why, self.ids)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready dict (the stone snapshot is left out)."""
        return {
            "type": self.type,
            "g": self.g,
            "t": self.t,
            "wall": self.wall,
            "session": self.session,
            "video": self.video,
            "stone_id": self.stone_id,
            "hand": self.hand,
            "why": self.why,
            "into": self.into,
            **({"ids": list(self.ids)} if self.ids is not None else {}),
            "source": self.source,
            "tray_stone": self.tray_stone,
        }


@dataclass(slots=True)
class Move:
    """One stone taken and put somewhere (an activity).

    `outcome` is `placed`, `dropped`, `carried_away`, `removed` or `interrupted` once the move has ended (`None`
    while it is open); `from_` and `to` are the stone's `solid.pos` at the start and at a put-down, `distance` their
    distance in x, y (scene units, the template gauge, not metres). `partial` moves were joined in the middle.
    """

    kind: ClassVar[Literal["move"]] = "move"
    stone_id: int | None
    hand: str | None
    start: FrameRef
    session: int
    video: str
    end: FrameRef | None = None
    from_: list[float] | None = None
    to: list[float] | None = None
    distance: float | None = None
    carried: bool = False
    tipped: bool = False
    partial: bool = False
    outcome: str | None = None
    duration: float | None = None

    def describe(self) -> str:
        """The move as one line: `1:30.3 Right hand tips stone 94 and sets it down (0.2 s)`."""
        return text.describe_move(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "stone_id": self.stone_id,
            "hand": self.hand,
            "start": self.start.to_dict(),
            "end": None if self.end is None else self.end.to_dict(),
            "duration": self.duration,
            "outcome": self.outcome,
            "from": self.from_,
            "to": self.to,
            "distance": self.distance,
            "carried": self.carried,
            "tipped": self.tipped,
            "partial": self.partial,
            "session": self.session,
            "video": self.video,
        }


@dataclass(slots=True)
class PushBurst:
    """A hand pushing stones about (an activity): `pushes` push events of one side, at most 1.0 s apart."""

    kind: ClassVar[Literal["push"]] = "push"
    hand: str
    start: FrameRef
    last: FrameRef
    session: int
    video: str
    stones: list[int] = field(default_factory=list)
    """The stones pushed, unique, ascending."""
    pushes: int = 0
    end: FrameRef | None = None
    duration: float | None = None

    def describe(self) -> str:
        """The burst as one line: `0:20.5 Both hands push stones 86, 90 and 94 (6 pushes in 0.6 s)`."""
        return text.describe_push(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "hand": self.hand,
            "start": self.start.to_dict(),
            "end": None if self.end is None else self.end.to_dict(),
            "duration": self.duration,
            "stones": list(self.stones),
            "pushes": self.pushes,
            "session": self.session,
            "video": self.video,
        }


Activity = Move | PushBurst
"""A move or a push burst."""


@dataclass(slots=True, frozen=True)
class HandView:
    """One hand: its world state (`free`, `touch`, `grasp`, `hinge`, `carry`), the stone it holds, and the title of
    its hand card (`Holding a stone`) when the frames carry the cards layer."""

    state: str | None = None
    stone: int | None = None
    card: str | None = None


@dataclass(slots=True, frozen=True)
class Hands:
    left: HandView = HandView()
    right: HandView = HandView()


@dataclass(slots=True, frozen=True, kw_only=True)
class InterpreterState:
    """The interpreter's view after a frame: the newest world's stones, hands, board and stats, open activities."""

    session: int | None = None
    video: str | None = None
    g: int | None = None
    t: float | None = None
    wall: float | None = None
    """Server wall time of `t`, from the newest clock state (`None` until one is known)."""
    stones: Mapping[int, StoneView] = field(default_factory=dict)
    hands: Hands = Hands()
    board: Rect | None = None
    stats: WorldStats | None = None
    open_moves: Mapping[int | None, Move] = field(default_factory=dict)
    """Moves not ended yet, by stone id (copies)."""
    open_bursts: Mapping[str, PushBurst] = field(default_factory=dict)
    """Push bursts not ended yet, by side (copies)."""
    tray: Tray | None = None
    """The open tray with its tray stones (`None` between trays)."""
    frames: int = 0
    revisions: int = 0
    gaps: int = 0


@dataclass(slots=True, frozen=True)
class Update:
    """What one frame changed: its new events, the activities it completed, the tray facts it made, the state after
    it."""

    frame: Frame | None
    """The frame (`None` only for the final update of a replay or an ended stream, which carries `finish()`)."""
    events: list[Event]
    activities: list[Activity]
    state: InterpreterState
    is_revision: bool = False
    """A revised world of a frame already seen (a paused frame refined): only its new events are here."""
    dropped: bool = False
    """A duplicate of a frame already seen: nothing changed (the high-level iterators skip these)."""
    final: bool = False
    """The end of a replay or of a stream: the activities that were still open, closed as `interrupted`."""
    before: int = 0
    """How many of `activities` were closed before this frame's events (a session change, stale push bursts)."""
    tray_facts: list[TrayFact] = field(default_factory=list)
    """The tray facts this frame made (after a session change, first those of the old session's tray leaving); the
    final update has those of the open tray leaving."""
    speckle: list[TraySpeckle] = field(default_factory=list)
    """The unit's speckle answer this frame's world carried, read as a tray's (SDK-DESIGN.md 4.6.12): at most one."""

    def lines(self) -> list[str]:
        """Everything as text in output order: activities closed before the events, the events, the other activities,
        the tray facts."""
        acts = [a.describe() for a in self.activities]
        facts = [f.describe() for f in self.tray_facts]
        return acts[: self.before] + [e.describe() for e in self.events] + acts[self.before :] + facts


@dataclass(slots=True)
class MoveCounts:
    total: int = 0
    placed: int = 0
    dropped: int = 0
    carried_away: int = 0
    removed: int = 0
    interrupted: int = 0
    partial: int = 0
    carried: int = 0
    mean_duration: float | None = None
    mean_distance: float | None = None


@dataclass(slots=True)
class PushCounts:
    total: int = 0
    pushes: int = 0
    left: int = 0
    right: int = 0
    both: int = 0


@dataclass(slots=True)
class StoneCounts:
    last: int | None = None
    max: int = 0
    confirmed_last: int | None = None


def _event_counts() -> dict[str, int]:
    return dict.fromkeys((*EVENT_TYPES, "other"), 0)


def _hand_step(prev: tuple[int, float, int] | None, g: int, t: float) -> int:
    """The milliseconds a new frame adds to its hands' states (SDK-DESIGN.md 4.5): the media time since the session's
    previous new frame `(g_prev, t_prev, step_prev)` when that frame is the grid step before it (`g == g_prev + 1`), in
    whole milliseconds, half up, when that is at least 1 ms and below 2^31 ms; otherwise the session's previous step (a
    frame after a gap, a `t` not after the previous one or not a finite number), and 200 (one grid frame of a recorded
    video) for a session's first frame."""
    if prev is None:
        return GRID_MS
    pg, pt, step = prev
    if g == pg + 1:
        try:
            x = (t - pt) * 1000 + 0.5
        except OverflowError:  # a time no double holds: json reads a long whole number as an int (SDK-DESIGN.md 2.3)
            return step
        if 1 <= x < 2**31:
            return math.floor(x)
    return step


def _hand_seconds() -> dict[str, dict[str, float]]:
    return {side: dict.fromkeys(HAND_STATES, 0.0) for side in ("left", "right")}


@dataclass(slots=True, kw_only=True)
class Summary:
    """Counts of a replay or of a stream so far (section 4.4); `to_dict()` is the golden JSON form, `describe()` its
    text. Times are seconds, rounded to 3 decimals; a hand time adds up each frame with a world in that state by its
    step (SDK-DESIGN.md 4.5: the media time since the session's previous frame when that frame is the grid step before
    it, else the previous step; 0.2 s on a recorded video, about 0.3 s on a plant camera)."""

    frames: int = 0
    frames_with_world: int = 0
    missing_world: int = 0
    revisions: int = 0
    sessions: int = 0
    first_g: int | None = None
    last_g: int | None = None
    first_t: float | None = None
    last_t: float | None = None
    media_seconds: float = 0.0
    gaps: int = 0
    events: dict[str, int] = field(default_factory=_event_counts)
    moves: MoveCounts = field(default_factory=MoveCounts)
    push_bursts: PushCounts = field(default_factory=PushCounts)
    stones: StoneCounts = field(default_factory=StoneCounts)
    trays: TrayCounts = field(default_factory=TrayCounts)
    hands: dict[str, dict[str, float]] = field(default_factory=_hand_seconds)

    def to_dict(self) -> dict[str, Any]:
        """The JSON form, with exactly the keys of section 4.4."""
        m, p, s = self.moves, self.push_bursts, self.stones
        return {
            "frames": self.frames,
            "frames_with_world": self.frames_with_world,
            "missing_world": self.missing_world,
            "revisions": self.revisions,
            "sessions": self.sessions,
            "first_g": self.first_g,
            "last_g": self.last_g,
            "first_t": self.first_t,
            "last_t": self.last_t,
            "media_seconds": self.media_seconds,
            "gaps": self.gaps,
            "events": {k: v for k, v in self.events.items() if k != "sheet" or v},
            "moves": {
                "total": m.total,
                "placed": m.placed,
                "dropped": m.dropped,
                "carried_away": m.carried_away,
                "removed": m.removed,
                "interrupted": m.interrupted,
                "partial": m.partial,
                "carried": m.carried,
                "mean_duration": m.mean_duration,
                "mean_distance": m.mean_distance,
            },
            "push_bursts": {"total": p.total, "pushes": p.pushes, "left": p.left, "right": p.right, "both": p.both},
            "stones": {"last": s.last, "max": s.max, "confirmed_last": s.confirmed_last},
            "trays": self.trays.to_dict(),
            "hands": {side: dict(states) for side, states in self.hands.items()},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Summary:
        """A summary from its JSON form (for example a published `<video>.summary.json`; one made before trays were
        counted has no `trays`, read as zeros)."""
        return cls(
            frames=d["frames"],
            frames_with_world=d["frames_with_world"],
            missing_world=d["missing_world"],
            revisions=d["revisions"],
            sessions=d["sessions"],
            first_g=d["first_g"],
            last_g=d["last_g"],
            first_t=d["first_t"],
            last_t=d["last_t"],
            media_seconds=d["media_seconds"],
            gaps=d["gaps"],
            events={**_event_counts(), **d["events"]},
            moves=MoveCounts(**d["moves"]),
            push_bursts=PushCounts(**d["push_bursts"]),
            stones=StoneCounts(**d["stones"]),
            trays=TrayCounts(**d["trays"]) if "trays" in d else TrayCounts(),
            hands={side: dict(states) for side, states in d["hands"].items()},
        )

    def describe(self) -> str:
        """The summary as text, one `\\n`-ended line per topic."""
        return text.describe_summary(self.to_dict())


def _key(
    type: str, stone_id: int | None, side: str | None, into: list[int] | None, why: str | None, ids: list[int] | None
) -> EventKey:
    if type == "sheet":
        return (type, None, why, tuple(sorted(set(ids or ()))))
    return (type, stone_id, side, None if into is None else tuple(into))


def event_key(e: WorldEvent) -> EventKey:
    """The event's identity within a frame, including the action and stone ids for sheets."""
    return _key(e.type, e.id, e.side, e.into, e.why, e.ids)


def _sort_key(a: Activity) -> tuple[int, int, int, int, str]:
    end = a.end or a.start
    if isinstance(a, Move):
        first = -1 if a.stone_id is None else a.stone_id
        return (end.g, a.start.g, 0, first, a.hand or "")
    return (end.g, a.start.g, 1, a.stones[0] if a.stones else -1, a.hand)


def _type_is(wanted: str) -> Callable[[Event], bool]:
    def predicate(e: Event) -> bool:
        return e.type == wanted

    return predicate


def _merge_hand(m: Move, side: str | None) -> None:
    if side is None:
        return
    if m.hand is None:
        m.hand = side
    elif m.hand != side:
        m.hand = "both"


class InterpreterCore:
    """The interpreter's rules as a pure state machine: no I/O, no clock, golden-tested.

    Feed it frames in the order received (`feed`), clock states as they come (`clock`), the stream's `gap` and `reset`
    events as they come (`gap`), and call `end()` (or `finish()`) at the end to close what is still open. `summary()`
    gives the counts so far, `trays()` the trays.

    Args:
        revision_window: Grid frames of each session kept for revisions and duplicates (the server retains 512).
        trays_kept: Trays that have left that `trays()` keeps, the newest (`None`: every one).
        tray_bounds: Fixed inspection rectangle in native image pixels (left, top, right, bottom).
            While waiting for a tray to settle, retiring an unseen track wholly outside this area
            leaves its inventory undisturbed. All world events are still delivered.
    """

    def __init__(
        self,
        *,
        revision_window: int = 4096,
        trays_kept: int | None = TRAYS_KEPT,
        tray_bounds: tuple[float, float, float, float] | None = None,
    ) -> None:
        if tray_bounds is not None and not (
            len(tray_bounds) == 4
            and all(math.isfinite(v) for v in tray_bounds)
            and tray_bounds[0] < tray_bounds[2]
            and tray_bounds[1] < tray_bounds[3]
        ):
            raise ValueError("tray_bounds must be a finite rectangle with positive width and height")
        self._tray_bounds = tray_bounds
        self._window = revision_window
        self._session: int | None = None
        self._video: str | None = None
        self._g: int | None = None
        self._t: float | None = None
        self._prev_g: int | None = None
        self._stones: dict[int, StoneView] = {}
        self._hands: WorldHands | None = None
        self._cards: CardsLayer | None = None
        self._board: Rect | None = None
        self._stats: WorldStats | None = None
        self._moves: dict[int | None, Move] = {}
        self._bursts: dict[str, PushBurst] = {}
        self._emitted: dict[int, set[EventKey]] = {}
        self._revs: dict[int, int] = {}
        self._links: dict[int, dict[int, str]] = {}  # g -> the tray links before that frame, for its revisions
        self._last_links: dict[int, str] | None = None
        self._last_world_ref: FrameRef | None = None
        self._clocks: dict[int, State] = {}  # session -> its newest clock state (the last 8 sessions)
        self._s = Summary()
        self._session_first_t = 0.0
        self._session_last_t = 0.0
        self._media = 0.0
        self._dur_sum = 0.0
        self._dur_n = 0
        self._dist_sum = 0.0
        self._dist_n = 0
        self._hand_ms = {side: dict.fromkeys(HAND_STATES, 0) for side in ("left", "right")}
        self._step_prev: tuple[int, float, int] | None = None  # (g, t, step) of the session's previous new frame
        self._tracks: dict[int, list[TrackPoint]] = {}  # the most recently moved stone last
        self._trays: deque[Tray] = deque(maxlen=trays_kept)  # trays that have left, oldest first
        self._speckle: deque[TraySpeckle] = deque(maxlen=SPECKLE_KEPT)  # the unit's speckle answers, oldest first
        self._ledger = self._new_ledger(0, "")

    # ---- inputs ----------------------------------------------------------------------------------------------------
    def clock(self, state: State) -> None:
        """A shared clock state (a `clock` event, or `GET /session`): wall times of frames of its session come from the
        newest state of that session, so frames of a session that ended still get theirs after the next session's
        clock has come. A state with a lower `revision` than the one kept for its session is older, and is ignored;
        the states of the last 8 sessions are kept."""
        old = self._clocks.get(state.session)
        if old is not None and state.revision < old.revision:
            return
        self._clocks.pop(state.session, None)
        self._clocks[state.session] = state
        while len(self._clocks) > 8:
            del self._clocks[next(iter(self._clocks))]

    def gap(self) -> None:
        """The stream dropped frames here (a `gap` or a `reset` event): the tray rules count the next frame as one after
        frames they could not read, as after a frame without a world, a jump in `g` or a revision (SDK-DESIGN.md
        4.6.1, presence questions)."""
        self._ledger.skipped()

    def feed(self, frame: Frame) -> Update:
        """One frame, in the order received. Returns its new events and completed activities, and the state."""
        acts: list[Activity] = []
        facts: list[TrayFact] = []
        before = 0
        w = frame.world
        g, t = frame.g, frame.t
        if frame.session != self._session:
            self._close_session(acts)
            facts = self._ledger.close()
            self._ledger = self._new_ledger(frame.session, frame.video)
            self._session, self._video = frame.session, frame.video
            self._stones, self._hands, self._cards, self._board, self._stats = {}, None, None, None, None
            self._emitted, self._revs, self._tracks = {}, {}, {}
            self._links, self._last_links = {}, None
            self._last_world_ref = None
            self._s.sessions += 1
            self._session_first_t = t
            self._prev_g = None
            self._step_prev = None
            before = len(acts)
        elif self._prev_g is not None and g <= self._prev_g:
            rev = w.rev if w is not None else 0
            if w is not None and g in self._revs and rev > self._revs[g]:
                return self._revise(frame, w, rev, self._prev_g)
            return Update(frame=frame, events=[], activities=[], state=self.state, dropped=True)
        s = self._s
        s.frames += 1
        if s.first_g is None:
            s.first_g, s.first_t = g, t
        s.last_g, s.last_t = g, t
        if self._prev_g is not None and g > self._prev_g + 1:
            s.gaps += 1
            self._ledger.skipped()
        self._prev_g = g
        step = _hand_step(self._step_prev, g, t)
        self._step_prev = (g, t, step)
        self._session_last_t = t
        self._g, self._t = g, t
        if frame.cards is not None:
            self._cards = frame.cards
        # the tray links before this frame: its events', later its revisions', and the speckle answers', which also
        # read those of a frame without a world (4.6.12; one copy while they do not change)
        links = self._ledger.links()
        if self._last_links is not None and links == self._last_links:
            links = self._last_links
        self._links[g] = self._last_links = links
        if w is None:
            s.missing_world += 1
            self._ledger.skipped()
            return Update(frame=frame, events=[], activities=acts, state=self.state, before=before, tray_facts=facts)
        s.frames_with_world += 1
        self._revs[g] = w.rev
        self._prune(g)
        stale = [side for side, b in self._bursts.items() if t - b.last.t > BURST_GAP]
        closed: list[Activity] = [self._end_burst(side) for side in stale]
        before += len(closed)
        keys = self._emitted.setdefault(g, set())
        events: list[Event] = []
        for e in w.events:
            keys.add(event_key(e))
            events.append(self._event(frame, e, w, links))
        self._activities(frame, w, events, closed)
        self._emit(closed, acts)
        self._take_state(w)
        self._record_tracks(g, t, w)
        ref = self._last_world_ref = self._ref(frame)
        n = len(w.stones)
        s.stones.last = n
        s.stones.max = max(s.stones.max, n)
        s.stones.confirmed_last = w.stats.confirmed
        for side, hand in (("left", w.hands.left), ("right", w.hands.right)):
            if hand is not None and hand.state in HAND_STATES:
                self._hand_ms[side][hand.state] += step
        here = TrayPoint(g=ref.g, t=ref.t, wall=ref.wall)
        facts += self._ledger.feed(w, here)
        answers: list[TraySpeckle] = []
        if w.speckle is not None:  # read after the trays took the frame (4.6.12)
            answer = self._ledger.speckle(w.speckle, here, lambda k: self._links.get(k, {}))
            if answer is not None:
                answers.append(answer)
                self._speckle.append(answer)
        return Update(
            frame=frame,
            events=events,
            activities=acts,
            state=self.state,
            before=before,
            tray_facts=facts,
            speckle=answers,
        )

    def end(self) -> Update:
        """End of the replay or stream, as the final update (`frame=None`, `final=True`): open moves close as
        `interrupted`, open push bursts close, and the open tray leaves (its facts are the update's `tray_facts`)."""
        acts: list[Activity] = []
        self._close_session(acts)
        facts = self._ledger.close()
        self._ledger = self._new_ledger(0, "")
        self._session = None
        return Update(frame=None, events=[], activities=acts, state=self.state, final=True, tray_facts=facts)

    def finish(self) -> list[Activity]:
        """`end()`'s activities: open moves close as `interrupted`, open push bursts close, the open tray leaves (its
        facts are in `trays()`)."""
        return self.end().activities

    # ---- outputs ---------------------------------------------------------------------------------------------------
    @property
    def state(self) -> InterpreterState:
        """The state after the newest frame."""
        hands = self._hands
        cards = self._cards

        def view(hand: Any, card: Any) -> HandView:
            return HandView(
                state=None if hand is None else hand.state,
                stone=None if hand is None else hand.stone,
                card=None if card is None else card.title,
            )

        return InterpreterState(
            session=self._session,
            video=self._video,
            g=self._g,
            t=self._t,
            wall=None if self._session is None or self._t is None else self._wall(self._session, self._t),
            stones=self._stones,
            hands=Hands(
                left=view(hands and hands.left, cards and cards.left),
                right=view(hands and hands.right, cards and cards.right),
            ),
            board=self._board,
            stats=self._stats,
            open_moves={k: replace(m) for k, m in self._moves.items()},
            open_bursts={k: replace(b, stones=list(b.stones)) for k, b in self._bursts.items()},
            tray=self._ledger.current(),
            frames=self._s.frames,
            revisions=self._s.revisions,
            gaps=self._s.gaps,
        )

    def summary(self) -> Summary:
        """The counts so far (after `finish()`, the counts of the whole replay or stream)."""
        s = self._s
        media = self._media + (self._session_last_t - self._session_first_t if self._session is not None else 0.0)
        out = replace(
            s,
            events=dict(s.events),
            moves=replace(
                s.moves,
                mean_duration=round3(self._dur_sum / self._dur_n) if self._dur_n else None,
                mean_distance=round3(self._dist_sum / self._dist_n) if self._dist_n else None,
            ),
            push_bursts=replace(s.push_bursts),
            stones=replace(s.stones),
            trays=replace(s.trays),
            hands={side: {k: round3(ms / 1000) for k, ms in states.items()} for side, states in self._hand_ms.items()},
            media_seconds=round3(media),
            first_t=None if s.first_t is None else round3(s.first_t),
            last_t=None if s.last_t is None else round3(s.last_t),
        )
        return out

    def trays(self) -> list[Tray]:
        """The trays so far, oldest first: those that have left (the newest `trays_kept`, across sessions) and the open
        one. Each has its `arrived` and `leaving` points, its tray stones and its facts (section 4.6)."""
        open_tray = self._ledger.current()
        return [*self._trays, *([] if open_tray is None else [open_tray])]

    def speckle(self) -> list[TraySpeckle]:
        """The unit's speckle answers read so far, oldest first (the newest 1024, across sessions), each with its tray
        and the tray stones in it (SDK-DESIGN.md 4.6.12). An answer that comes after its tray left is here, not in the
        tray."""
        return list(self._speckle)

    # ---- rules -----------------------------------------------------------------------------------------------------
    def _new_ledger(self, session: int, video: str) -> TrayLedger:
        return TrayLedger(self._s.trays, session, video, self._trays.append, self._tray_bounds)

    def _wall(self, session: int, t: float) -> float | None:
        # as SessionClock.wall_of: the playhead runs from the end of the hold while playing, from the pause while paused
        c = self._clocks.get(session)
        if c is None or not c.rate > 0:
            return None
        start = max(c.anchor_wall, c.hold_until) if c.playing else c.anchor_wall
        return start + (t - c.anchor_media) / c.rate

    def _ref(self, frame: Frame) -> FrameRef:
        return FrameRef(g=frame.g, t=frame.t, wall=self._wall(frame.session, frame.t))

    def _prune(self, g: int) -> None:
        if len(self._revs) > 2 * self._window:
            low = g - self._window
            self._revs = {k: v for k, v in self._revs.items() if k >= low}
            self._emitted = {k: v for k, v in self._emitted.items() if k >= low}
            self._links = {k: v for k, v in self._links.items() if k >= low}

    def _revise(self, frame: Frame, w: WorldLayer, rev: int, newest_g: int) -> Update:
        g = frame.g
        self._s.revisions += 1
        self._revs[g] = rev
        keys = self._emitted.setdefault(g, set())
        links = self._links.get(g, {})  # a revised event's tray stone is the one before its own frame
        events: list[Event] = []
        for e in w.events:
            k = event_key(e)
            if k in keys:
                continue
            keys.add(k)
            events.append(self._event(frame, e, w, links))
        closed: list[Activity] = []
        self._activities(frame, w, events, closed)
        acts: list[Activity] = []
        self._emit(closed, acts)
        if g >= newest_g:
            self._take_state(w)
        self._ledger.skipped()  # a frame again, which the tray rules never read: it may hold a hand at work (4.6.1)
        return Update(frame=frame, events=events, activities=acts, state=self.state, is_revision=True)

    def _event(self, frame: Frame, e: WorldEvent, w: WorldLayer, links: dict[int, str]) -> Event:
        stone = next((s for s in w.stones if s.id == e.id), None)
        self._s.events[e.type if e.type in EVENT_TYPES else "other"] += 1
        return Event(
            type=e.type,
            g=frame.g,
            t=frame.t,
            wall=self._wall(frame.session, frame.t),
            session=frame.session,
            video=frame.video,
            stone_id=e.id,
            hand=e.side,
            why=e.why,
            into=e.into,
            ids=e.ids,
            source=e.source,
            tray_stone=None if e.id is None else links.get(e.id),
            stone=stone,
        )

    def _activities(self, frame: Frame, w: WorldLayer, events: list[Event], closed: list[Activity]) -> None:
        ref = self._ref(frame)
        by_id = {s.id: s for s in w.stones}

        def pos(sid: int | None) -> list[float] | None:
            stone = by_id.get(sid) if sid is not None else None
            # a pos with fewer than 2 numbers is as absent (SDK-DESIGN.md 4.2)
            return None if stone is None or len(stone.solid.pos) < 2 else list(stone.solid.pos)

        hand_closed = {ev.stone_id for ev in events if ev.type in _HAND_CLOSERS}
        for ev in events:
            typ, sid, side = ev.type, ev.stone_id, ev.hand
            if typ == "delete" and sid in hand_closed:
                continue  # the same frame has the hand's put-down or carry-away of this stone: that decides
            if typ == "push":
                burst_side = _BURST_SIDES.get(side or "")
                if burst_side is not None and not ev.why:
                    self._push(burst_side, sid, ref, frame)
                continue
            m = self._moves.get(sid)
            if typ in ("lift", "hinge"):
                if m is None:
                    self._moves[sid] = Move(
                        stone_id=sid,
                        hand=side,
                        start=ref,
                        session=frame.session,
                        video=frame.video,
                        from_=pos(sid),
                        tipped=typ == "hinge",
                    )
                else:
                    m.tipped = m.tipped or typ == "hinge"
                    _merge_hand(m, side)
                continue
            if typ == "carry":
                if m is not None:
                    m.carried = True
                    _merge_hand(m, side)
                continue
            outcome = _OUTCOMES.get(typ)
            if outcome is None:
                continue
            if m is None:
                if typ == "delete":
                    continue
                m = Move(stone_id=sid, hand=side, start=ref, session=frame.session, video=frame.video, partial=True)
            else:
                del self._moves[sid]
                _merge_hand(m, side)
            m.outcome = outcome
            m.end = ref
            m.duration = ref.t - m.start.t
            m.to = pos(sid) if outcome in ("placed", "dropped") else None
            if m.from_ is not None and m.to is not None:
                dx, dy = m.to[0] - m.from_[0], m.to[1] - m.from_[1]
                m.distance = round3(math.sqrt(dx * dx + dy * dy))
            closed.append(m)

    def _push(self, side: str, sid: int | None, ref: FrameRef, frame: Frame) -> None:
        # A push joins its side's open burst: a burst more than BURST_GAP older than this frame was closed before the
        # frame's events (`feed`), and a revision is never newer than the newest frame.
        b = self._bursts.get(side)
        if b is None:
            b = PushBurst(hand=side, start=ref, last=ref, session=frame.session, video=frame.video)
            self._bursts[side] = b
        b.last = ref
        b.pushes += 1
        if sid is not None and sid not in b.stones:
            bisect.insort(b.stones, sid)

    def _end_burst(self, side: str) -> PushBurst:
        b = self._bursts.pop(side)
        b.end = b.last
        b.duration = b.end.t - b.start.t
        return b

    def _emit(self, closed: list[Activity], out: list[Activity]) -> None:
        closed.sort(key=_sort_key)
        for a in closed:
            self._count(a)
        out.extend(closed)

    def _count(self, a: Activity) -> None:
        s = self._s
        if isinstance(a, PushBurst):
            p = s.push_bursts
            p.total += 1
            p.pushes += a.pushes
            setattr(p, a.hand, getattr(p, a.hand) + 1)
            return
        m = s.moves
        m.total += 1
        setattr(m, a.outcome or "interrupted", getattr(m, a.outcome or "interrupted") + 1)
        m.partial += a.partial
        m.carried += a.carried
        if not a.partial and a.outcome != "interrupted":
            self._dur_sum += a.duration or 0.0
            self._dur_n += 1
        if a.distance is not None:
            self._dist_sum += a.distance
            self._dist_n += 1

    def _close_session(self, out: list[Activity]) -> None:
        if self._session is None:
            return
        closed: list[Activity] = []
        for m in self._moves.values():
            m.outcome = "interrupted"
            m.end = self._last_world_ref or m.start
            m.duration = m.end.t - m.start.t
            m.to = None
            m.distance = None
            closed.append(m)
        self._moves = {}
        closed.extend(self._end_burst(side) for side in list(self._bursts))
        self._emit(closed, out)
        self._media += self._session_last_t - self._session_first_t

    def track(self, stone_id: int) -> list[TrackPoint]:
        """The path of a stone's solid in this session, oldest first: a point for every frame fed where it moved, at
        most the newest 512, for the 4096 most recently moved stones; empty for a stone the core has no track of. The
        server's own track since the session began is `factory.api.get_stone`."""
        return list(self._tracks.get(stone_id, [])[-TRACK_POINTS:])

    def _record_tracks(self, g: int, t: float, w: WorldLayer) -> None:
        """Adds a point to the track of every stone whose solid moved since its last point."""
        for s in w.stones:
            pos = s.solid.pos
            if len(pos) < 3:
                continue
            point = TrackPoint(g=g, t=t, pos=(pos[0], pos[1], pos[2]))
            path = self._tracks.get(s.id)
            if path is not None and path[-1].pos == point.pos:
                continue  # did not move: keeps its place among the most recently moved
            path = self._tracks.pop(s.id, None) or []
            path.append(point)
            if len(path) >= 2 * TRACK_POINTS:
                del path[:TRACK_POINTS]
            self._tracks[s.id] = path
            if len(self._tracks) > TRACK_STONES:
                del self._tracks[next(iter(self._tracks))]

    def _take_state(self, w: WorldLayer) -> None:
        self._stones = {s.id: s for s in w.stones}
        self._hands = w.hands
        self._board = None if w.draw is None else w.draw.board
        self._stats = w.stats


class Interpreter:
    """The Interpreter on the shared session: follows the frames stream and feeds an `InterpreterCore`.

    Make one with `factory.interpreter()`. Iterate `updates()`, `events()`, `activities()` or `tray_facts()`, or
    `await wait_for(...)`,
    one at a time. A later loop goes on where the last one stopped and misses nothing in between: it first yields the
    rest of the frame the last loop stopped in (the events and activities that loop did not yield; after `wait_for`,
    the events after the match and all of that frame's activities), then the frames after it (the stream resumes
    after the last frame fed, while the unit still holds it). By default it counts as a
    viewer (`observe=False`): the worker interprets only while somebody is watching, so keep it running to get events.

    Wall times: the unit sends a `clock` event only when the shared state changes, none on connect, so after every
    (re)connect the Interpreter reads the state (`GET /session`) before the first frame. Every event and activity has
    its `wall` time from then on; on the live camera that is the timestamp that matters.
    """

    def __init__(
        self,
        streams: Streams,
        *,
        layers: Layers = ("world", "cards"),
        observe: bool | None = None,
        backfill: bool = True,
        resume: bool = True,
        precision: str | None = "rounded",
        core: InterpreterCore | None = None,
        max_reconnects: int | None = None,
    ) -> None:
        self._streams = streams
        self._layers = layers
        self._observe = observe
        self._backfill = backfill
        self._resume = resume
        self._precision = precision
        self._max_reconnects = max_reconnects
        self.core = core or InterpreterCore()
        """The state machine this interpreter feeds."""
        self.last_event_id: str | None = None
        """The id of the newest frame event received; the next iteration resumes after it."""
        self._busy = False
        self._pending: Update | None = None  # the rest of the frame where the last loop stopped

    @property
    def state(self) -> InterpreterState:
        """The state after the newest frame."""
        return self.core.state

    def summary(self) -> Summary:
        """The counts so far."""
        return self.core.summary()

    def finish(self) -> list[Activity]:
        """Close what is still open (as `interrupted`), for example before you stop following the session; the open
        tray leaves (its facts are in `trays()`; `end()` returns them too)."""
        return self.core.finish()

    def end(self) -> Update:
        """`finish()` as the final update: the activities still open and the facts of the open tray leaving."""
        return self.core.end()

    def trays(self) -> list[Tray]:
        """The trays so far, oldest first (see `InterpreterCore.trays`): jump between their `arrived` and `leaving`
        points with `factory.session.seek(point.t)` on a recording."""
        return self.core.trays()

    def speckle(self) -> list[TraySpeckle]:
        """The unit's speckle answers read so far, oldest first (see `InterpreterCore.speckle`)."""
        return self.core.speckle()

    def track(self, stone_id: int) -> list[TrackPoint]:
        """The path of a stone's solid from the frames this interpreter fed (see `InterpreterCore.track`); the server's
        own track since the session began is `factory.api.get_stone`."""
        return self.core.track(stone_id)

    def updates(self) -> AsyncStream[Update]:
        """Every frame's update (duplicates are skipped). The stream is endless while `resume` is on."""
        return AsyncStream(self._updates())

    def events(self) -> AsyncStream[Event]:
        """Just the events, typed, in world order."""
        return AsyncStream(self._events())

    def activities(self) -> AsyncStream[Activity]:
        """Just the completed activities: moves and push bursts."""
        return AsyncStream(self._activities())

    def tray_facts(self) -> AsyncStream[TrayFact]:
        """Just the tray facts: trays arriving and leaving, tray stones removed, set down or no longer sure."""
        return AsyncStream(self._tray_facts())

    def tray_reports(self) -> AsyncStream[TrayReport]:
        """A report as each tray leaves (SDK-DESIGN.md 4.7): which stones were removed, added and moved, and whether
        that is the tray's whole account. A tray leaves when the next one arrives, the session changes or the frames
        end."""
        return AsyncStream(self._tray_reports())

    async def wait_for(self, match: str | Callable[[Event], bool], timeout: float | None = None) -> Event:
        """The first event of type `match` (for example `"lift"`), or the first for which `match(event)` is true.

        Raises `TimeoutError` after `timeout` seconds, and `FactoryError` (`stream_closed`) if the stream ends first.
        The rest of the matching event's frame (its later events and its activities) is left to the next loop.
        """
        predicate = _type_is(match) if isinstance(match, str) else match

        async def first() -> Event:
            async with self.events() as events:
                async for event in events:
                    if predicate(event):
                        return event
            raise FactoryError(code="stream_closed", message="the stream ended before a matching event")

        if timeout is None:
            return await first()
        try:
            return await asyncio.wait_for(first(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"no matching event within {timeout} s") from None

    async def _updates(self) -> AsyncGenerator[Update, None]:
        if self._busy:
            raise RuntimeError("this Interpreter is already being iterated; make another with factory.interpreter()")
        self._busy = True
        stream = self._streams.frames(
            self._layers,
            observe=self._observe,
            precision=self._precision,
            resume=self._resume,
            backfill=self._backfill,
            clock_on_connect=True,
            last_event_id=self.last_event_id,
            max_reconnects=self._max_reconnects,
        )
        rest, self._pending = self._pending, None
        try:
            if rest is not None:
                yield rest
            async for item in stream:
                if isinstance(item, FrameItem):
                    if item.id:
                        self.last_event_id = item.id
                    update = self.core.feed(item.frame)
                    if not update.dropped:
                        yield update
                elif isinstance(item, ClockItem):
                    self.core.clock(item.state)
                elif isinstance(item, (GapItem, ResetItem)):
                    self.core.gap()
            yield self.core.end()
        finally:
            self._busy = False
            await stream.aclose()

    async def _events(self) -> AsyncGenerator[Event, None]:
        async with self.updates() as updates:
            async for update in updates:
                passed = 0
                try:
                    for event in update.events:
                        passed += 1
                        yield event
                    passed = -1
                finally:
                    if passed >= 0:  # the loop stopped inside this frame
                        self._keep(update, passed, 0)

    async def _activities(self) -> AsyncGenerator[Activity, None]:
        async with self.updates() as updates:
            async for update in updates:
                passed = 0
                try:
                    for activity in update.activities:
                        passed += 1
                        yield activity
                    passed = -1
                finally:
                    if passed >= 0:
                        self._keep(update, 0, passed)

    async def _tray_facts(self) -> AsyncGenerator[TrayFact, None]:
        async with self.updates() as updates:
            async for update in updates:
                passed = 0
                try:
                    for fact in update.tray_facts:
                        passed += 1
                        yield fact
                    passed = -1
                finally:
                    if passed >= 0:
                        self._keep(update, 0, 0, passed)

    async def _tray_reports(self) -> AsyncGenerator[TrayReport, None]:
        async with self.tray_facts() as facts:
            async for fact in facts:
                if fact.kind != "leaving":
                    continue
                left = [t for t in self.core.trays() if t.session == fact.session and t.n == fact.tray]
                for report in [r for r in (t.report() for t in left) if r is not None]:
                    yield report

    def _keep(self, update: Update, events: int, activities: int, facts: int = 0) -> None:
        """Keeps the rest of `update`, after its first `events` events, first `activities` activities and first `facts`
        tray facts, for the next loop."""
        rest = dataclasses.replace(
            update,
            events=update.events[events:],
            activities=update.activities[activities:],
            tray_facts=update.tray_facts[facts:],
            before=max(0, update.before - activities),  # the activities taken were the first ones
        )
        if rest.events or rest.activities or rest.tray_facts:
            self._pending = rest

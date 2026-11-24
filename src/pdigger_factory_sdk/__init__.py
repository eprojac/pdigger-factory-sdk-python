"""pDigger Factory SDK for Python: what happened to each tray of stones at a factory camera, and the detail behind it.

Two modes, one client:

- Simple mode, the report of each tray: `Replay.open(source).tray_reports()` for a recording,
  `factory.interpreter().tray_reports()` from a unit as each tray leaves (`TrayReport`: the stones removed, added and
  moved, when and where, and whether that is the tray's whole account).
- Advanced mode, the detail, for research and for correcting a report that is not complete, in three layers:
  - the interpreter: `FactoryClient.interpreter()` (events, moves, push bursts, hands, stones, tray facts and the open
    account, summary, human-readable text), `Replay` (the same over a recording file), `factory.session` (the shared
    session and its clock);
  - the streams: `factory.streams` (frames over SSE with resume, revisions, backfill; long-poll; raw packets) and the
    render-ready draw lists (`factory.api.render_overlay2d`, `render_scene3d`);
  - the API: `factory.api` (one method per HTTP operation, typed `models`), `codec` (maps, bitsets, depth, surfaces,
    joints).

`testing.FakeFactory` answers the API from fixtures or a recording, so code using the SDK can be unit-tested offline.

    import asyncio
    from pdigger_factory_sdk import FactoryClient

    async def main() -> None:
        async with FactoryClient("http://factory-orin.local:8120") as factory:
            async for report in factory.interpreter().tray_reports():
                print(report.describe())

    asyncio.run(main())
"""

from ._http import Backoff
from ._stream import AsyncStream
from ._version import USER_AGENT, __version__
from .api import Api, EventStream, Media, MediaStream, Metric, Metrics, Picture, StreamMessage
from .client import DEFAULT_URL, FactoryClient
from .errors import ErrorCode, FactoryError
from .interpreter import (
    Activity,
    Event,
    FrameRef,
    HandView,
    Interpreter,
    InterpreterCore,
    InterpreterState,
    Move,
    PushBurst,
    Summary,
    TrackPoint,
    Update,
)
from .models import Frame, Source, State, StoneView, WorldLayer, WorldSpeckle
from .replay import RecordingHeader, Replay, ReplayResult
from .session import LIVE, Session, SessionClock
from .streams import ClockItem, FrameItem, GapItem, PacketItem, ResetItem, StreamItem, Streams, WorkerItem
from .trays import (
    StoneSpeckle,
    Tray,
    TrayCandidate,
    TrayChange,
    TrayFact,
    TrayOpen,
    TrayPoint,
    TrayQuestion,
    TrayReport,
    TraySpeckle,
    TrayStone,
)

__all__ = [
    "DEFAULT_URL",
    "LIVE",
    "USER_AGENT",
    "Activity",
    "Api",
    "AsyncStream",
    "Backoff",
    "ClockItem",
    "ErrorCode",
    "Event",
    "EventStream",
    "FactoryClient",
    "FactoryError",
    "Frame",
    "FrameItem",
    "FrameRef",
    "GapItem",
    "HandView",
    "Interpreter",
    "InterpreterCore",
    "TrackPoint",
    "InterpreterState",
    "Media",
    "MediaStream",
    "Metric",
    "Metrics",
    "Move",
    "PacketItem",
    "Picture",
    "PushBurst",
    "RecordingHeader",
    "Replay",
    "ReplayResult",
    "ResetItem",
    "Session",
    "SessionClock",
    "Source",
    "State",
    "StoneSpeckle",
    "StoneView",
    "StreamItem",
    "StreamMessage",
    "Streams",
    "Summary",
    "Tray",
    "TrayCandidate",
    "TrayChange",
    "TrayFact",
    "TrayOpen",
    "TrayPoint",
    "TrayQuestion",
    "TrayReport",
    "TraySpeckle",
    "TrayStone",
    "Update",
    "WorkerItem",
    "WorldLayer",
    "WorldSpeckle",
    "__version__",
]

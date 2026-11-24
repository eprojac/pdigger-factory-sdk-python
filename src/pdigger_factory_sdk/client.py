"""`FactoryClient`: the entry point. One client per server; every level hangs off it (SDK-DESIGN.md section 2.1).

    async with FactoryClient("http://factory-orin.local:8120") as factory:
        sources = await factory.sources()                 # high level
        async for event in factory.interpreter().events():
            print(event.describe())

`factory.api` is the low level (one method per operation), `factory.streams` the mid level (typed streams),
`factory.session` the shared session's controls and clock, `factory.interpreter()` the high level.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Callable, Mapping
from types import TracebackType

import httpx

from ._defaults import DEFAULTS
from ._http import Backoff, HttpCore, SleepFn
from ._version import USER_AGENT
from .api import Api, Layers
from .errors import FactoryError
from .interpreter import Interpreter, InterpreterCore
from .models import Source
from .session import Session
from .streams import Streams

__all__ = ["DEFAULT_URL", "FactoryClient"]

DEFAULT_URL = "http://factory-orin.local:8120"
"""The base URL used when none is given and `PDIGGER_FACTORY_URL` is not set."""


class FactoryClient:
    """An async client of one pDigger Factory server. Use it as `async with FactoryClient(url) as factory:`.

    Args:
        base_url: The server, for example `http://factory-orin.local:8120`; default `PDIGGER_FACTORY_URL`, else
            `DEFAULT_URL`. A path prefix is kept (`https://host/prefix` asks `https://host/prefix/api/v2/...`). A URL
            that is not `http(s)://host[:port][/prefix]` raises `FactoryError` with code `invalid_param` (field
            `base_url`) here, before any request.
        timeout: Request timeout, seconds.
        connect_timeout: Connect timeout, seconds.
        stream_timeout: Longest silence on a stream before it counts as broken (and is resumed), seconds.
        retries: Retries of a request (GET/HEAD on connection errors, 429, 502, 503, 504; controls on 429).
        backoff: The retry backoff (base 0.25 s, factor 2, max 5 s, full jitter).
        headers: Extra headers for every request.
        token: A bearer token (`Authorization: Bearer ...`); the 20.20 unit ignores it, a deployment may require it.
        user_agent_suffix: Appended to the user agent `pdigger-factory-sdk-python/<version>`.
        transport: An httpx async transport (tests pass a `testing.FakeFactory`).
        http_client: Your own `httpx.AsyncClient` (it is not closed by the client).
        observe: The default of `observe` for streams and the Interpreter: `False` counts this client as a viewer,
            which keeps the worker interpreting; `True` (`observe=1`) watches without keeping it busy.
        rng: The random source of the backoff's jitter (seed it for deterministic retries).
        sleep: The async sleep of retries, backfill and `session.until_live()` (tests inject one that does not wait).
        monotonic: The monotonic clock of backfill timing and of `session.until_live()`'s timeout.
        wall_clock: The local wall clock of `session.clock()`.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = 10.0,
        connect_timeout: float = 5.0,
        stream_timeout: float = 30.0,
        retries: int = 3,
        backoff: Backoff | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
        user_agent_suffix: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        observe: bool = False,
        rng: random.Random | None = None,
        sleep: SleepFn | None = None,
        monotonic: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
    ) -> None:
        defaults = DEFAULTS.get()
        self.base_url = _checked_base_url(base_url or os.environ.get("PDIGGER_FACTORY_URL") or DEFAULT_URL)
        if transport is None and defaults is not None and http_client is None:
            transport = defaults.transport
        if rng is None:
            rng = random.Random(defaults.seed if defaults is not None else None)
        sleep = sleep or (defaults.sleep if defaults is not None else None) or asyncio.sleep
        monotonic = monotonic or (defaults.monotonic if defaults is not None else None) or time.monotonic
        wall_clock = wall_clock or (defaults.wall if defaults is not None else None) or time.time
        agent = USER_AGENT + (f" {user_agent_suffix}" if user_agent_suffix else "")
        all_headers = {"User-Agent": agent, **(headers or {})}
        if token:
            all_headers["Authorization"] = f"Bearer {token}"
        self._owns_client = http_client is None
        client = http_client or httpx.AsyncClient(transport=transport)
        backoff = backoff or Backoff()
        self._http = HttpCore(
            client,
            base_url=self.base_url,
            headers=all_headers,
            retries=retries,
            backoff=backoff,
            rng=rng,
            sleep=sleep,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            stream_timeout=httpx.Timeout(timeout, connect=connect_timeout, read=stream_timeout),
            wall=wall_clock,
        )
        self.observe = observe
        self.api = Api(self._http)
        """Low level: one method per HTTP operation."""
        self.session = Session(self.api, wall_clock, sleep=sleep, monotonic=monotonic)
        """High level: the shared session's controls and clock (they move it for every viewer)."""
        self.streams = Streams(
            self.api,
            sleep=sleep,
            monotonic=monotonic,
            backoff=backoff,
            rng=rng,
            observe=observe,
            on_state=self.session._note,
        )
        """Mid level: typed frame, long-poll and raw packet streams."""

    async def __aenter__(self) -> FactoryClient:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool (not that of an `http_client` you passed)."""
        if self._owns_client:
            await self._http.aclose()

    def interpreter(
        self,
        layers: Layers = ("world", "cards"),
        *,
        observe: bool | None = None,
        backfill: bool = True,
        resume: bool = True,
        precision: str | None = "rounded",
        core: InterpreterCore | None = None,
        max_reconnects: int | None = None,
    ) -> Interpreter:
        """The Interpreter on the shared session: whatever it shows, a live camera or a recording.

        Args:
            layers: The layers it asks for: `world` drives it, `cards` adds the hand cards' titles.
            observe: True watches without counting as a viewer (an idle worker then interprets nothing).
            backfill: Hold frames whose world was late until it is read (in order, up to about 4 s later).
            resume: Follow the session endlessly, reconnecting with `Last-Event-ID`.
            precision: `rounded` (default) or `full`.
            core: An `InterpreterCore` to continue (a new one by default).
            max_reconnects: Give up with `stream_closed` after this many failed reconnects in a row (default: never;
                every failed attempt is logged as a warning to the `pdigger_factory_sdk` logger).
        """
        return Interpreter(
            self.streams,
            layers=layers,
            observe=self.observe if observe is None else observe,
            backfill=backfill,
            resume=resume,
            precision=precision,
            core=core,
            max_reconnects=max_reconnects,
        )

    async def sources(self) -> list[Source]:
        """The catalog: every source the shared session can show (the recordings, then the live cameras)."""
        catalog = await self.api.list_videos()
        return [] if catalog is None else catalog.videos

    async def source(self, id: str) -> Source:
        """One source by id (`"dry1"`, a camera's serial such as `"CAM00000001"`, ...)."""
        return await self.api.get_video(id)

    async def cameras(self) -> list[Source]:
        """The live cameras of the catalog (`kind: camera`), in catalog order; empty when the server lists none.

        Each is a source like a recording: `session.watch(camera)` selects it for the shared session, and
        `session.until_live()` waits out its connection and warm-up. Their ids are the camera serials.
        """
        return [source for source in await self.sources() if source.is_live]

    async def live(self) -> Source:
        """The first live camera of the catalog; `FactoryError` with code `live_unavailable` when it has none.

        For code written for one camera; a unit may list several (20.20 lists six plant cameras): prefer `cameras()`.
        """
        cameras = await self.cameras()
        if not cameras:
            raise FactoryError(code="live_unavailable", message="the server's catalog has no live camera")
        return cameras[0]

    def __repr__(self) -> str:
        return f"FactoryClient({self.base_url!r})"


def _checked_base_url(base_url: str) -> str:
    """`base_url` when it is `http(s)://host[:port][/prefix]`, else `FactoryError` `invalid_param` (field `base_url`).

    The SDK appends the API's paths to it, so a query or a fragment is refused too.
    """
    problem = None
    try:
        url = httpx.URL(base_url)
    except httpx.InvalidURL as e:
        problem = str(e)
    else:
        if url.scheme not in ("http", "https"):
            problem = "the scheme must be http or https"
        elif not url.host:
            problem = "it has no host"
        elif url.port is not None and not 0 < url.port < 65536:
            problem = f"port {url.port} is not 1 to 65535"
        elif url.query or url.fragment:
            problem = "it has a query or a fragment"
    if problem is not None:
        raise FactoryError(
            code="invalid_param",
            field="base_url",
            message=f"base URL {base_url!r} must be http(s)://host[:port]: {problem}",
        )
    return base_url

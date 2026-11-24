"""HTTP transport: one place for timeouts, retries, backoff and error mapping (SDK-DESIGN.md section 2.1)."""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import FactoryError

__all__ = ["Backoff", "HttpCore", "SleepFn", "http_error"]

log = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]
"""An async sleep (`asyncio.sleep` by default; tests inject one that does not wait)."""

_IDEMPOTENT = frozenset({"GET", "HEAD"})
_RETRY_STATUS_IDEMPOTENT = frozenset({429, 502, 503, 504})


@dataclass(slots=True, frozen=True)
class Backoff:
    """Exponential backoff with full jitter: attempt `k` waits uniformly in `[0, min(max, base * factor**k)]`.

    Deterministic when the client is given a seeded `random.Random`.
    """

    base: float = 0.25
    """Seconds of the first ceiling."""
    factor: float = 2.0
    """Growth of the ceiling per attempt."""
    max: float = 5.0
    """The largest ceiling, seconds."""

    def ceiling(self, attempt: int) -> float:
        """The longest wait before retry number `attempt` (0 for the first retry)."""
        if self.base == 0:
            return 0.0  # min(max, 0 * factor**k) for every k, also where factor**k overflows
        try:
            grown = self.base * float(self.factor) ** attempt  # in floats: an int factor would make an exact power
        except OverflowError:  # a long outage (or a large factor): far past the cap
            return self.max
        return min(self.max, grown)

    def delay(self, attempt: int, rng: random.Random) -> float:
        """A wait before retry number `attempt`: uniform in `[0, ceiling(attempt)]`."""
        return rng.uniform(0.0, self.ceiling(attempt))


class HttpCore:
    """Sends requests with the client's retry policy and turns error replies into `FactoryError`.

    GET and HEAD are retried on connection errors, 429, 502, 503 and 504; POST, PUT and DELETE only on 429 (the server
    checks rate limits before it applies anything). A retry waits the error body's `retry_ms`, else `Retry-After`,
    else the backoff. Other 4xx replies are never retried, and neither is a request that cannot be sent at all (a URL
    that is not valid, a scheme without a transport).
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        headers: Mapping[str, str],
        retries: int,
        backoff: Backoff,
        rng: random.Random,
        sleep: SleepFn,
        timeout: httpx.Timeout,
        stream_timeout: httpx.Timeout,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers)
        self.retries = retries
        self.backoff = backoff
        self.rng = rng
        self.sleep = sleep
        self.timeout = timeout
        self.stream_timeout = stream_timeout
        self.wall = wall
        """The local wall clock that a `Retry-After` date is measured against when the reply has no `Date`."""

    def with_options(self, *, retries: int | None = None, timeout: float | None = None) -> HttpCore:
        """A view of this transport with other retries or request timeout (seconds); it shares the connection pool."""
        t = self.timeout if timeout is None else httpx.Timeout(timeout, connect=self.timeout.connect)
        return HttpCore(
            self.client,
            base_url=self.base_url,
            headers=self.headers,
            retries=self.retries if retries is None else retries,
            backoff=self.backoff,
            rng=self.rng,
            sleep=self.sleep,
            timeout=t,
            stream_timeout=self.stream_timeout,
            wall=self.wall,
        )

    async def send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        stream: bool = False,
        check: Callable[[httpx.Response], None] | None = None,
    ) -> httpx.Response:
        """Send with retries; returns a reply below 400 (a streamed one must be closed by the caller).

        `check` sees a successful reply that is not streamed before its body is read, and may refuse it by raising
        (the reply is closed first, and nothing is retried): `get_video_media` refuses a body that never ends.
        """
        method = method.upper()
        if self.is_closed:
            raise FactoryError(code="stream_closed", message=f"{method} {path}: the client is closed")
        idempotent = method in _IDEMPOTENT
        query = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            try:
                request = self.client.build_request(
                    method,
                    self.base_url + path,
                    params=query,
                    json=body,
                    headers={**self.headers, **(headers or {})},
                    timeout=self.stream_timeout if stream else self.timeout,
                )
            except httpx.InvalidURL as e:  # not an httpx.HTTPError: no request was made
                raise http_error(f"{method} {path}", e) from e
            try:
                # Always streamed, so an error reply's body is read below, where one that breaks or does not
                # decode still leaves its status; a successful reply's body is read here (and retried like a
                # connection error when it breaks), unless the caller streams it.
                response = await self.client.send(request, stream=True)
                if not stream and response.status_code < 400:
                    try:
                        if check is not None:
                            check(response)
                        await response.aread()
                    finally:
                        await response.aclose()
            except httpx.HTTPError as e:
                if _retryable_failure(e) and idempotent and attempt < self.retries:
                    wait = self.backoff.delay(attempt, self.rng)
                    log.debug(
                        "%s %s: %s; retry %d of %d in %.2f s",
                        method,
                        path,
                        _describe(e),
                        attempt + 1,
                        self.retries,
                        wait,
                    )
                    await self.sleep(wait)
                    attempt += 1
                    continue
                raise http_error(f"{method} {path}", e) from e
            if response.status_code < 400:
                return response
            try:
                content = await response.aread()
            except httpx.HTTPError:
                content = b""  # the error body broke or does not decode: the status still says what happened
            finally:
                await response.aclose()
            error = FactoryError.from_reply(response.status_code, content, response.headers, now=self.wall())
            status = response.status_code
            retryable = status == 429 or (idempotent and status in _RETRY_STATUS_IDEMPOTENT)
            if retryable and attempt < self.retries:
                wait = error.retry_ms / 1000.0 if error.retry_ms is not None else self.backoff.delay(attempt, self.rng)
                log.debug(
                    "%s %s: HTTP %d %s; retry %d of %d in %.2f s",
                    method,
                    path,
                    status,
                    error.code,
                    attempt + 1,
                    self.retries,
                    wait,
                )
                await self.sleep(wait)
                attempt += 1
                continue
            raise error

    async def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET a JSON reply, decoded."""
        response = await self.send("GET", path, params=params, headers={"Accept": "application/json"})
        return decode_json(response.content, path)

    async def send_json(self, method: str, path: str, *, params: Mapping[str, Any] | None, body: Any) -> Any:
        """A control: send a JSON body, decode the JSON reply."""
        response = await self.send(method, path, params=params, body=body, headers={"Accept": "application/json"})
        return decode_json(response.content, path)

    @property
    def is_closed(self) -> bool:
        """Whether the connection pool is closed."""
        return self.client.is_closed

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self.client.aclose()


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def loads(text: str | bytes) -> Any:
    """`json.loads` without the `NaN` and `Infinity` that Python's json reads but JSON (RFC 8259) does not have; JSON
    nested deeper than Python's recursion limit is a `ValueError` too, like any other text that does not decode."""
    try:
        return json.loads(text, parse_constant=_no_constant)
    except RecursionError:
        raise ValueError("JSON nested too deep") from None


def decode_json(content: bytes, what: str) -> Any:
    """Decoded JSON, or a `decode` error."""
    try:
        return loads(content)
    except ValueError as e:
        raise FactoryError(code="decode", message=f"{what}: the reply is not JSON ({e})") from None


def _retryable_failure(e: httpx.HTTPError) -> bool:
    """A failure that another attempt may not meet: a connection error, not a scheme that no transport serves."""
    return isinstance(e, httpx.TransportError) and not isinstance(e, httpx.UnsupportedProtocol)


def http_error(what: str, e: httpx.HTTPError | httpx.InvalidURL) -> FactoryError:
    """The `FactoryError` of an httpx failure: `decode` for a body that does not decode (a bad `Content-Encoding`),
    `invalid_param` for a request that cannot be sent at all (a URL that is not valid, a scheme without a transport;
    field `base_url`), `transport` for everything else (no usable HTTP reply: connect, read, timeout, protocol
    errors)."""
    if isinstance(e, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return FactoryError(code="invalid_param", field="base_url", message=f"{what}: {_describe(e)}")
    code = "decode" if isinstance(e, httpx.DecodingError) else "transport"
    return FactoryError(code=code, message=f"{what}: {_describe(e)}")


def _describe(e: Exception) -> str:
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__

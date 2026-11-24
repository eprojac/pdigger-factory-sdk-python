"""The transport: retries, backoff, error mapping, headers and timeouts."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from email.utils import formatdate

import httpx
import pytest

from pdigger_factory_sdk import Backoff, FactoryClient, FactoryError, __version__
from pdigger_factory_sdk._http import HttpCore, decode_json, loads
from pdigger_factory_sdk.testing import FakeFactory, FakeTime

from .support import API, client


def test_backoff() -> None:
    b = Backoff()
    assert [b.ceiling(k) for k in range(6)] == [0.25, 0.5, 1.0, 2.0, 4.0, 5.0]
    rng = random.Random(1)
    waits = [b.delay(k, rng) for k in range(20)]
    assert all(0 <= w <= b.ceiling(k) for k, w in enumerate(waits))
    again = random.Random(1)
    assert waits == [b.delay(k, again) for k in range(20)]
    assert b.ceiling(10_000) == 5.0  # a long outage: the cap, never an overflow
    assert Backoff(factor=1.01).ceiling(400) == 5.0  # a slow growth still reaches the cap
    assert Backoff(factor=1.04).ceiling(100) == 5.0
    assert Backoff(factor=1e5).ceiling(64) == 5.0  # far past it, without an overflow
    assert Backoff(factor=2).ceiling(10**7) == 5.0  # an int factor too (in floats, at once)
    assert Backoff(base=0.0).ceiling(5000) == 0.0  # no wait at all, also where factor**k overflows
    assert Backoff(base=0.0, factor=1e5).ceiling(64) == 0.0
    assert 0 <= b.delay(10_000, rng) <= 5.0


async def test_get_retries_on_503_with_backoff() -> None:
    fake = FakeFactory(replies=API)
    fake.fail("getHealth", status=503, code="busy", times=2)
    async with client(fake) as factory:
        health = await factory.api.get_health()
    assert health.ok
    assert len(fake.time.slept) == 2
    assert fake.time.slept[0] <= 0.25
    assert fake.time.slept[1] <= 0.5


async def test_retry_ms_and_retry_after_are_used() -> None:
    fake = FakeFactory(replies=API)
    fake.fail("getHealth", status=429, code="too_many_requests", retry_ms=300)
    fake.fail("getHealth", status=503, code="busy", retry_after="2")
    async with client(fake) as factory:
        await factory.api.get_health()
    assert fake.time.slept == [0.3, 2.0]


async def test_a_retry_after_date_is_measured_against_the_client_wall_clock() -> None:
    fake = FakeFactory(replies=API, time=FakeTime(wall=1794958961.0))
    fake.fail("getHealth", status=503, code="busy", retry_after=formatdate(1794958964.0, usegmt=True))
    async with client(fake) as factory:  # the fake's replies have no Date header
        await factory.api.get_health()
    assert fake.time.slept == [3.0]


async def test_gives_up_after_the_retries() -> None:
    fake = FakeFactory(replies=API)
    fake.fail("getHealth", status=502, code="internal", times=5)
    async with client(fake, retries=2) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.get_health()
    assert info.value.status == 502
    assert len(fake.time.slept) == 2


async def test_transport_errors_are_retried_for_get_only() -> None:
    fake = FakeFactory(replies=API)
    fake.fail("getHealth", exc=httpx.ConnectError("refused"), times=1)
    async with client(fake) as factory:
        assert (await factory.api.get_health()).ok
        fake.fail("sessionPause", exc=httpx.ConnectError(""), times=1)
        with pytest.raises(FactoryError) as info:
            await factory.api.session_pause(dry_run=True)
    assert info.value.code == "transport"
    assert info.value.status is None
    assert info.value.message == "POST /api/v2/session/pause: ConnectError"
    assert isinstance(info.value.__cause__, httpx.ConnectError)


async def test_a_request_that_cannot_be_sent_is_not_retried() -> None:
    fake = FakeFactory(replies=API)
    fake.fail("getHealth", exc=httpx.UnsupportedProtocol("no transport for ftp"), times=9)
    async with client(fake) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.get_health()
    assert (info.value.code, info.value.field, info.value.status) == ("invalid_param", "base_url", None)
    assert info.value.message == "GET /api/v2/health: UnsupportedProtocol: no transport for ftp"
    assert not info.value.is_retryable
    assert (len(fake.requests), fake.time.slept) == (1, [])


async def test_a_url_that_is_not_valid_is_an_error_of_the_sdk() -> None:
    t = FakeTime()
    async with httpx.AsyncClient(transport=FakeFactory(replies=API)) as http:
        core = HttpCore(
            http,
            base_url="http://[::1:8120",  # FactoryClient refuses this one when it is made
            headers={},
            retries=3,
            backoff=Backoff(),
            rng=random.Random(1),
            sleep=t.sleep,
            timeout=httpx.Timeout(1.0),
            stream_timeout=httpx.Timeout(1.0),
        )
        with pytest.raises(FactoryError) as info:
            await core.send("GET", "/api/v2/health")
    assert (info.value.code, info.value.field, info.value.status) == ("invalid_param", "base_url", None)
    assert info.value.message == "GET /api/v2/health: InvalidURL: Invalid port: ':1:8120'"
    assert isinstance(info.value.__cause__, httpx.InvalidURL)
    assert t.slept == []


async def test_transport_error_after_the_retries() -> None:
    fake = FakeFactory(replies=API)
    fake.fail("getHealth", exc=httpx.ReadTimeout("slow"), times=9)
    async with client(fake, retries=1) as factory:
        with pytest.raises(FactoryError, match="ReadTimeout: slow"):
            await factory.api.get_health()


async def test_controls_retry_on_429_but_not_on_503() -> None:
    fake = FakeFactory(replies=API)
    fake.fail("sessionPause", status=429, code="too_many_controls", retry_ms=300)
    async with client(fake) as factory:
        reply = await factory.api.session_pause(dry_run=True)
        assert reply.state is not None
        fake.fail("sessionPause", status=503, code="busy")
        with pytest.raises(FactoryError) as info:
            await factory.api.session_pause(dry_run=True)
    assert info.value.code == "busy"
    assert fake.time.slept == [0.3]


async def test_4xx_is_not_retried() -> None:
    fake = FakeFactory(replies=API)
    async with client(fake) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.get_video("nope")
    assert info.value.code == "unknown_video"
    assert fake.time.slept == []


class _BrokenBody(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"co'
        raise httpx.ReadError("gone")


async def test_streamed_error_body_that_breaks_while_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, stream=_BrokenBody())

    t = FakeTime()
    async with FactoryClient("http://fake.test", transport=httpx.MockTransport(handler), sleep=t.sleep) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.stream_frames()
    assert info.value.code == "not_found"
    assert info.value.message == "HTTP 404"


async def test_a_body_that_breaks_is_retried_like_a_connection_error() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(200, stream=_BrokenBody())
        return httpx.Response(200, json={"ok": True, "worker": "running", "viewers": 0})

    t = FakeTime()
    async with FactoryClient("http://fake.test", transport=httpx.MockTransport(handler), sleep=t.sleep) as factory:
        assert (await factory.api.get_health()).ok
    assert calls == ["GET", "GET"]
    assert len(t.slept) == 1


def _undecodable(status: int = 200, content_type: str = "application/json") -> httpx.MockTransport:
    """Replies whose body does not decode: `Content-Encoding: gzip` on bytes that are not gzip."""

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Encoding": "gzip", "Content-Type": content_type}
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(b"not gzip at all"))

    return httpx.MockTransport(handler)


async def test_a_reply_that_does_not_decode_is_a_decode_error() -> None:
    t = FakeTime()
    async with FactoryClient("http://fake.test", transport=_undecodable(), sleep=t.sleep) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.get_health()
    assert info.value.code == "decode"
    assert info.value.status is None
    assert info.value.message.startswith("GET /api/v2/health: DecodingError: ")
    assert isinstance(info.value.__cause__, httpx.DecodingError)
    assert t.slept == []  # not retried


async def test_an_error_reply_that_does_not_decode_keeps_its_status() -> None:
    t = FakeTime()
    async with FactoryClient("http://fake.test", transport=_undecodable(404), sleep=t.sleep) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.get_health()
    assert (info.value.code, info.value.status, info.value.message) == ("not_found", 404, "HTTP 404")


async def test_a_stream_that_does_not_decode_is_a_decode_error() -> None:
    t = FakeTime()
    transport = _undecodable(content_type="text/event-stream")
    async with FactoryClient("http://fake.test", transport=transport, sleep=t.sleep, monotonic=t.monotonic) as factory:
        stream = await factory.api.stream_frames()
        with pytest.raises(FactoryError) as info:
            async for _ in stream:
                pass
        assert info.value.code == "decode"
        assert info.value.message.startswith("the connection broke: DecodingError: ")
        with pytest.raises(FactoryError) as again:
            async for _ in factory.streams.frames():  # a decode error is not resumed
                pass
        assert again.value.code == "decode"
    assert t.slept == []


async def test_headers_user_agent_token_and_prefix() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "worker": "running", "viewers": 0})

    async with FactoryClient(
        "https://host.test/prefix/",
        transport=httpx.MockTransport(handler),
        headers={"X-Team": "a"},
        token="t0k",
        user_agent_suffix="my-app/2",
        timeout=3.0,
    ) as factory:
        await factory.api.get_health()
        await factory.api.with_options(retries=0, timeout=1.5).get_health()
        await factory.api.with_options().get_health()
    first = seen[0]
    assert str(first.url) == "https://host.test/prefix/api/v2/health"
    assert first.headers["User-Agent"] == f"pdigger-factory-sdk-python/{__version__} my-app/2"
    assert first.headers["Authorization"] == "Bearer t0k"
    assert first.headers["X-Team"] == "a"
    assert first.headers["Accept"] == "application/json"
    assert first.extensions["timeout"]["read"] == 3.0
    assert seen[1].extensions["timeout"]["read"] == 1.5


def test_decode_json() -> None:
    assert decode_json(b"[1]", "x") == [1]
    with pytest.raises(FactoryError, match="x: the reply is not JSON"):
        decode_json(b"{", "x")


def test_json_nested_too_deep_is_a_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Python 3.13 and older raise RecursionError on DEEP (test_damaged.py) and 3.14 does not, so it is raised here
    def too_deep(*_: object, **__: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("pdigger_factory_sdk._http.json.loads", too_deep)
    with pytest.raises(ValueError, match="nested too deep"):
        loads("[]")


async def test_a_reply_that_is_not_json() -> None:
    fake = FakeFactory(replies={"getHealth": "<html>"})
    async with client(fake) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.get_health()
    assert info.value.code == "decode"


async def test_own_http_client_is_not_closed() -> None:
    fake = FakeFactory(replies=API)
    own = httpx.AsyncClient(transport=fake)
    t = FakeTime()
    async with FactoryClient("http://fake.test", http_client=own, sleep=t.sleep) as factory:
        assert (await factory.api.get_health()).ok
    assert not own.is_closed
    await own.aclose()

"""The shared session, its clock, the live cameras and the client's options."""

from __future__ import annotations

import random
from dataclasses import replace

import httpx
import pytest

from pdigger_factory_sdk import DEFAULT_URL, LIVE, FactoryClient, FactoryError, Picture, SessionClock, State
from pdigger_factory_sdk.testing import CONNECT_S, LIVE_CAMERA, LIVE_LAG_S, FakeFactory, FakeTime, Reply, use_fake

from .support import API, api_json, client, recording


def state(**kw: object) -> State:
    base = {
        "session": 7,
        "revision": 3,
        "video": "dry1",
        "playing": True,
        "rate": 0.8,
        "anchor_media": 100.0,
        "anchor_wall": 1000.0,
        "hold_until": 1002.0,
        "duration": 120.0,
        "changed_by": "viewer",
        "focus": None,
        "media_time": 100.0,
    }
    base.update(kw)
    return State.from_json(base)


def clock(s: State, offset: float = 0.0, local: float = 0.0) -> SessionClock:
    return SessionClock(state=s, server_now=0.0, offset=offset, rtt=0.01, local_clock=lambda: local)


def test_media_time_is_held_then_runs_at_the_rate() -> None:
    c = clock(state())
    assert c.media_time_at(1001.0) == 100.0  # still in the worker's hold
    assert c.media_time_at(1012.0) == pytest.approx(108.0)
    assert c.media_time_at(1100.0) == 120.0  # clamped to the duration
    assert c.wall_of(108.0) == pytest.approx(1012.0)
    assert not c.is_live


def test_paused_clock() -> None:
    c = clock(state(playing=False, anchor_media=50.0))
    assert c.media_time_at(5000.0) == 50.0
    assert c.wall_of(52.0) == pytest.approx(1002.5)


def test_rate_zero_and_local_times() -> None:
    c = clock(state(rate=0.0), offset=10.0, local=990.0)
    assert c.wall_of(1.0) is None
    assert c.local_wall_of(1.0) is None
    assert c.server_time() == 1000.0
    assert c.server_time(5.0) == 15.0
    assert c.local_time(1000.0) == 990.0
    c2 = clock(state(), offset=10.0, local=1002.0)
    assert c2.media_time_now() == pytest.approx(108.0)
    assert c2.local_wall_of(108.0) == pytest.approx(1002.0)
    assert c2.progress() == pytest.approx(0.9)


def test_live_clock_has_no_duration_or_progress() -> None:
    c = clock(state(video=LIVE, rate=1.0, duration=None, hold_until=0.0), local=1100.0)
    assert c.is_live
    assert c.media_time_now() == 200.0
    assert c.progress() is None
    on_air = {"phase": "live", "warmup": 3.0, "since": 996.0, "lag": 0.3}
    camera = clock(state(video="CAM00000001", rate=1.0, duration=None, hold_until=1000.0, live=on_air), local=1010.0)
    assert camera.is_live
    assert camera.media_time_now() == pytest.approx(110.0)
    assert camera.wall_of(110.0) == pytest.approx(1010.0)
    assert clock(state(duration=0.0)).progress() is None
    assert clock(state(anchor_media=500.0), local=2000.0).progress() == 1.0


async def test_session_clock_sampling() -> None:
    fake = FakeFactory(replies=API)
    times = iter([10.0, 10.5])
    async with FactoryClient("http://fake.test", transport=fake, wall_clock=lambda: next(times)) as factory:
        c = await factory.session.clock()
    assert c.rtt == 0.5
    assert c.offset == pytest.approx(fake.time.time() - 10.25)
    assert c.state.session == api_json("getSession")["state"]["session"]


async def test_watch_plays_first_when_paused() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"))
    async with client(fake) as factory:
        await factory.session.pause()
        assert not (await factory.session.state()).playing
        new = await factory.session.watch("dry1", 300.0)
        assert new.playing
        assert new.anchor_media == 300.0
        assert [r.url.path.rsplit("/", 1)[-1] for r in fake.requests[-3:]] == ["session", "play", "select"]
        again = await factory.session.watch(await factory.source("dry1"))
        assert again.session == new.session + 1
        assert fake.requests[-1].url.path.endswith("/select")


async def test_watching_a_camera_connects_warms_up_and_goes_live() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"), live=True)
    async with client(fake) as factory:
        cameras = await factory.cameras()
        assert [c.id for c in cameras] == [LIVE_CAMERA]
        camera = await factory.live()
        assert camera == cameras[0]
        assert (camera.is_live, camera.seekable, camera.hold, camera.duration) == (True, False, 3.0, None)
        n = len(fake.requests)
        with pytest.raises(FactoryError) as info:
            await factory.session.watch(camera, at=3.0)  # a Source says it is a camera: refused without a request
        assert (info.value.code, info.value.field, info.value.status) == ("not_seekable", "time", None)
        assert len(fake.requests) == n
        with pytest.raises(FactoryError) as info:
            await factory.session.watch(LIVE_CAMERA, at=3.0)  # an id: the catalog says it is a camera
        assert (info.value.code, info.value.field) == ("not_seekable", "time")
        assert [r.url.path for r in fake.requests[n:]] == ["/api/v2/videos"]
        with pytest.raises(FactoryError) as info:
            await factory.session.watch(LIVE, at=3.0)  # the id the SDK gave the camera before it shipped
        assert info.value.field == "time"
        before = await factory.session.state()
        started = fake.time.time()
        watched = await factory.session.watch(LIVE_CAMERA)
        assert (watched.video, watched.duration, watched.rate, watched.playing) == (LIVE_CAMERA, None, 1.0, True)
        assert watched.session == before.session + 1
        assert watched.live is not None
        assert (watched.live.phase, watched.live.since, watched.live.warmup) == ("connecting", started, 3.0)
        assert factory.session.last_state == watched
        fake.time.advance(CONNECT_S)  # the camera's first frame: the interpreter warms up on it
        warming = await factory.session.state()
        assert warming.live is not None
        assert (warming.live.phase, warming.revision) == ("warming", watched.revision + 1)
        assert (await factory.api.poll_frames(wait=0)).frames == []  # nothing is released while it warms up
        live = await factory.session.until_live()
        assert live.live is not None
        assert (live.live.phase, live.live.lag, live.revision) == ("live", LIVE_LAG_S, watched.revision + 2)
        assert set(fake.time.slept) == {0.25}  # the state was read every 0.25 s
        went_live = live.hold_until - started
        assert CONNECT_S + 3.0 + LIVE_LAG_S <= went_live < CONNECT_S + 3.0 + LIVE_LAG_S + 0.2
        assert fake.time.time() - live.hold_until < 0.25
        frames = (await factory.api.poll_frames(limit=4)).frames
        assert [f.video for f in frames] == [LIVE_CAMERA] * 4
        assert frames[0].t == live.anchor_media  # the playhead starts at the first frame shown
        picture = await factory.session.picture()
        assert isinstance(picture, Picture)
        assert picture.g == (await factory.api.get_frame("latest")).g
        assert (await factory.session.picture(frames[1].g, session=live.session)).g == frames[1].g
        clock = await factory.session.clock()
        assert clock.is_live
        assert clock.progress() is None


async def test_seek_pause_and_play_on_a_camera() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"), live=True)
    async with client(fake) as factory:
        n = len(fake.requests)
        with pytest.raises(FactoryError) as info:
            await factory.session.seek(5.0)  # the shared state, read first, shows a camera: no seek is sent
        assert (info.value.code, info.value.field, info.value.status) == ("not_seekable", "video", None)
        assert [r.url.path for r in fake.requests[n:]] == ["/api/v2/session"]
        with pytest.raises(FactoryError) as info:
            await factory.session.seek(5.0, video=LIVE_CAMERA)  # sent: the unit refuses a seek in a camera
        assert (info.value.code, info.value.field, info.value.status) == ("not_seekable", "video", 409)
        n = len(fake.requests)
        with pytest.raises(FactoryError, match="not_seekable"):
            await factory.session.seek(5.0, video=LIVE)  # refused at once, without a request
        assert len(fake.requests) == n
        paused = await factory.session.pause()
        assert not paused.playing
        fake.time.advance(10.0)
        played = await factory.session.play()
        assert played.playing
        assert played.live is not None
        assert played.live.phase == "connecting"  # it connects again, as on the unit
        edge = fake.time.time() - played.live.since - played.live.lag
        assert played.anchor_media == pytest.approx(edge)  # play continues at the live edge
        again = await factory.session.until_live()  # the client's sleeps move the fake's clock
        assert again.live is not None
        assert (again.live.phase, again.revision) == ("live", played.revision + 2)
        recorded = await factory.session.watch("dry1", 300.0)
        assert recorded.live is None
        assert await factory.session.until_live() == recorded  # a recording: at once
        moved = await factory.session.seek(301.0, video="dry1", dry_run=True)
        assert moved.anchor_media == 301.0
        assert (await factory.session.state()).session == recorded.session


async def test_until_live_gives_up_after_its_timeout() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"), live=True)
    async with client(fake) as factory:
        await factory.session.watch(LIVE_CAMERA)
        with pytest.raises(FactoryError) as info:
            await factory.session.until_live(timeout=1.0)
        assert (info.value.code, info.value.message) == ("live_timeout", f"{LIVE_CAMERA} is still warming after 1 s")
        await factory.session.pause()  # a paused camera does not get on
        with pytest.raises(FactoryError, match=r"is still warming after 0\.6 s"):
            await factory.session.until_live(timeout=0.6)
        assert fake.time.slept[-3:] == [0.25, 0.25, pytest.approx(0.1)]


async def test_cameras_are_listed_after_the_recordings() -> None:
    catalog = api_json("listVideos")  # the 20.20 unit's: its four recordings, then its six plant cameras
    serials = ["CAM00000001", "CAM00000002", "CAM00000003", "CAM00000004", "CAM00000005", "CAM00000006"]
    fake = FakeFactory(replies={"listVideos": catalog})
    async with client(fake) as factory:
        assert [c.id for c in await factory.cameras()] == serials
        assert (await factory.live()).id == "CAM00000001"
        assert not any(s.is_live for s in (await factory.sources())[: len(catalog["videos"]) - 6])


async def test_a_fresh_client_reads_the_live_session_before_a_seek() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"), live=True)
    async with client(fake) as factory:
        before = factory.session.last_state
        with pytest.raises(FactoryError) as info:
            await factory.session.seek(5.0)
        assert before is None
        assert factory.session.last_state is not None  # the state read to decide
    assert (info.value.code, info.value.status, info.value.field) == ("not_seekable", None, "video")
    assert [r.url.path for r in fake.requests] == ["/api/v2/session"]  # read, refused, nothing sent


async def test_a_seek_follows_what_another_viewer_selected() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"), live=True)
    async with client(fake) as me, client(fake) as other:
        assert (await me.session.state()).video == LIVE_CAMERA  # what this client saw last
        await other.session.watch("dry1", 300.0)  # another viewer moves the shared session to a recording
        n = len(fake.requests)
        moved = await me.session.seek(301.0)
        assert (moved.video, moved.anchor_media) == ("dry1", 301.0)
        assert [r.url.path for r in fake.requests[n:]] == ["/api/v2/session", "/api/v2/session/seek"]
        assert me.session.last_state == moved
        await other.session.watch(LIVE_CAMERA)  # and back to the camera: this client last saw dry1
        n = len(fake.requests)
        with pytest.raises(FactoryError, match="not_seekable") as info:
            await me.session.seek(302.0)
        assert info.value.status is None
        assert [r.url.path for r in fake.requests[n:]] == ["/api/v2/session"]


async def test_a_seek_in_a_named_recording_is_sent_as_it_is() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"), live=True)
    async with client(fake) as factory:
        moved = await factory.session.seek(5.0, video="dry1")  # from the camera to the recording, at 0:05
    assert (moved.video, moved.anchor_media, moved.live) == ("dry1", 5.0, None)
    assert [r.url.path for r in fake.requests] == ["/api/v2/session/seek"]


async def test_the_last_state_seen() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"))
    async with client(fake) as factory:
        session = factory.session
        now = await session.state()
        assert session.last_state == now
        await session.pause(dry_run=True)  # a dry run's state is not kept
        assert session.last_state == now
        paused = await session.pause()
        assert session.last_state == paused
        session._note(replace(now, revision=paused.revision - 1))  # older: the same session, a lower revision
        assert session.last_state == paused
        other = replace(now, session=now.session + 1, revision=1)
        session._note(other)  # another session: newer whatever its revision
        assert session.last_state == other


async def test_focus_and_clear() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"))
    async with client(fake) as factory:
        world = await factory.api.get_world()
        stone = world.stones[0].id
        focused = await factory.session.focus(stone, g=world.g, session=world.session)
        assert focused.focus is not None
        assert focused.focus.id == stone
        cleared = await factory.session.clear_focus()
        assert cleared.focus is None


async def test_live_unavailable_without_a_camera() -> None:
    fake = FakeFactory(recording=recording("dry1-0296-0306-full"))  # a recording and no camera
    async with client(fake) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.live()
    assert info.value.code == "live_unavailable"


async def test_sources_on_304() -> None:
    fake = FakeFactory(replies={"listVideos": Reply(status=304)})
    async with client(fake) as factory:
        assert await factory.sources() == []


async def test_client_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PDIGGER_FACTORY_URL", raising=False)
    plain = FactoryClient()
    assert plain.base_url == DEFAULT_URL
    assert repr(plain) == f"FactoryClient('{DEFAULT_URL}')"
    await plain.aclose()
    monkeypatch.setenv("PDIGGER_FACTORY_URL", "http://unit.test:8120")
    assert FactoryClient().base_url == "http://unit.test:8120"


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("http://[::1:8120", "Invalid port: ':1:8120'"),
        ("factory-orin.local:8120", "the scheme must be http or https"),
        ("ftp://unit.test", "the scheme must be http or https"),
        ("http://", "it has no host"),
        ("http://unit.test:99999", "port 99999 is not 1 to 65535"),
        ("http://unit.test:0", "port 0 is not 1 to 65535"),
        ("http://unit.test:8120/?x=1", "it has a query or a fragment"),
        ("http://unit.test:8120/#top", "it has a query or a fragment"),
    ],
)
def test_a_base_url_that_cannot_work_is_refused_when_the_client_is_made(url: str, problem: str) -> None:
    with pytest.raises(FactoryError) as info:
        FactoryClient(url)
    assert (info.value.code, info.value.field, info.value.status) == ("invalid_param", "base_url", None)
    assert info.value.message == f"base URL {url!r} must be http(s)://host[:port]: {problem}"


async def test_base_urls_that_work(monkeypatch: pytest.MonkeyPatch) -> None:
    for url in ("https://unit.test/prefix/", "http://[::1]:8120", "HTTP://Unit.Test:8120", "http://user:pw@unit.test"):
        async with FactoryClient(url) as factory:
            assert factory.base_url == url
    monkeypatch.setenv("PDIGGER_FACTORY_URL", "unit.test:8120")  # the variable is checked too
    with pytest.raises(FactoryError, match="the scheme must be http or https"):
        FactoryClient()


async def test_use_fake_installs_transport_and_time() -> None:
    fake = FakeFactory(replies=API)
    with use_fake(fake, seed=3):
        factory = FactoryClient()
        fake.fail("getHealth", status=503, code="busy")
        async with factory:
            assert (await factory.api.get_health()).ok
            sampled = await factory.session.clock()
    assert len(fake.time.slept) == 1
    assert fake.time.slept[0] == pytest.approx(random.Random(3).uniform(0, 0.25))
    assert sampled.rtt == 0.0
    assert FactoryClient().observe is False


async def test_own_http_client_ignores_the_fake_default() -> None:
    fake = FakeFactory(replies=API)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=api_json("getHealth"))

    with use_fake(fake):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as own:
            factory = FactoryClient("http://own.test", http_client=own, sleep=FakeTime().sleep)
            assert (await factory.api.get_health()).ok
    assert seen == ["/api/v2/health"]
    assert fake.requests == []


async def test_interpreter_options() -> None:
    fake = FakeFactory(recording=recording("joined-mid-move"))
    async with client(fake, observe=True) as factory:
        interpreter = factory.interpreter(layers="world", backfill=False, resume=False, precision=None)
        events = [e async for e in interpreter.events()]
    assert events
    params = fake.requests[0].url.params
    assert params["observe"] == "1"
    assert params["layers"] == "world"
    assert "precision" not in params
    assert fake.viewers == 0

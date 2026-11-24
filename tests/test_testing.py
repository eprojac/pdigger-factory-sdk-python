"""The fake server itself: what users rely on in their own tests (SDK-DESIGN.md section 12)."""

from __future__ import annotations

import asyncio
import gzip
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from pdigger_factory_sdk import ClockItem, FactoryClient, FactoryError, FrameItem, GapItem
from pdigger_factory_sdk.testing import (
    CONNECT_S,
    KEEP_PICTURES,
    LIVE_CAMERA,
    LIVE_LAG_S,
    FakeExhausted,
    FakeFactory,
    FakeTime,
    Reply,
)

from .support import API, BASE_URL, api_json, client, recording, recording_lines, recording_name, recordings
from .synth import HEADER, clock_line, ev, frame, stone, worker_line

FULL = "dry1-0296-0306-full"
JOINED = "joined-mid-move"
"""Its camera's first run: g 16 (t 3.6911) to 21 (t 4.688311) are released, the 16 before them warm it up."""


async def raw(fake: FakeFactory, method: str, path: str, **kw: Any) -> httpx.Response:
    async with httpx.AsyncClient(transport=fake, base_url=BASE_URL) as http:
        return await http.request(method, path, **kw)


async def post(fake: FakeFactory, path: str, body: Any = None) -> httpx.Response:
    return await raw(fake, "POST", path, json={} if body is None else body)


def code(response: httpx.Response) -> str:
    return str(response.json()["code"])


def refusal(response: httpx.Response) -> tuple[int, str, str | None, str]:
    """An error reply's status, code, field and message."""
    body = response.json()
    return response.status_code, str(body["code"]), body.get("field"), str(body["error"])


def sse_events(text: str) -> list[tuple[str, Any]]:
    """The events of an SSE body, decoded: `(event, data)`."""
    return [(e, json.loads(d)) for e, d in re.findall(r"^event: (\w+)\ndata: (.*)$", text, re.M)]


async def state_of(fake: FakeFactory) -> Any:
    return (await raw(fake, "GET", "/api/v2/session")).json()["state"]


async def test_the_docstring_example() -> None:
    fake = FakeFactory(recording=recording("dry1-0000-0130"))
    lifts = 0
    async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep) as factory:
        with pytest.raises(FakeExhausted):
            async for event in factory.interpreter().events():
                lifts += event.type == "lift"
    assert lifts == 9


def test_reply_and_time() -> None:
    assert Reply(body={"a": 1}).json() == {"a": 1}
    assert Reply(body="not json").json() is None
    assert Reply(status=404, body={"a": 1}).json() is None
    assert Reply(body=b"x").content() == b"x"
    t = FakeTime(wall=10.0, monotonic=1.0)
    t.advance(2.0)
    assert (t.time(), t.monotonic()) == (12.0, 3.0)


@pytest.mark.parametrize("path", [pytest.param(p, id=recording_name(p)) for p in recordings()])
async def test_the_fake_clock_starts_where_the_recording_plays(path: Path) -> None:
    """The fake's state and its frames agree: the shared playhead is at the first frame, or at the anchor (paused, or
    playing with the first frame during its hold, before the anchor: `dry2-pause-seek`)."""
    frames = [x for x in recording_lines(recording_name(path)) if "g" in x and "cursor" in x]
    fake = FakeFactory(recording=path)
    async with client(fake) as factory:
        shared = await factory.session.clock()
    now, state = shared.media_time_now(), shared.state
    start = max(frames[0]["t"], state.anchor_media) if state.playing else state.anchor_media
    assert now == pytest.approx(start, abs=1e-6)
    assert frames[0]["t"] - 2.5 <= now <= frames[-1]["t"]
    assert shared.offset == 0.0  # the client reads the fake's clock


@pytest.mark.parametrize(
    ("lines", "replies", "wall"),
    [
        ([HEADER, clock_line(), frame(5)], None, 1001.0 + 1.0 / 0.8),  # playing: the playhead at g 5 (t 1.0)
        ([HEADER, clock_line(playing=False), frame(5)], None, 1000.0),  # paused: the line's server_now
        ([HEADER, clock_line(session=2), frame(5)], None, 1000.0),  # the first frame is of another session
        ([HEADER, clock_line(rate=0.0), frame(5)], None, 1000.0),
        ([HEADER, clock_line(hold_until=None), frame(5)], None, 1000.0),
        ([HEADER, clock_line()], None, 1000.0),  # no frames
        ([HEADER, clock_line(server_now=None), frame(5, session=2)], {"getSession": {"server_now": 7.5}}, 7.5),
        ([HEADER, frame(5)], {"getSession": {"server_now": "soon"}}, FakeTime().time()),
        ([], None, FakeTime().time()),
    ],
)
def test_where_the_fake_clock_starts(lines: list[dict[str, Any]], replies: dict[str, Any] | None, wall: float) -> None:
    assert FakeFactory(recording=lines, replies=replies).time.time() == pytest.approx(wall)
    assert FakeFactory(recording=lines, replies=replies, time=FakeTime(wall=5.0)).time.time() == 5.0


async def test_the_fake_serves_the_playhead_of_its_clock() -> None:
    fake = FakeFactory(recording=recording("aug28-1270-1300"))
    async with client(fake) as factory:
        assert (await factory.session.state()).media_time == pytest.approx(1270.0)
        await fake.time.sleep(10.0)  # 10 s on the wall clock: 8 s of media at rate 0.8
        assert (await factory.session.state()).media_time == pytest.approx(1278.0)
        paused = await factory.session.pause()
        assert paused.anchor_media == pytest.approx(1278.0)
        assert paused.media_time == pytest.approx(1278.0)
        await fake.time.sleep(5.0)
        assert (await factory.session.state()).media_time == pytest.approx(1278.0)  # it holds while paused
        await factory.session.play()
        await fake.time.sleep(5.0)
        assert (await factory.session.state()).media_time == pytest.approx(1282.0)
        assert (await factory.session.clock()).media_time_now() == pytest.approx(1282.0)


async def test_the_served_playhead_ends_at_the_duration_and_needs_numbers() -> None:
    ending = FakeFactory(recording=[HEADER, clock_line(duration=1.5), frame(5)])
    await ending.time.sleep(10.0)
    assert (await raw(ending, "GET", "/api/v2/session")).json()["state"]["media_time"] == 1.5
    odd = FakeFactory(recording=[HEADER, clock_line(hold_until=None, media_time=0.25), frame(5)])
    assert (await raw(odd, "GET", "/api/v2/session")).json()["state"]["media_time"] == 0.25  # kept as it was


async def test_a_recording_without_clock_lines_plays_from_the_start() -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(10)])  # its source is the video its frames name (hold 2.2)
    start = await state_of(fake)
    assert (start["hold_until"], start["media_time"]) == (start["anchor_wall"], 0.0)  # no select's hold
    fake.time.advance(2.0)
    assert (await state_of(fake))["media_time"] == pytest.approx(1.6)  # 2 s at rate 0.8


async def test_fake_time_sleep_never_goes_back() -> None:
    t = FakeTime()
    await t.sleep(-1.0)
    assert t.slept == [-1.0]
    assert t.time() == FakeTime().time()


def test_recording_files_plain_and_gzip(tmp_path: Path) -> None:
    lines = recording_lines("joined-mid-move")
    plain = tmp_path / "r.ndjson"
    plain.write_text("\n".join([*map(json.dumps, lines), "[1]", "nope"]), encoding="utf-8")
    zipped = tmp_path / "r.ndjson.gz"
    zipped.write_bytes(gzip.compress(plain.read_bytes()))
    for path in (plain, zipped):
        fake = FakeFactory(recording=path)
        assert fake.header is not None
        assert fake.has_recording
        assert fake.catalog[0]["id"] == "dry1"


async def test_routes_methods_and_missing_replies() -> None:
    fake = FakeFactory(recording=recording(FULL))
    assert code(await raw(fake, "POST", "/api/v2/health")) == "method_not_allowed"
    assert code(await raw(fake, "GET", "/nowhere")) == "not_found"
    assert code(await raw(fake, "GET", "/api/v2/topology")) == "not_found"  # no reply recorded
    root = (await raw(fake, "GET", "/api/v2")).json()
    assert root["build"] == "claude-20261122-hr4x1"
    model = (await raw(fake, "GET", "/api/v2/model")).json()
    assert model["checkpoint"]
    assert (await raw(fake, "GET", "/api/v2/health")).json()["ok"]
    assert (await raw(fake, "GET", "/api/v2/ready")).json()["ready"]
    assert (await raw(fake, "GET", "/api/v2/status")).json()["boot"] == "fakeb00t01"
    assert code(await raw(fake, "GET", "/api/v2/raw/tensors/1")) == "debug_disabled"
    assert (await raw(fake, "GET", "/api/v2/session/frames/1480/regions.png")).status_code == 404


async def test_catalog_etag_and_media() -> None:
    fake = FakeFactory(recording=recording(FULL), replies=API, live=True)
    first = await raw(fake, "GET", "/api/v2/videos")
    assert [v["id"] for v in first.json()["videos"]] == ["dry1", LIVE_CAMERA]
    assert first.json()["default_video"] == "dry1"  # a recording, as on the unit, although the session shows the camera
    camera = first.json()["videos"][1]
    dry1 = first.json()["videos"][0]
    assert camera == {
        "id": LIVE_CAMERA,
        "title": "Live camera",
        "subtitle": "The recording, replayed as a live camera",
        "kind": "camera",
        "live": True,
        "seekable": False,
        "duration": None,
        "frames": None,
        "default_start": None,
        "fps": 5.0,  # dry1's grid frame rate: 10.03 / 2, to 0.1
        "grid_step": 1,
        "hold": 3.0,
        "proxy": "",
        "proxy_wh": dry1["proxy_wh"],
        "native_wh": dry1["native_wh"],
        "held_out": [],
        "station": "FakeFactory",
    }
    assert (await raw(fake, "GET", f"/api/v2/videos/{LIVE_CAMERA}")).json() == camera
    again = await raw(fake, "GET", "/api/v2/videos", headers={"If-None-Match": first.headers["ETag"]})
    assert again.status_code == 304
    assert code(await raw(fake, "GET", "/api/v2/videos/nope/media")) == "unknown_video"
    assert (await raw(fake, "GET", "/api/v2/videos/dry1/media")).status_code == 206
    media = await raw(fake, "GET", f"/api/v2/videos/{LIVE_CAMERA}/media")
    proxy = f"{LIVE_CAMERA} is a live camera: it has no proxy video; each frame's picture is "
    assert refusal(media) == (404, "not_found", "id", proxy + "/api/v2/session/frames/{g}/image.jpg")
    assert (await raw(fake, "HEAD", f"/api/v2/videos/{LIVE_CAMERA}/media")).status_code == 404
    alone = await raw(FakeFactory(live=True), "GET", "/api/v2/videos")
    assert alone.json()["default_video"] == LIVE_CAMERA  # never empty: the camera of a fake without a recording
    sized = alone.json()["videos"][0]
    assert (sized["fps"], sized["proxy_wh"], sized["native_wh"]) == (5.0, [960, 540], [1920, 1080])


@pytest.mark.parametrize(
    ("like", "fps", "proxy_wh"),
    [
        ({"id": "r", "fps": 10}, 10.0, [960, 540]),  # no grid step: 1
        ({"id": "r", "grid_step": 2}, 5.0, [960, 540]),  # no fps: 5.0
        ({"id": "r", "fps": 10, "grid_step": 2, "proxy_wh": None}, 5.0, [960, 540]),  # only a list of sizes is copied
        ({"id": "r", "fps": 0.25, "grid_step": True}, 0.3, [960, 540]),  # halves away from zero, not to even (0.2)
        ({"id": "r", "fps": 30, "grid_step": 6, "proxy_wh": [640, 360]}, 5.0, [640, 360]),
    ],
)
def test_the_camera_is_sized_like_the_first_source(like: dict[str, Any], fps: float, proxy_wh: list[int]) -> None:
    camera = FakeFactory(replies={"listVideos": {"videos": [like]}}, live=True).catalog[-1]
    assert (camera["id"], camera["fps"], camera["proxy_wh"], camera["native_wh"]) == (
        LIVE_CAMERA,
        fps,
        proxy_wh,
        [1920, 1080],
    )


async def test_a_cameras_pictures() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    picture = await raw(fake, "GET", "/api/v2/session/frames/latest/image.jpg")
    assert (picture.status_code, picture.headers["Content-Type"]) == (200, "image/jpeg")
    assert (picture.headers["Cache-Control"], picture.headers["X-Interp-G"]) == ("no-store", "21")
    assert int(picture.headers["Content-Length"]) == len(picture.content)
    assert picture.content.startswith(b"\xff\xd8\xff\xfe")  # a comment segment right after SOI
    assert f"FakeFactory {LIVE_CAMERA} g 21".encode() in picture.content
    session = fake.state["session"]
    first = await raw(fake, "GET", "/api/v2/session/frames/16/image.jpg", params={"session": session})
    assert first.headers["X-Interp-G"] == "16"
    warm_up = await raw(fake, "GET", "/api/v2/session/frames/15/image.jpg")  # a warm-up frame: never released
    assert refusal(warm_up) == (404, "not_retained", "g", "frame 15 is not retained")
    assert warm_up.json()["retained"] == {"session": session, "g0": 16, "g1": 21, "t0": 3.6911, "t1": 4.688311, "n": 6}
    assert (await raw(fake, "POST", "/api/v2/session/frames/latest/image.jpg")).status_code == 405
    without = FakeFactory(recording=recording(FULL))
    recorded = await raw(without, "GET", "/api/v2/session/frames/latest/image.jpg")
    assert refusal(recorded) == (404, "not_found", "g", "dry1 is a recording: its picture is the proxy video")
    session = without.state["session"]
    stale = await raw(without, "GET", f"/api/v2/session/frames/latest/image.jpg?session={session - 1}")
    assert (stale.status_code, code(stale)) == (410, "session_gone")  # before the recording's 404, as on the unit
    same = await raw(without, "GET", f"/api/v2/session/frames/latest/image.jpg?session={session}")
    assert code(same) == "not_found"


async def test_only_the_newest_pictures_are_kept() -> None:
    fake = FakeFactory(recording=recording("aug28-1270-1300"), live=True)
    retained = (await raw(fake, "GET", "/api/v2/status")).json()["retained"]
    assert (retained["g0"], retained["g1"], retained["n"]) == (15, 150, 136)  # the camera's run: g 15 to 150
    kept, gone = retained["g1"] - KEEP_PICTURES + 1, retained["g1"] - KEEP_PICTURES
    picture = await raw(fake, "GET", f"/api/v2/session/frames/{kept}/image.jpg")
    assert (picture.status_code, picture.headers["X-Interp-G"]) == (200, str(kept))
    old = await raw(fake, "GET", f"/api/v2/session/frames/{gone}/image.jpg")
    message = f"the picture of frame {gone} is no longer kept: only the newest 64 pictures are"
    assert refusal(old) == (404, "not_retained", "g", message)
    assert "retained" not in old.json()  # as on the unit: without a retained window
    assert (await raw(fake, "GET", f"/api/v2/session/frames/{gone}")).status_code == 200  # the frame is still retained


@pytest.mark.parametrize("route", ["/api/v2/session/frames/{g}", "/api/v2/session/frames/{g}/image.jpg"])
async def test_frame_routes_check_g_and_session_as_the_unit(route: str) -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)
    session = fake.state["session"]
    for g in ("x", "-1", "12345678901"):  # an integer of 1 to 10 digits, or latest
        assert refusal(await raw(fake, "GET", route.format(g=g))) == (
            400,
            "invalid_param",
            "g",
            "g must be an integer or latest",
        )
    for guard in ("abc", "1.5", str(2**63)):
        refused = await raw(fake, "GET", route.format(g="latest"), params={"session": guard})
        assert refusal(refused) == (400, "invalid_param", "session", "session must be an integer")
    stale = await raw(fake, "GET", route.format(g="latest"), params={"session": f"+{session - 1}"})
    assert refusal(stale) == (410, "session_gone", "session", f"session {session - 1} has ended")
    for guard in ("", str(session)):
        assert (await raw(fake, "GET", route.format(g="latest"), params={"session": guard})).status_code == 200
    assert (await raw(fake, "GET", route.format(g="0016"))).status_code == 200  # 16


async def test_the_frame_routes_check_the_session_first_and_the_picture_g_first() -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)
    stale = {"session": fake.state["session"] - 1}
    assert code(await raw(fake, "GET", "/api/v2/session/frames/x", params=stale)) == "session_gone"
    assert code(await raw(fake, "GET", "/api/v2/raw/packets/x", params=stale)) == "session_gone"
    assert code(await raw(fake, "GET", "/api/v2/raw/packets/x")) == "invalid_param"
    assert code(await raw(fake, "GET", "/api/v2/session/frames/x/image.jpg", params=stale)) == "invalid_param"


async def test_a_stream_waits_out_a_cameras_warm_up() -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)
    async with client(fake) as factory:
        watched = await factory.session.watch(LIVE_CAMERA)
        assert watched.live is not None
        frames = [i.frame async for i in factory.streams.frames(resume=False) if isinstance(i, FrameItem)]
        live = await factory.session.state()
    assert frames
    assert {f.session for f in frames} == {watched.session}
    assert frames[0].t >= CONNECT_S + 3.0  # the first 3 s of the camera's frames only warmed the interpreter up
    went_live = watched.live.since + frames[0].t + LIVE_LAG_S
    assert fake.time.time() == pytest.approx(went_live)  # the stream waited for it on the fake's clock
    assert live.live is not None
    assert live.live.phase == "live"
    assert live.hold_until == pytest.approx(went_live)


async def test_a_stream_that_sees_a_camera_selected_gets_its_phases() -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)
    async with client(fake) as factory:
        clocks: list[str] = []
        seen = 0
        async for item in factory.streams.frames(resume=False):
            if isinstance(item, ClockItem) and item.state.live is not None:
                clocks.append(item.state.live.phase)
            if isinstance(item, FrameItem):
                seen += 1
                if seen == 2:
                    await factory.api.session_select(LIVE_CAMERA)  # a new session of the camera, from the start
    assert clocks == ["connecting", "warming", "live"]


async def test_nothing_is_retained_before_a_camera_is_live() -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)
    async with client(fake) as factory:
        watched = await factory.session.watch(LIVE_CAMERA)
        for call in (factory.api.get_frame("latest"), factory.session.picture()):
            with pytest.raises(FactoryError) as info:
                await call
            assert (info.value.status, info.value.code, info.value.message) == (
                404,
                "not_retained",
                "no frame is retained yet",
            )
    empty = {"session": watched.session, "g0": None, "g1": None, "t0": None, "t1": None, "n": 0}
    assert (await raw(fake, "GET", "/api/v2/session/frames/latest")).json()["retained"] == empty
    status = (await raw(fake, "GET", "/api/v2/status")).json()
    assert (status["retained"], status["long_polls"], status["streams"]) == (empty, 0, 0)
    assert refusal(await raw(fake, "GET", "/api/v2/session/frames/16")) == (
        404,
        "not_retained",
        "g",
        "frame 16 is not retained",
    )


async def test_a_select_of_the_camera_connects_then_warms_up_and_goes_live() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    start = await state_of(fake)
    assert (start["revision"], start["changed_by"], fake.live_phase()) == (3, "start", "live")
    selected = (await post(fake, "/api/v2/session/select", {"video": LIVE_CAMERA})).json()["state"]
    since = fake.time.time()
    assert selected["live"] == {"phase": "connecting", "warmup": 3.0, "since": since, "lag": 0.0}
    assert (selected["session"], selected["revision"], selected["changed_by"]) == (start["session"] + 1, 4, "viewer")
    assert (selected["rate"], selected["duration"], selected["focus"], selected["media_time"]) == (1.0, None, None, 0.0)
    assert (selected["anchor_media"], selected["anchor_wall"], selected["hold_until"]) == (0.0, since, since + 3600)
    fake.advance(CONNECT_S / 2)
    assert fake.live_phase() == "connecting"
    fake.advance(CONNECT_S / 2)  # the camera's first frame: the interpreter warms up on it
    warming = await state_of(fake)
    assert (warming["live"]["phase"], warming["live"]["lag"], warming["revision"]) == ("warming", 0.0, 5)
    assert (warming["media_time"], warming["hold_until"]) == (0.0, since + 3600)  # the anchors are the select's
    fake.advance(3.0)
    assert fake.live_phase() == "warming"  # live with its first frame after the warm-up: t 3.6911, 0.25 s later
    fake.advance(1.0)
    live = await state_of(fake)
    went_live = since + 3.6911 + LIVE_LAG_S
    assert (live["live"]["phase"], live["live"]["lag"], live["revision"], live["anchor_media"]) == (
        "live",
        LIVE_LAG_S,
        6,
        3.6911,
    )
    assert live["anchor_wall"] == live["hold_until"] == pytest.approx(went_live)
    assert live["media_time"] == pytest.approx(fake.time.time() - since - LIVE_LAG_S)
    assert (live["changed_by"], live["playing"]) == ("viewer", True)
    frames = (await raw(fake, "GET", "/api/v2/session/frames", params={"limit": 16})).json()["frames"]
    assert [f["g"] for f in frames] == [16, 17, 18, 19, 20, 21]  # the whole run, released at once when it went live
    recorded = (await post(fake, "/api/v2/session/select", {"video": "dry1"})).json()["state"]
    assert ("live" in recorded, recorded["rate"], fake.live_phase()) == (False, 0.8, "")


async def test_a_seek_naming_a_recording_leaves_the_camera() -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)
    selected = (await post(fake, "/api/v2/session/select", {"video": LIVE_CAMERA})).json()["state"]
    sought = (await post(fake, "/api/v2/session/seek", {"video": "dry1", "time": 300})).json()["state"]
    recorded = ("dry1", 300, 0.8, 1807.157567)
    assert (sought["video"], sought["anchor_media"], sought["rate"], sought["duration"]) == recorded
    assert ("live" in sought, sought["playing"], sought["session"]) == (False, True, selected["session"] + 1)
    assert sought["hold_until"] - sought["anchor_wall"] == pytest.approx(2.2)  # the recording's hold
    fake.advance(10.0)  # the camera's phases are gone with its session
    assert (fake.live_phase(), fake.state["revision"]) == ("", sought["revision"])
    picture = await raw(fake, "GET", "/api/v2/session/frames/latest/image.jpg")
    assert refusal(picture) == (404, "not_found", "g", "dry1 is a recording: its picture is the proxy video")
    latest = (await raw(fake, "GET", "/api/v2/session/frames/latest")).json()
    assert (latest["video"], latest["session"], latest["t"] >= 300) == ("dry1", sought["session"], True)


async def test_a_paused_camera_takes_no_phase() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    await post(fake, "/api/v2/session/select", {"video": LIVE_CAMERA})
    paused = (await post(fake, "/api/v2/session/pause")).json()["state"]
    assert (paused["revision"], paused["live"]["phase"], paused["anchor_media"]) == (5, "connecting", 0.0)
    fake.advance(10.0)
    assert fake.live_phase() == "connecting"
    poll = (await raw(fake, "GET", "/api/v2/session/frames", params={"cursor": 0})).json()
    assert (poll["state"]["live"]["phase"], poll["frames"], poll["state"]["media_time"]) == ("connecting", [], 0.0)

    async def first_frame() -> Any:
        async with (
            httpx.AsyncClient(transport=fake, base_url=BASE_URL) as http,
            http.stream("GET", "/api/v2/session/frames/stream", params={"format": "ndjson"}) as response,
        ):
            async for line in response.aiter_lines():
                return json.loads(line)
        return None  # pragma: no cover - the stream waits for the play

    streams = [asyncio.create_task(first_frame()) for _ in range(2)]  # both wait for a change of the state
    for _ in range(20):
        await asyncio.sleep(0)
    assert not any(s.done() for s in streams)
    assert fake.live_phase() == "connecting"
    assert (await post(fake, "/api/v2/session/pause")).json()["state"]["revision"] == 5  # a repeat changes nothing
    played = (await post(fake, "/api/v2/session/play")).json()["state"]
    assert [(await s)["g"] for s in streams] == [16, 16]  # the camera's frames from g 0 again: 16 is the first shown
    assert fake.time.time() > played["anchor_wall"] + 3  # its phases came after the play
    assert fake.live_phase() == "live"


async def test_a_play_of_a_live_camera_warms_up_again_after_a_break() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    before = (await raw(fake, "GET", "/api/v2/session/frames/latest")).json()
    assert (before["g"], before["t"]) == (21, 4.688311)
    stone = (await raw(fake, "GET", "/api/v2/session/world")).json()["stones"][0]["id"]
    focused = (await raw(fake, "PUT", "/api/v2/session/focus", json={"stone_id": stone})).json()["state"]
    assert (focused["revision"], focused["changed_by"]) == (4, "viewer")
    paused = (await post(fake, "/api/v2/session/pause")).json()["state"]
    assert (paused["live"]["phase"], paused["anchor_media"]) == ("live", pytest.approx(3.6911))  # the live edge
    fake.advance(2.0)
    assert fake.live_phase() == "live"  # a pause keeps the phase
    played = (await post(fake, "/api/v2/session/play", {"video": "dry1", "time": 5})).json()["state"]  # body ignored
    assert played["video"] == LIVE_CAMERA
    assert played["live"] == {**paused["live"], "phase": "connecting"}  # in its own reply; since and lag kept
    assert (played["revision"], played["hold_until"]) == (paused["revision"] + 1, paused["hold_until"])
    edge = fake.time.time() - played["live"]["since"] - LIVE_LAG_S
    assert played["anchor_media"] == played["media_time"] == pytest.approx(edge)  # it plays on at the live edge
    fake.advance(CONNECT_S)
    warming = await state_of(fake)
    assert (warming["live"]["phase"], warming["revision"]) == ("warming", played["revision"] + 1)
    assert warming["live"]["lag"] == LIVE_LAG_S  # as on the unit, warming changes only the phase
    assert warming["media_time"] == pytest.approx(edge + CONNECT_S)  # the playhead runs on through the warm-up
    fake.advance(30.0)
    again = await state_of(fake)
    live_again = ("live", LIVE_LAG_S, played["revision"] + 2)
    assert (again["live"]["phase"], again["live"]["lag"], again["revision"]) == live_again
    assert again["anchor_wall"] == again["hold_until"] > played["anchor_wall"]
    assert again["focus"] is not None  # kept through the phases
    g0 = before["g"] + 1 + 4  # the run after the break: g jumps by 5
    for g in (before["g"] + 1, g0, g0 + 15):  # its first 16 frames warm the interpreter up again
        assert code(await raw(fake, "GET", f"/api/v2/session/frames/{g}")) == "not_retained"
    first = (await raw(fake, "GET", f"/api/v2/session/frames/{g0 + 16}")).json()
    assert first["t"] == again["anchor_media"]  # the playhead starts at its first frame shown
    assert first["t"] > before["t"] + 2.0  # after the frames sent already, from the play on
    assert (await raw(fake, "GET", "/api/v2/session/frames/latest")).json()["g"] > g0 + 16
    assert (await raw(fake, "GET", "/api/v2/session/frames/16/image.jpg")).status_code == 200  # before the break
    repeat = (await post(fake, "/api/v2/session/play")).json()["state"]  # a play while playing changes nothing
    assert (repeat["revision"], repeat["live"]["phase"]) == (again["revision"], "live")
    fake.advance(30.0)
    assert ((await state_of(fake))["revision"], fake.live_phase()) == (again["revision"], "live")


async def test_a_play_before_the_camera_is_live_plans_its_phases_again() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    await post(fake, "/api/v2/session/select", {"video": LIVE_CAMERA})
    await post(fake, "/api/v2/session/pause")
    fake.advance(10.0)
    played = (await post(fake, "/api/v2/session/play")).json()["state"]
    assert (played["live"]["phase"], played["media_time"], played["anchor_media"]) == ("connecting", 0.0, 0.0)
    fake.advance(CONNECT_S)
    assert fake.live_phase() == "warming"
    fake.advance(3.5)
    after = await state_of(fake)
    assert (after["live"]["phase"], after["playing"], after["revision"]) == ("live", True, played["revision"] + 2)
    assert after["anchor_wall"] > played["anchor_wall"] + 3
    assert (await raw(fake, "GET", "/api/v2/session/frames/latest")).json()["g"] == 21  # numbered from 0 again
    await post(fake, "/api/v2/session/pause")
    await post(fake, "/api/v2/session/select", {"video": LIVE_CAMERA})  # a select keeps it paused
    await post(fake, "/api/v2/session/play")
    fake.advance(5.0)
    assert (fake.live_phase(), fake.state["playing"]) == ("live", True)


async def test_every_change_is_a_clock_event() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True, on_exhausted="end")
    async with client(fake) as factory:
        stone = (await factory.api.get_world()).stones[0].id
        stream = await factory.api.stream_frames()
        events: list[tuple[str, Any]] = []
        async for message in stream:
            events.append((message.event, message.json()))
            if len(events) == 1:  # the first frame
                await factory.session.pause()
                await factory.session.pause()  # a repeat: no change
                await factory.session.focus(stone)
                await factory.session.clear_focus()
                await factory.session.play()
    clocks = [data for event, data in events if event == "clock"]
    assert [(c["revision"], c["playing"], c["live"]["phase"]) for c in clocks] == [
        (4, False, "live"),
        (5, False, "live"),
        (6, False, "live"),
        (7, True, "connecting"),
        (8, True, "warming"),
        (9, True, "live"),
    ]
    assert [c["focus"] is not None for c in clocks] == [False, True, False, False, False, False]
    assert all(c["server_now"] >= clocks[3]["server_now"] for c in clocks[3:])  # the phases come after the play
    frames = [data["g"] for event, data in events if event == "frame"]
    assert frames[:6] == [16, 17, 18, 19, 20, 21]
    assert frames[6] == 21 + 1 + 4 + 16  # the run after the play
    ndjson = await raw(fake, "GET", "/api/v2/session/frames/stream", params={"format": "ndjson"})
    assert all("g" in json.loads(line) for line in ndjson.text.splitlines())  # NDJSON carries no clock event


async def test_a_stream_after_a_select_gets_the_cameras_phases_as_clock_events() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    selected = (await post(fake, "/api/v2/session/select", {"video": LIVE_CAMERA})).json()["state"]
    since = selected["live"]["since"]
    stream = await raw(fake, "GET", "/api/v2/session/frames/stream", params={"limit": 1, "layers": "world"})
    events = sse_events(stream.text)
    assert [(e, d["live"]["phase"]) for e, d in events if e == "clock"] == [("clock", "warming"), ("clock", "live")]
    assert [d["server_now"] - since for e, d in events if e == "clock"] == [CONNECT_S, pytest.approx(3.9411)]
    assert (events[-1][0], events[-1][1]["g"], events[-1][1]["session"]) == ("frame", 16, selected["session"])
    assert fake.time.time() == pytest.approx(since + 3.6911 + LIVE_LAG_S)  # the stream moved the clock on to each


async def test_a_long_poll_takes_the_phases_within_its_wait() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    since = (await post(fake, "/api/v2/session/select", {"video": LIVE_CAMERA})).json()["state"]["live"]["since"]
    refused = await raw(fake, "GET", "/api/v2/session/frames", params={"wait": 5000})
    assert refusal(refused) == (400, "invalid_param", "wait", "wait must be within 0..1000 ms")
    poll = (await raw(fake, "GET", "/api/v2/session/frames", params={"cursor": 0, "wait": 0})).json()
    assert (poll["state"]["live"]["phase"], fake.time.time()) == ("connecting", since)
    poll = (await raw(fake, "GET", "/api/v2/session/frames", params={"cursor": 0, "layers": "world"})).json()
    assert (poll["state"]["live"]["phase"], poll["frames"], fake.time.time()) == ("warming", [], since + CONNECT_S)
    fake.advance(3.0)
    poll = (await raw(fake, "GET", "/api/v2/session/frames", params={"cursor": 0})).json()
    assert (poll["state"]["live"]["phase"], [f["g"] for f in poll["frames"]]) == ("live", [16, 17, 18, 19, 20, 21])
    assert fake.time.time() == pytest.approx(since + 3.6911 + LIVE_LAG_S)
    assert poll["cursor"] == poll["frames"][-1]["cursor"]


async def test_camera_frames_are_numbered_as_the_unit_numbers_them() -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)
    frame_ = (await raw(fake, "GET", "/api/v2/session/frames/latest", params={"layers": "raw,overlay"})).json()
    layer, overlay = frame_["raw"], frame_["overlay"]
    packet = (await raw(fake, "GET", "/api/v2/raw/packets/latest")).json()
    assert frame_["i"] == frame_["g"] == layer["g"] == layer["i"] == packet["g"] == packet["i"] == 50
    assert frame_["t"] == layer["t"] == packet["t"]
    assert frame_["cursor"] == layer["cursor"] == packet["cursor"]
    assert (layer["video"], packet["video"], layer["session"], packet["session"]) == (
        LIVE_CAMERA,
        LIVE_CAMERA,
        frame_["session"],
        frame_["session"],
    )
    assert overlay["prev_g"] == frame_["g"] - 1
    stream = await raw(fake, "GET", "/api/v2/raw/stream", params={"limit": 1}, headers={"Accept": "text/event-stream"})
    ((event_id, data),) = re.findall(r"^id: (\S+)\nevent: frame\ndata: (.*)$", stream.text, re.M)
    first = json.loads(data)
    assert (first["g"], first["i"], event_id) == (16, 16, f"fakeb00t01-{first['cursor']}")
    joined = FakeFactory(recording=recording(JOINED), live=True)
    missing = await raw(joined, "GET", "/api/v2/raw/packets/latest")  # its frames have no raw layer
    assert refusal(missing) == (404, "not_retained", "g", "frame latest is not retained")
    assert missing.json()["retained"]["n"] == 6
    served = (await raw(joined, "GET", "/api/v2/session/frames/latest")).json()["cursor"]
    await post(joined, "/api/v2/session/select", {"video": LIVE_CAMERA})
    sse = await raw(joined, "GET", "/api/v2/session/frames/stream", params={"limit": 2})
    ids = re.findall(r"^id: fakeb00t01-(\d+)\nevent: frame\ndata: (.*)$", sse.text, re.M)
    assert [int(n) for n, _ in ids] == [json.loads(d)["cursor"] for _, d in ids]  # new cursors, given at the release
    assert served < int(ids[0][0]) < int(ids[1][0])


async def test_a_camera_recording_shows_as_a_recording_until_it_is_selected() -> None:
    camera = {"id": "cam9", "kind": "camera", "live": True, "seekable": False, "duration": None}
    lines: list[dict[str, Any]] = [
        {"recording": "pfr1", "source": camera},
        {"cursor": 1, "session": 5, "video": "cam9", "g": 7, "t": 0.0},
        {"cursor": 2, "session": 5, "video": "cam9", "g": 8, "t": 3.2},
        {"cursor": 3, "session": 6, "video": "cam9", "g": 0, "t": 0.4},
    ]
    fake = FakeFactory(recording=lines)
    assert ("live" in fake.state, fake.state["rate"], fake.live_phase()) == (False, 0.8, "")
    assert (await raw(fake, "GET", "/api/v2/session/frames/latest")).json()["g"] == 8  # the recorded frames
    picture = await raw(fake, "GET", "/api/v2/session/frames/latest/image.jpg")
    assert refusal(picture) == (404, "not_found", "g", "cam9 is a recording: its picture is the proxy video")
    await post(fake, "/api/v2/session/select", {"video": "cam9"})
    assert fake.state["session"] == 7
    stream = await raw(fake, "GET", "/api/v2/session/frames/stream", params={"format": "ndjson", "limit": 2})
    one, two = (json.loads(line) for line in stream.text.splitlines())
    # its sessions end to end: g 0 (t 0.5) and g 1 (t 3.7) of session 5, then g 2 (t 3.9) of session 6
    assert (one["g"], one["t"], two["g"], two["t"], one["video"], one["i"]) == (1, 3.7, 2, 3.9, "cam9", 1)


async def test_a_cameras_next_run_after_it_was_served() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    async with client(fake) as factory:
        stream = await factory.api.stream_frames()
        last = [m async for m in stream if m.event == "frame"][-1]
        assert last.json()["g"] == 21
        with pytest.raises(FakeExhausted):
            await factory.api.stream_frames(last_event_id=last.id)  # the run was served
        await factory.session.pause()
        await factory.session.play()
        again = await factory.api.stream_frames(last_event_id=last.id)  # the next run comes, after a warm-up
        frames = [m.json()["g"] async for m in again if m.event == "frame"]
    assert frames[0] == 21 + 1 + 4 + 16


async def test_a_stream_resumed_between_a_cameras_runs_gets_the_next() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    await post(fake, "/api/v2/session/pause")
    await post(fake, "/api/v2/session/play")  # it connects again: its next run is to come
    fake.drop_after(6)
    async with client(fake) as factory:
        stream = await factory.api.stream_frames()
        seen = []
        with pytest.raises(FactoryError) as info:
            async for message in stream:
                seen.append(message)
        assert (info.value.code, seen[-1].json()["g"]) == ("transport", 21)
        assert (fake.exhausted, fake.live_phase()) == (True, "connecting")  # the run served, the next one to come
        again = await factory.api.stream_frames(last_event_id=seen[-1].id)
        frames = [m.json()["g"] async for m in again if m.event == "frame"]
    assert frames[0] == 21 + 1 + 4 + 16


async def test_dry_runs_move_nothing() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    dry = (await post(fake, "/api/v2/session/select?dry_run=1", {"video": LIVE_CAMERA})).json()
    assert (dry["dry_run"], dry["state"]["live"]["phase"]) == (True, "connecting")
    assert dry["state"]["session"] == fake.state["session"] + 1
    assert fake.live_phase() == "live"
    paused = (await post(fake, "/api/v2/session/pause")).json()
    assert (paused["dry_run"], paused["state"]["revision"]) == (False, 4)
    fake.advance(2.0)
    played = (await post(fake, "/api/v2/session/play?dry_run=1")).json()["state"]
    assert (played["live"]["phase"], played["playing"]) == ("connecting", True)
    assert played["anchor_media"] == pytest.approx(fake.time.time() - played["live"]["since"] - LIVE_LAG_S)
    assert (fake.live_phase(), fake.state["playing"], fake.state["revision"]) == ("live", False, 4)
    stream = await raw(fake, "GET", "/api/v2/session/frames/stream")
    assert [(e, d.get("g")) for e, d in sse_events(stream.text)] == [
        *[("frame", g) for g in range(16, 22)],
        ("clock", None),  # the pause's, and nothing of the dry runs
    ]


async def test_the_world_before_a_camera_is_live() -> None:
    fake = FakeFactory(recording=recording(JOINED), live=True)
    stone = (await raw(fake, "GET", "/api/v2/session/world")).json()["stones"][0]["id"]
    await post(fake, "/api/v2/session/select", {"video": LIVE_CAMERA})
    unavailable = {"error": "the Stable sim is not running", "code": "world_unavailable", "retry_ms": 1000}
    world = await raw(fake, "GET", "/api/v2/session/world")
    assert (world.status_code, world.json()) == (503, unavailable)
    focus = await raw(fake, "PUT", "/api/v2/session/focus", json={"stone_id": stone})
    assert (focus.status_code, focus.json()) == (503, unavailable)
    events = (await raw(fake, "GET", "/api/v2/session/world/events")).json()
    assert (events["events"], "oldest_g" in events) == ([], False)
    fake.advance(5.0)
    latest = (await raw(fake, "GET", "/api/v2/session/frames/latest")).json()
    world = await raw(fake, "GET", "/api/v2/session/world")
    assert (world.json()["g"], world.json()["video"], world.json()["session"]) == (21, LIVE_CAMERA, latest["session"])
    assert (await raw(fake, "GET", "/api/v2/session/world/events")).json()["oldest_g"] == 16
    assert (await raw(fake, "PUT", "/api/v2/session/focus", json={"stone_id": stone})).status_code == 200


@pytest.mark.parametrize("lines", [None, [{"recording": "pfr1"}]], ids=["nothing", "header"])
async def test_a_fake_with_only_its_camera_answers_from_it(lines: list[dict[str, Any]] | None) -> None:
    fake = FakeFactory(recording=lines, live=True)  # live with no frames: nothing is retained, not a recorded reply
    empty = {"session": 1, "g0": None, "g1": None, "t0": None, "t1": None, "n": 0}
    for path in ("/api/v2/session/frames/latest", "/api/v2/raw/packets/latest"):
        refused = await raw(fake, "GET", path)
        assert refusal(refused) == (404, "not_retained", "g", "no frame is retained yet")
        assert refused.json()["retained"] == empty
    events = await raw(fake, "GET", "/api/v2/session/world/events")
    assert (events.status_code, events.json()["events"], "oldest_g" in events.json()) == (200, [], False)
    poll = await raw(fake, "GET", "/api/v2/session/frames", params={"wait": 0})
    assert (poll.status_code, poll.json()["state"]["live"]["phase"], poll.json()["frames"]) == (200, "live", [])
    assert fake.has_recording


async def test_the_worker_status() -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(3)])  # no worker line: running, in the session shown
    worker = {"state": "running", "reason": "", "session": 1, "video": "dry1"}
    assert (await raw(fake, "GET", "/api/v2/session")).json()["worker"] == worker
    await post(fake, "/api/v2/session/select", {"video": "dry1"})  # the video its frames name is in the catalog
    assert (await raw(fake, "GET", "/api/v2/status")).json()["worker"] == {**worker, "session": 2}
    poll = (await raw(fake, "GET", "/api/v2/session/frames", params={"wait": 0})).json()
    assert poll["worker"] == {**worker, "session": 2}
    camera = FakeFactory(live=True)
    assert (await raw(camera, "GET", "/api/v2/session")).json()["worker"]["video"] == LIVE_CAMERA
    idle = FakeFactory(recording=[worker_line("idle", "nobody is watching"), frame(0)])
    idled = {"state": "idle", "reason": "nobody is watching"}
    assert (await raw(idle, "GET", "/api/v2/session")).json()["worker"] == idled
    assert (await raw(idle, "GET", "/api/v2/health")).json()["worker"] == "running"  # the fake's worker never stops


async def test_sources_without_duration_or_hold() -> None:
    fake = FakeFactory(recording=[{"recording": "pfr1", "source": {"id": "v"}}, {"cursor": 1, "g": 1, "t": 0.2}])
    refused = await post(fake, "/api/v2/session/seek", {"time": -1})
    assert refusal(refused) == (400, "time_out_of_range", "time", "time must be at least 0 s")
    selected = (await post(fake, "/api/v2/session/select", {"video": "v"})).json()["state"]
    assert (selected["session"], selected["hold_until"] - selected["anchor_wall"]) == (2, pytest.approx(1.4))
    assert (await raw(fake, "GET", "/api/v2/session/frames/latest")).json()["g"] == 1  # the source's, without a video
    plain = FakeFactory(recording=[{"recording": "pfr1", "source": {"id": "v", "duration": 5}}, {"cursor": 1, "g": 1}])
    assert (plain.state["session"], plain.state["video"]) == (1, "v")  # frames without a session: session 1
    camera = FakeFactory(recording=[{"cursor": 1, "g": 1, "t": 0.2}, {"cursor": 2, "g": 2, "t": 0.4}], live=True)
    assert (camera.state["session"], camera.live_phase()) == (1, "live")
    latest = await raw(camera, "GET", "/api/v2/session/frames/latest/image.jpg")  # both are warm-up frames
    assert refusal(latest)[1:] == ("not_retained", "g", "no frame is retained yet")
    empty = FakeFactory()
    assert refusal(await post(empty, "/api/v2/session/seek", {"time": 1})) == (
        409,
        "not_seekable",
        "video",
        "the session shows no video",
    )
    assert code(await post(empty, "/api/v2/session/select", {"video": "v"})) == "unknown_video"


async def test_a_cameras_frames_without_a_session_are_one_segment_from_the_first() -> None:
    rig = FakeFactory(recording=[{"cursor": n + 1, "g": 100 + n, "t": 10.0 + 0.2 * n} for n in range(25)], live=True)
    latest = (await raw(rig, "GET", "/api/v2/session/frames/latest")).json()
    assert (latest["g"], latest["t"]) == (24, 5.3)  # numbered from the first frame on: g from 0, t from CONNECT_S


async def test_a_long_poll_waits_while_a_camera_warms_up() -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)
    async with client(fake) as factory:
        watched = await factory.session.watch(LIVE_CAMERA)
        reply = await factory.api.poll_frames(rev=watched.revision, wait=1000)  # nothing comes yet: it waits
        assert (reply.frames, reply.state.live.phase if reply.state.live else None) == ([], "warming")
        later = await factory.api.poll_frames(rev=reply.state.revision, wait=1000)
        while not later.frames:
            later = await factory.api.poll_frames(rev=later.state.revision, wait=1000)
    assert later.state.live is not None
    assert later.state.live.phase == "live"
    assert later.frames[0].session == watched.session


async def test_a_pause_before_the_camera_is_live_holds_its_phase() -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)
    async with client(fake) as factory:
        await factory.session.watch(LIVE_CAMERA)
        paused = await factory.session.pause()
        fake.time.advance(60.0)
        assert (await factory.session.state()).live == paused.live  # still connecting: nothing moves while paused
        played = await factory.session.play()
        assert played.live is not None
        assert played.live.phase == "connecting"  # the phases are planned again from the play
        live = await factory.session.until_live()
    assert live.live is not None
    assert (live.live.phase, live.playing, live.revision) == ("live", True, played.revision + 2)
    assert live.anchor_wall > played.anchor_wall + 3.0


async def test_world_events_and_stones_from_a_recording() -> None:
    lines = [
        HEADER,
        frame(1, stones=[stone(1)], events=[ev("new", 1), ev("confirm", 1)]),
        frame(2, stones=[stone(1, 0.1)], events=[ev("lift", 1, "left")]),
        frame(3, with_world=False),
        frame(4, stones=[stone(1, 0.2)], events=[ev("place", 1, "left")]),
    ]
    fake = FakeFactory(recording=lines)
    log = (await raw(fake, "GET", "/api/v2/session/world/events")).json()
    assert [e["type"] for e in log["events"]] == ["new", "confirm", "lift", "place"]
    assert (log["next_after"], log["oldest_g"]) == (4, 1)
    page = (await raw(fake, "GET", "/api/v2/session/world/events", params={"after": 1, "limit": 1})).json()
    assert [e["type"] for e in page["events"]] == ["lift"]
    assert page["next_after"] == 2
    typed = (await raw(fake, "GET", "/api/v2/session/world/events", params={"type": "place"})).json()
    assert [e["g"] for e in typed["events"]] == [4]
    assert code(await raw(fake, "GET", "/api/v2/session/world/events", params={"limit": "x"})) == "invalid_param"
    assert code(await raw(fake, "GET", "/api/v2/session/world/events", params={"limit": 0})) == "invalid_param"
    assert code(await raw(fake, "GET", "/api/v2/session/world/events", params={"type": "zap"})) == "invalid_param"
    track = (await raw(fake, "GET", "/api/v2/session/world/stones/1")).json()
    assert (track["first_g"], track["last_g"], len(track["path3d"]), track["g"]) == (1, 4, 3, 4)
    assert code(await raw(fake, "GET", "/api/v2/session/world/stones/x")) == "invalid_param"
    assert code(await raw(fake, "GET", "/api/v2/session/world/stones/9")) == "unknown_stone"
    assert (await raw(fake, "GET", "/api/v2/session/world")).json()["g"] == 4
    empty = FakeFactory(recording=[frame(1, with_world=False)])
    assert code(await raw(empty, "GET", "/api/v2/session/world")) == "world_unavailable"
    assert (await raw(empty, "GET", "/api/v2/session/world/events")).json()["events"] == []


async def test_frames_and_packets_from_a_recording() -> None:
    fake = FakeFactory(recording=recording(FULL))
    latest = (await raw(fake, "GET", "/api/v2/session/frames/latest", params={"layers": "world"})).json()
    assert set(latest) >= {"world", "g", "cursor"}
    assert "raw" not in latest
    one = (await raw(fake, "GET", f"/api/v2/session/frames/{latest['g'] - 1}")).json()
    assert one["g"] == latest["g"] - 1
    gone = await raw(fake, "GET", "/api/v2/session/frames/latest", params={"session": 1})
    assert gone.status_code == 410
    missing = await raw(fake, "GET", "/api/v2/session/frames/5")
    assert missing.status_code == 404
    assert missing.json()["retained"]["n"] == 51
    assert code(await raw(fake, "GET", "/api/v2/session/frames/abc")) == "invalid_param"
    assert code(await raw(fake, "GET", "/api/v2/session/frames/latest", params={"layers": "x"})) == "unknown_layer"
    assert code(await raw(fake, "GET", "/api/v2/raw/packets/abc")) == "invalid_param"
    packet = (await raw(fake, "GET", "/api/v2/raw/packets/latest")).json()
    assert packet["schema"] == "sip1"
    worldly = FakeFactory(recording=[frame(1)])
    assert code(await raw(worldly, "GET", "/api/v2/raw/packets/1")) == "not_retained"
    nothing = FakeFactory(recording=[])
    retained = (await raw(nothing, "GET", "/api/v2/session/frames/latest")).json()
    assert retained["code"] == "this FakeFactory has no reply getFrame-latest" or retained["code"] == "not_found"


async def test_replied_frames_without_a_recording() -> None:
    fake = FakeFactory(replies=API)
    latest = api_json("getFrame-latest")
    assert (await raw(fake, "GET", f"/api/v2/session/frames/{latest['g']}")).json()["g"] == latest["g"]
    assert (await raw(fake, "GET", "/api/v2/session/frames/1", params={"session": 1})).status_code == 410
    assert (await raw(fake, "GET", "/api/v2/session/frames/1")).status_code == 404
    bare = FakeFactory(replies={"getFrame-latest": latest})
    assert (await raw(bare, "GET", "/api/v2/session/frames/1", params={"session": 1})).status_code == 410
    assert code(await raw(bare, "GET", "/api/v2/session/frames/1")) == "not_retained"
    assert code(await raw(FakeFactory(), "GET", "/api/v2/session/frames/1")) == "not_found"
    assert code(await raw(FakeFactory(), "GET", "/api/v2/session/world")) == "world_unavailable"


async def test_rasters_and_renders() -> None:
    fake = FakeFactory(replies=API)
    assert code(await raw(fake, "GET", "/api/v2/session/frames/1/regions.png", params={"scale": 9})) == "invalid_param"
    assert code(await raw(fake, "GET", "/api/v2/session/frames/1/heat.png")) == "not_found"
    assert code(await raw(fake, "GET", "/api/v2/session/render/overlay2d")) == "invalid_param"
    bad_mode = await raw(fake, "GET", "/api/v2/session/render/overlay2d", params={"g": 1, "mode": "x"})
    assert bad_mode.json()["field"] == "mode"
    bad_surface = await raw(fake, "GET", "/api/v2/session/render/scene3d", params={"g": 1, "surface": "x"})
    assert bad_surface.json()["field"] == "surface"
    prim3d = await raw(fake, "GET", "/api/v2/session/render/scene3d", params={"g": 1, "draw": "primitives"})
    assert prim3d.status_code == 200


async def test_controls_are_validated_like_the_server() -> None:
    fake = FakeFactory(recording=recording(FULL), live=True)  # it shows its camera, live
    select, seek = "/api/v2/session/select", "/api/v2/session/seek"
    assert code(await raw(fake, "POST", select, content=b"{")) == "invalid_json"
    assert code(await raw(fake, "POST", select, content=b"[1]")) == "not_an_object"
    assert code(await raw(fake, "POST", "/api/v2/session/pause?dry_run=2", content=b"[1]")) == "not_an_object"
    flag = (400, "invalid_param", "dry_run", "dry_run must be 0 or 1")
    assert refusal(await post(fake, "/api/v2/session/pause?dry_run=2")) == flag
    assert refusal(await post(fake, f"{select}?dry_run=true", {"video": "nope"})) == flag  # before the fields
    within = "time must be within 0..1807.158 s"
    leave_out = (400, "not_seekable", "time", "cam-live is a live camera: leave time out")
    for body, refused in [  # in the unit's order: the video, a camera (any time, null too), then the time
        ({}, (400, "missing_field", "video", "Unknown video None")),
        ({"video": None}, (400, "invalid_type", "video", "Unknown video None")),
        ({"video": 5}, (400, "invalid_type", "video", "Unknown video 5")),
        ({"video": True}, (400, "invalid_type", "video", "Unknown video True")),
        ({"video": False}, (400, "invalid_type", "video", "Unknown video False")),
        ({"video": "nope", "time": "x"}, (400, "unknown_video", "video", "Unknown video 'nope'")),
        ({"video": LIVE_CAMERA, "time": 3}, leave_out),
        ({"video": LIVE_CAMERA, "time": None}, leave_out),
        ({"video": LIVE_CAMERA, "time": "x"}, leave_out),
        ({"video": "dry1", "time": "soon"}, (400, "invalid_type", "time", within)),
        ({"video": "dry1", "time": None}, (400, "invalid_type", "time", within)),
        ({"video": "dry1", "time": True}, (400, "invalid_type", "time", within)),
        ({"video": "dry1", "time": -1}, (400, "time_out_of_range", "time", within)),
        ({"video": "dry1", "time": 99999}, (400, "time_out_of_range", "time", within)),
    ]:
        assert refusal(await post(fake, f"{select}?dry_run=1", body)) == refused, body
    camera = (409, "not_seekable", "video", "cam-live is a live camera and cannot seek")
    for body, refused in [  # the video it names, else the one shown; a camera; then the time
        ({"video": "nope"}, (400, "unknown_video", "video", "Unknown video 'nope'")),
        ({"video": "nope", "time": 1}, (400, "unknown_video", "video", "Unknown video 'nope'")),
        ({"video": None, "time": 1}, (400, "invalid_type", "video", "Unknown video None")),
        ({}, camera),
        ({"time": True}, camera),
        ({"time": 3}, camera),
        ({"video": LIVE_CAMERA, "time": 1}, camera),
        ({"video": "dry1"}, (400, "missing_field", "time", within)),
        ({"video": "dry1", "time": True}, (400, "invalid_type", "time", within)),
        ({"video": "dry1", "time": None}, (400, "invalid_type", "time", within)),
        ({"video": "dry1", "time": -1}, (400, "time_out_of_range", "time", within)),
    ]:
        assert refusal(await post(fake, f"{seek}?dry_run=1", body)) == refused, body
    assert (fake.state["revision"], fake.live_phase()) == (3, "live")  # nothing was applied
    start = fake.catalog[0]["default_start"]
    selected = (await post(fake, f"{select}?dry_run=0", {"video": "dry1"})).json()
    assert (selected["state"]["anchor_media"], start, selected["dry_run"]) == (139.0, 139.0, False)
    assert refusal(await post(fake, seek, {"time": -1})) == (400, "time_out_of_range", "time", within)
    put = await raw(fake, "PUT", "/api/v2/session/focus", json={})
    assert code(put) == "missing_field"
    assert code(await raw(fake, "PUT", "/api/v2/session/focus", json={"stone_id": "7"})) == "invalid_type"
    assert code(await raw(fake, "PUT", "/api/v2/session/focus", json={"stone_id": 7, "session": 1})) == "stale_session"
    assert code(await raw(fake, "PUT", "/api/v2/session/focus", json={"stone_id": 999999})) == "unknown_stone"
    unchanged = await raw(fake, "DELETE", "/api/v2/session/focus")
    assert unchanged.json()["state"]["focus"] is None


async def test_a_camera_is_a_source_listed_as_live_or_the_id_live() -> None:
    listed = {"videos": [{"id": "rig", "kind": "camera", "duration": 10.0}, {"id": "live", "duration": None}]}
    fake = FakeFactory(replies={"listVideos": listed, "getVideoMedia-range": Reply(status=206, body=b"x")})
    seek = await post(fake, "/api/v2/session/seek", {"video": "live", "time": 1})  # the design's old id
    assert refusal(seek) == (409, "not_seekable", "video", "live is a live camera and cannot seek")
    selected = await post(fake, "/api/v2/session/select", {"video": "rig", "time": 1})  # kind camera, but not live
    state = selected.json()["state"]
    assert (selected.status_code, "live" in state, state["anchor_media"]) == (200, False, 1.0)
    for video in ("rig", "live"):  # media is refused only to a source listed as live
        assert (await raw(fake, "GET", f"/api/v2/videos/{video}/media")).status_code == 206, video


async def test_clearing_a_focus_is_a_change_by_the_viewer() -> None:
    fake = FakeFactory(recording=[HEADER, clock_line(changed_by="start", focus={"id": 1}), frame(1)])
    cleared = (await raw(fake, "DELETE", "/api/v2/session/focus")).json()["state"]
    assert (cleared["focus"], cleared["changed_by"], cleared["revision"]) == (None, "viewer", 2)


async def test_select_starts_a_session_from_that_time() -> None:
    fake = FakeFactory(recording=recording("dry2-0582-0600"))
    async with client(fake) as factory:
        before = await factory.session.state()
        state = await factory.session.watch("dry2", 590.0)
        assert state.session == before.session + 1
        frames = [i.frame async for i in factory.streams.frames(resume=False) if isinstance(i, FrameItem)]
    assert frames[0].t >= 590.0
    assert all(f.session == state.session for f in frames)
    assert any(f.world is None for f in frames)  # the recording's frames without a world stay so


async def test_a_select_during_a_stream_switches_it() -> None:
    fake = FakeFactory(recording=recording("joined-mid-move"))
    async with client(fake) as factory:
        seen: list[int] = []
        async for item in factory.streams.frames(resume=False):
            if isinstance(item, FrameItem):
                seen.append(item.frame.session)
                if len(seen) == 3:
                    await factory.api.session_seek(81.0)
    assert len(set(seen)) == 2
    assert seen.count(seen[0]) == 3


async def test_live_mode() -> None:
    fake = FakeFactory(recording=recording("session-change-mid-move"), live=True)
    async with client(fake) as factory:
        state = await factory.session.state()
        assert (state.video, state.duration, state.rate) == (LIVE_CAMERA, None, 1.0)
        assert state.live is not None
        assert (state.live.phase, state.live.lag) == ("live", LIVE_LAG_S)  # selected just long enough ago
        assert state.hold_until == fake.time.time()
        frames = [i.frame async for i in factory.streams.frames(resume=False) if isinstance(i, FrameItem)]
    assert frames[0].t == state.anchor_media  # the first frame after the warm-up
    assert frames[0].g == round((frames[0].t - CONNECT_S) * 5)  # g counts the warm-up's frames too, from 0
    assert {f.video for f in frames} == {LIVE_CAMERA}
    assert len({f.session for f in frames}) == 1
    assert [f.g for f in frames] == sorted(f.g for f in frames)
    no_catalog = FakeFactory(recording=[frame(1)], live=True)
    assert [v["id"] for v in no_catalog.catalog] == ["dry1", LIVE_CAMERA]  # the video its frames name, then the camera
    assert (no_catalog.catalog[0]["duration"], no_catalog.catalog[1]["fps"]) == (None, 5.0)
    assert no_catalog.state["live"]["phase"] == "live"  # its only frame was a warm-up frame: live, nothing shown


async def test_stream_parameters() -> None:
    fake = FakeFactory(recording=[frame(g) for g in range(10)])
    assert code(await raw(fake, "GET", "/api/v2/session/frames/stream", params={"after": -2})) == "invalid_param"
    assert code(await raw(fake, "GET", "/api/v2/session/frames/stream", params={"limit": 0})) == "invalid_param"
    assert code(await raw(fake, "GET", "/api/v2/session/frames/stream", params={"layers": "x"})) == "unknown_layer"
    async with client(fake) as factory:
        stream = await factory.api.stream_frames(max_s=1)
        frames = [m async for m in stream if m.event == "frame"]
    assert len(frames) == 5  # one media second of frames 0.2 s apart
    no_format = await raw(fake, "GET", "/api/v2/raw/stream")
    assert no_format.headers["Content-Type"] == "application/x-ndjson"
    assert no_format.content == b""  # these frames carry no raw packet
    with pytest.raises(FakeExhausted):
        await raw(fake, "GET", "/api/v2/raw/stream")
    fresh = FakeFactory(recording=[frame(1)])
    sse = await raw(fresh, "GET", "/api/v2/raw/stream", headers={"Accept": "text/event-stream"})
    assert sse.headers["Content-Type"] == "text/event-stream"


async def test_no_clock_event_on_connect_as_on_the_unit() -> None:
    """The clock lines before a stream's first frame are the state it starts in; later ones come as clock events, and
    a stream resumed after them gets none either."""
    lines = [HEADER, clock_line(revision=1), frame(0), clock_line(revision=2, playing=False), frame(1)]
    fake = FakeFactory(recording=lines)
    assert (await raw(fake, "GET", "/api/v2/session")).json()["state"]["revision"] == 1
    stream = await raw(fake, "GET", "/api/v2/session/frames/stream")
    assert re.findall(r"^event: (\w+)", stream.text, re.M) == ["frame", "clock", "frame"]
    state = (await raw(fake, "GET", "/api/v2/session")).json()["state"]
    assert (state["revision"], state["playing"]) == (2, False)
    resumed = await raw(fake, "GET", "/api/v2/session/frames/stream", headers={"Last-Event-ID": "fakeb00t01-1"})
    assert re.findall(r"^event: (\w+)", resumed.text, re.M) == ["frame"]


async def test_a_long_poll_takes_the_clock_lines_it_passes() -> None:
    """As a stream does: a reply has the state in force at its frames and ends before a later clock line; the next
    reply carries that line's state with the frames after it, and `GET /session` answers it."""
    lines = [HEADER, clock_line(revision=1), frame(0), clock_line(revision=2, playing=False), frame(1)]
    fake = FakeFactory(recording=lines)
    first = (await raw(fake, "GET", "/api/v2/session/frames", params={"limit": 2, "wait": 0})).json()
    assert (first["state"]["revision"], [f["g"] for f in first["frames"]]) == (1, [0])
    assert (await state_of(fake))["revision"] == 1
    params = {"cursor": first["cursor"], "rev": 1, "wait": 0}
    second = (await raw(fake, "GET", "/api/v2/session/frames", params=params)).json()
    assert (second["state"]["revision"], second["state"]["playing"]) == (2, False)
    assert [f["g"] for f in second["frames"]] == [1]
    state = await state_of(fake)
    assert (state["revision"], state["playing"]) == (2, False)
    with pytest.raises(FakeExhausted):
        await raw(fake, "GET", "/api/v2/session/frames", params={"cursor": second["cursor"], "rev": 2, "wait": 0})


async def test_ndjson_side_events_and_trailing_injections() -> None:
    fake = FakeFactory(recording=[{"sse": "gap", "data": {"gap": 2}}, frame(1), {"sse": "clock", "data": 5}, frame(2)])
    fake.inject("gap", {"gap": 7}, after=99)
    fake.inject("worker", {"state": "idle", "reason": ""}, after=0)
    async with client(fake) as factory:
        stream = await factory.api.stream_frames(format="ndjson")
        messages = [m async for m in stream]
    assert [(m.event, m.json().get("gap")) for m in messages] == [
        ("gap", 2),
        ("frame", None),
        ("frame", None),
        ("gap", 7),
    ]


async def test_raw_stream_skips_frames_without_packets_and_paces() -> None:
    lines = recording_lines(FULL)
    fake = FakeFactory(recording=[lines[0], frame(1), *lines[1:]], realtime=True)
    async with client(fake) as factory:
        stream = await factory.api.raw_stream(format="ndjson")
        packets = [m async for m in stream]
    assert len(packets) == 51
    assert fake.time.slept
    assert sum(fake.time.slept) == pytest.approx(10.0, abs=0.3)


async def test_replied_streams_and_polls_end() -> None:
    fake = FakeFactory(replies=API)
    async with client(fake) as factory:
        with pytest.raises(FakeExhausted):
            await factory.api.stream_frames(last_event_id="a2011b037b-10767")  # the recorded reply's last frame
        reply = await factory.api.poll_frames()
        with pytest.raises(FakeExhausted):
            await factory.api.poll_frames(cursor=reply.cursor)
        assert code(await raw(fake, "GET", "/api/v2/session/frames", params={"limit": 99})) == "invalid_param"
        assert code(await raw(fake, "GET", "/api/v2/session/frames", params={"layers": "x"})) == "unknown_layer"
    assert code(await raw(FakeFactory(replies={"x": 1}), "GET", "/api/v2/session/frames")) == "not_found"


async def test_idle_long_poll_waits() -> None:
    fake = FakeFactory(recording=[frame(1)], on_exhausted="end")
    async with client(fake) as factory:
        state = await factory.session.state()
        reply = await factory.api.poll_frames(cursor=999, rev=state.revision, wait=250)
    assert reply.frames == []
    assert reply.cursor == 999
    assert fake.time.slept == [0.25]


def test_sync_transport_serves_files_only() -> None:
    fake = FakeFactory(files={"/a.txt": b"hello"})
    with httpx.Client(transport=fake, base_url=BASE_URL) as http:
        assert http.get("/a.txt").content == b"hello"
        assert http.get("/api/v2/health").status_code == 404
        assert http.post("/a.txt").status_code == 404
        assert http.post("/api/v2/health").status_code == 405


async def test_files_over_the_async_transport() -> None:
    fake = FakeFactory(files={"/a.txt": b"hello"})
    assert (await raw(fake, "GET", "/a.txt")).content == b"hello"


async def test_fail_with_custom_body_and_repeated_close() -> None:
    fake = FakeFactory(recording=[frame(1), frame(2)])
    fake.fail("getHealth", status=500, body=b"<html>oops</html>", times=1)
    async with client(fake, retries=0) as factory:
        with pytest.raises(FactoryError) as info:
            await factory.api.get_health()
        assert info.value.message == "<html>oops</html>"
        stream = await factory.api.stream_frames()
        await stream.response.aclose()
        await stream.response.aclose()
    assert fake.streams_closed == 1


async def test_gap_items_reach_the_typed_stream() -> None:
    fake = FakeFactory(recording=[frame(1)])
    fake.inject("gap", {"gap": 3})
    async with client(fake) as factory:
        items = [i async for i in factory.streams.frames(resume=False)]
    assert isinstance(items[0], GapItem)


async def test_delay_hook_sees_every_event() -> None:
    seen: list[tuple[str, int]] = []

    async def delay(op: str, n: int) -> None:
        seen.append((op, n))
        await asyncio.sleep(0)

    fake = FakeFactory(recording=[frame(1), frame(2)], delay=delay)
    async with client(fake) as factory:
        await (await factory.api.stream_frames()).take(5)
    assert seen == [("streamFrames", 0), ("streamFrames", 1)]


async def test_a_stream_counts_one_close() -> None:
    fake = FakeFactory(recording=[frame(1)])
    response = await fake.handle_async_request(httpx.Request("GET", f"{BASE_URL}/api/v2/session/frames/stream"))
    stream = response.stream
    assert isinstance(stream, httpx.AsyncByteStream)
    assert (fake.streams_opened, fake.viewers) == (1, 1)
    assert (await raw(fake, "GET", "/api/v2/status")).json()["streams"] == 1  # the open ones
    await stream.aclose()
    await stream.aclose()
    assert (fake.streams_closed, fake.viewers) == (1, 0)
    assert (await raw(fake, "GET", "/api/v2/status")).json()["streams"] == 0

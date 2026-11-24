# Python API conventions

<a id="python-what-is-particular-to-this-sdk"></a>

Use ordinary loops for recording files and `asyncio` for live data. This guide covers stream lifetimes, exceptions,
logging and testing with pytest.

## asyncio, and plain loops for files

The client is async: `async with FactoryClient() as factory:` opens one connection pool for the API, the streams and
the session, and closes it at the end. A replay of a file needs no event loop: `Replay.open(path).activities()`,
`.events()`, `.tray_facts()`, `.trays()` and `.summary()` are plain iterators and calls, and each has an async twin
(`aactivities()`, `atrays()`, ...) for code that is async already, or that replays at the pace the file was recorded
([examples/replay_paced.py](../../examples/replay_paced.py)):

```python
replay = Replay.open(source)
async with replay.aactivities(pace="realtime", speed=speed) as activities:
    async for activity in activities:
        print(activity.describe())
```

## Streams are context managers

Every stream (the interpreter's `updates()`, `events()`, `activities()`, `tray_facts()`, and `factory.streams`) is an
async context manager that closes its connection when the block ends. Leave a loop early inside its `async with`:
Python closes an abandoned `async for` only later, and the next loop over the same interpreter would find it busy. A
later loop goes on where the last one stopped, so nothing is missed between them.

```python
async with factory.interpreter().activities() as activities:
    async for activity in activities:
        if isinstance(activity, Move) and activity.outcome is not None:
            counts[activity.outcome] = counts.get(activity.outcome, 0) + 1
        seen += 1
        if seen == limit:
            break
```

## Errors: one type, stable codes

Every failure of a request or a stream is a `FactoryError` with a `code` that does not change between versions (the
API's own codes and the SDK's: `transport`, `decode`, `live_unavailable`, `live_timeout`, `stream_closed`), the HTTP
`status` when there was one, and the `field` a control refused; `wait_for` with a `timeout` raises the builtin
`TimeoutError` instead. Reads are retried by themselves, with backoff and jitter, on connection errors
and on 429, 502, 503 and 504; controls only on 429, which the unit sends before it applies anything
([low-level.md](../low-level.md#retries-and-timeouts)).
From [examples/error_handling.py](../../examples/error_handling.py):

```python
async with FactoryClient(retries=2, timeout=5.0) as factory:
    try:
        camera = await factory.live()
        print(f"live camera: {camera.title}")
    except FactoryError as e:
        if e.code != ErrorCode.LIVE_UNAVAILABLE:
            raise
        print(f"no live camera: {e.message}")
```

## Logging

The SDK logs to the `pdigger_factory_sdk` logger: a warning for every backoff wait before a reconnect, info when the
unit restarted, debug for retries. Show it as you would any library's log
([examples/reconnects.py](../../examples/reconnects.py)):

```python
def show_the_sdk_log() -> None:
    handler = logging.StreamHandler()  # stderr
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    sdk = logging.getLogger("pdigger_factory_sdk")
    sdk.addHandler(handler)
    sdk.setLevel(logging.DEBUG)
```

## The fake unit in pytest

`pdigger_factory_sdk.testing.FakeFactory` is an httpx transport that answers the API from a recording and recorded
replies, on a fake clock, with faults on demand. Give it to the client, and pass its clock's sleep so that retries and
waits take no real time ([examples/testing_with_fake.py](../../examples/testing_with_fake.py)):

```python
async def test_a_server_that_fails() -> None:
    fake = FakeFactory(recording=RECORDING)
    fake.fail("listVideos", status=503, code="busy", times=4)  # more failures than the client retries
    async with FactoryClient("http://fake", transport=fake, sleep=fake.time.sleep, retries=3) as factory:
        with pytest.raises(FactoryError) as error:
            await factory.sources()
    assert error.value.code == "busy"
    assert fake.time.slept  # the retries waited, on the fake clock
```

For code that makes its own client, `use_fake(fake)` makes every `FactoryClient` in the block use the fake
([testing.md](../testing.md#code-that-makes-its-own-client)).

## Types

The models are dataclasses with type hints, and the package ships `py.typed`, so mypy and your editor know every
field. A model keeps the reply it came from in `raw_json`, unknown fields included, so a newer unit's fields are
never lost. The `codec` functions return plain Python values (`Grid`, lists of `Joint` or `StoneItem`), with no NumPy
needed.

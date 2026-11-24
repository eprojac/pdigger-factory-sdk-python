# Tutorials

Start with a tray report. The later tutorials show how to read the events and frames behind it.

| Tutorial | What you’ll do |
| --- | --- |
| [Read a tray report](00-start-here.md) | Read the changes, export a row and investigate uncertainty. |
| [Follow the interpreter](01-high-level.md) | Follow hand movements, get tray facts and trace a stone. |
| [Read individual frames](02-low-level.md) | Inspect model maps, draw stones and hands, and read a frame image. |
| [Watch a live camera](03-live-cameras.md) | Select a camera, wait for warm-up and read its pictures. |
| [Use the Python SDK](04-python.md) | Manage asyncio streams and test with pytest. |

## Before you start

Follow the [installation steps](../../README.md#install) to get the SDK and its example recordings. Run the commands
below from the clone with its virtual environment active.

The replay steps use the bundled files in `fixtures/recordings/`. You can also
[download longer recordings](https://factory.pdigger.com/developers/data/).

For the live steps, set `PDIGGER_FACTORY_URL` to the unit’s base URL. In pytest, `FakeFactory` can supply the same
data from a fixture. [Run an example against a fake unit](../testing.md#code-that-makes-its-own-client).

A unit has one shared session. Selecting a camera, seeking or changing playback affects everyone watching. The
live-camera tutorial selects a real source. The other control examples use dry runs.

The same tutorials are available in [Go](https://github.com/eprojac/pdigger-factory-sdk-go/tree/main/docs/tutorials)
· [Rust](https://github.com/eprojac/pdigger-factory-sdk-rust/tree/main/docs/tutorials) ·
[TypeScript](https://github.com/eprojac/pdigger-factory-sdk-ts/tree/main/docs/tutorials).

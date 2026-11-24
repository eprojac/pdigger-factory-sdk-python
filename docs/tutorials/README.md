# Tutorials

Five short tutorials take you from simple mode, one report per tray, through advanced mode to drawing a frame by hand.
Every piece of code in them is a few lines of a program in [examples/](../../examples), and the tests run each program
against the fixture recordings and the SDK's fake unit, so the code runs as it is shown.

| Tutorial | What you build |
| --- | --- |
| [Start here](00-start-here.md) | simple mode: one report per tray (the stones removed, added and moved, when and where, and whether that is all of it), the same reports as data, the details behind a report that is not complete, and the reports of a unit, live |
| [High level](01-high-level.md) | advanced mode: what the hands did, as sentences and as data: activities, trays and the stones taken off them, the summary, a stone's path; replayed offline, then followed live |
| [Low level](02-low-level.md) | advanced mode: one call per API operation: health and speed, the model, a stone and the focus, frames and the model's maps, draw lists, stones and hands drawn by hand, a frame's image data, the streams |
| [Live cameras](03-live-cameras.md) | advanced mode: the shared session's sources, a plant camera selected, warmed up and followed, and its pictures |
| [Python](04-python.md) | what is particular to this SDK: asyncio and plain loops, streams as context managers, the fake unit in pytest, errors, logging |

## Before you start

- Python 3.10 or newer, and the SDK, from this repository:

  ```sh
  pip install "pdigger-factory-sdk @ git+https://github.com/eprojac/pdigger-factory-sdk-python"
  ```

- For the first two tutorials, nothing else: they replay recordings, offline. Clone the repository for its fixture
  recordings (`fixtures/recordings/`), or download the published ones from https://factory.pdigger.com/developers/data/.
- For the live steps, a unit: set `PDIGGER_FACTORY_URL` to it (by default the SDK asks
  http://factory-orin.local:8120). Without one, the tests run every step against the SDK's fake unit
  (`pip install -r requirements-test.txt -e .`, then `pytest tests/test_examples.py -k tutorial`); to run a program on
  the fake yourself, wrap it in `use_fake`, as [testing.md](../testing.md#code-that-makes-its-own-client) shows.

The unit interprets one source at a time for everybody: a recording or a live camera, on one shared session. A
control (select, seek, play, pause, focus) changes what every viewer sees, so the tutorials send dry runs, which the
unit checks without applying them; only the live-camera tutorial really selects a camera.

The same tutorials, step for step, are in the other languages:
[Go](https://github.com/eprojac/pdigger-factory-sdk-go/tree/main/docs/tutorials),
[Rust](https://github.com/eprojac/pdigger-factory-sdk-rust/tree/main/docs/tutorials) and
[TypeScript](https://github.com/eprojac/pdigger-factory-sdk-ts/tree/main/docs/tutorials).

# Testing

## Unit tests (no unit, no network)

```sh
make test
```

`make test` builds a pinned Python 3.12 image with the versions in `requirements-test.txt`. It starts the container
with networking disabled and runs `tools/test.sh`. The checks run in this order.

1. `ruff format --check` and `ruff check`;
2. `mypy --strict` over `src`, `tests`, `examples` and `tools`;
3. `tools/gen_models.py --check` (the models are what the spec gives);
4. `pytest` with line and branch coverage of `pdigger_factory_sdk`, failing below 100 %;
5. the API reference (pdoc).

Run `make check` to use the same script in your local environment. The tests inject time and randomness. The longest
real waits are a 10 ms timeout and a 5 ms `max_s` deadline, where `asyncio.wait` uses a timeout calculated from the
injected monotonic clock.

What the tests cover:

- **Golden tests** (`tests/test_golden.py`): for every recording in `fixtures/recordings/` (the folder is iterated,
  never a fixed list), `Replay` (sync and async) and the live interpreter over `FakeFactory` (one connection, and
  resuming) give `fixtures/golden/<name>.events.txt`, `.tray-stones.txt`, `.activities.txt`, `.trays.txt`,
  `.open.txt`, `.speckle.txt` and `.summary.txt` byte for byte, and the summary JSON equal to `.summary.json`. A guard checks that the
  recordings, the golden files (nine per recording, no others) and the entries of `fixtures/MANIFEST.json` (sizes and sha256, the golden summaries of the four complete
  recordings in `fixtures/golden/complete/` included) are the same set, and that those summaries read as their text.
- **The interpreter's rules** one by one, the text and numbers, the summary.
- **Damaged input** (`tests/test_damaged.py`, `fixtures/damaged`, the same in every SDK): every line a reader skips and
  every event that does not decode (SDK-DESIGN.md section 6 and 2.3 rule 9), replayed, as headers, streamed live
  (frames, the interpreter's updates, `wait_for`) and as raw packets.
- **Every API operation** (32) against recorded replies, the models of every schema, errors, retries and backoff, SSE
  parsing, stream resume, revisions, duplicates, backfill (including its races), the long-poll and raw packets.
- **The live cameras**: sources, `watch(camera)` and `until_live()` through the phases, refused times and seeks,
  pictures (`not_retained`, `session_gone` before a recording's `not_found`), wall times, the emulation in
  `FakeFactory`.
- **The fake server itself**, since users test with it.
- **Every example** in `examples/` runs over the fake server, and every Python sample of the README and `docs/` is
  checked to be an excerpt of an example. The picture `docs/img/quickstart-replay.svg` is checked against the code's
  output.
- **The docs** (`tests/test_docs.py`): no unfilled placeholders (the shared `docs/BEHAVIOUR-20.20.md` included),
  every link and anchor resolves, every file the docs and the tools' docstrings name exists, and the
  README (the PyPI page) has absolute links only. The output the docs show is real: every `text` block is a run of
  lines of a golden file of the recording its section names, and every interpreter sentence quoted in the docs, the
  docstrings, the code's comments and the examples is a line of a golden file.

### Coverage exclusions

None. Every line and branch of `src/pdigger_factory_sdk` runs in the container; there is no `pragma: no cover`. (The
SDK has no code that needs a window or a GPU.)

## Opt-in tests

`tests/test_integration.py` is skipped unless one of these variables is set. The Docker unit run sets neither.

`make integration` runs them in the test image with the network on. Set `PDIGGER_FACTORY_URL` (an address the
container can reach: an IP address, since `.local` names do not resolve inside Docker), `PDIGGER_FACTORY_RECORDINGS`
(a folder, mounted read-only), or both:

```sh
PDIGGER_FACTORY_URL=http://192.0.2.20:8120 make integration
PDIGGER_FACTORY_RECORDINGS=$PWD/recordings make integration
```

The commands below do the same in your own environment.

### Against a unit: `PDIGGER_FACTORY_URL`

```sh
PDIGGER_FACTORY_URL=http://factory-orin.local:8120 pytest -m integration tests/test_integration.py -k "not recording"
```

Read-only by construction: the client's transport is a guard that refuses, before anything is sent, a control
without `dry_run=1`, a stream without `observe=1` and a long-poll with a viewer id. So the checks never move the shared
session and never keep the worker busy. They read the catalog, the session and its clock, the world, frames, rasters,
packets, renders, metrics and the spec, and try every control as a dry run. Checks that need frames are skipped when
nobody else is watching (the worker is then idle). `PDIGGER_FACTORY_TIMEOUT` (default 60 s) bounds those waits.

### The complete recordings: `PDIGGER_FACTORY_RECORDINGS`

```sh
python tools/download_recordings.py recordings      # the four world files and their summaries, sha256 checked
PDIGGER_FACTORY_RECORDINGS=recordings pytest -m integration tests/test_integration.py -k recording
```

For every `<video>.world.ndjson.gz` in the folder, `Replay` computes the summary of the whole video and compares it with
the golden `fixtures/golden/complete/<video>.summary.json` and with the published `<video>.summary.json` beside it
(each one that exists), and its text with the `.summary.txt` files byte for byte. The `Integration` workflow runs this
every week and by hand, with the downloads cached per published listing; by hand it can also run the unit checks
against a unit with a public address (a unit on a factory network: `make integration` from a machine on that network).

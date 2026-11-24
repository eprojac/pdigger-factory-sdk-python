# Changelog

All notable changes to this SDK. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
version follows [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

Initial SDK for API v2.2. The reference unit ran build `claude-20261123-speckle7` with four recordings and six plant
cameras. It supplies stone appearance data and surface-pattern comparisons.

### Added

#### Tray reports and tracking

- Read one `TrayReport` for each completed tray, live or from a recording. Each change carries a stone ID, a time, a
  position and a confidence score. Use `report.to_dict()` for the complete data.
  [Report fields](docs/interpreter.md#report-fields).
- Inspect `tray.open` at departure to see the possible identities for each unresolved stone. Tray facts carry reason
  codes. Tray stones expose their arrival and departure positions, touch history and the appearance matches used by
  the report. [Tray tracking](docs/interpreter.md#trays-and-tray-stones).
- Read the unit’s `world.speckle` comparisons (build `claude-20261123-speckle3` onwards). `TraySpeckle` groups
  matches by tray. `StoneSpeckle` gives a stone’s best and second-best scores, match quality and pairing. The
  interpreter keeps the latest 1,024 comparisons.
  [Speckle fields and examples](docs/interpreter.md#the-units-speckle-check).
- Version 8 of the tray rules uses the departure comparison to confirm or recover stone IDs. It records
  `speckle_same`, `speckle_moved` or `speckle_found` as the reason. The evidence can be found, absent or undecided.
  Each change’s confidence also appears in the printed report. [Confidence scores](docs/interpreter.md#confidence).
- Trays and tray stones (SDK-DESIGN.md 4.6): stone ids of the SDK's own, one per stone and never reused (a second stone
  first seen on a tray in the same tenth of a second under one world id gets `-2` at the end, a third `-3`), kept over
  each tray from its arrival to its leaving whatever ids the engine's tracker renames or reuses; tray facts that are
  certain or unclear, never revised (`update.tray_facts`, `interpreter.tray_facts()`, `Replay.tray_facts()`), every tray
  with its arrival and leaving points to jump between (`interpreter.trays()`, `Replay.trays()`, `Tray.window()`),
  `state.tray`, `Event.tray_stone`, `InterpreterCore.end()`, and the `trays` counts of the summary.
- If a stone disappears at two calm moments, the SDK can hold its identity open. It restores the identity if the
  stone returns in place and the intervening frames rule out a swap. Hand activity, competing matches, missing
  frames or a stream reset can end the question. Stones with an appearance history follow their own matching rules.
  [Presence and identity rules](spec/SDK-DESIGN.md#461-the-tray-rules).
- A certain removal requires evidence of recent hand contact. Otherwise a lost stone stays available for matching or
  becomes an unclear removal at departure. A visit continues the same tray only if at least 90% of its initial
  stones were already seen on it. Across the four recordings, the SDK found 628 trays and produced 532 reports. A
  video check confirmed 49 of 52 certain removals.
- At departure, the SDK uses appearance, timing and nearby hand activity to decide whether a new tracker ID belongs
  to a lost stone. If the evidence is ambiguous, it records an unclear removal and addition and lists each as a
  candidate for the other. The departure rules also allow some previously tracked stones that have no bounding box
  at departure. In a video check, 32 of the 63 extra stones counted this way were present. Another 25 were false
  detections on empty jul30 boards. [Departure rules](spec/SDK-DESIGN.md#461-the-tray-rules).

#### Client, replay and streams

- `FactoryClient`: the async client (httpx), with retries and full-jitter backoff, timeouts, a bearer token, extra
  headers, an injectable transport, and injectable time and randomness.
- High level: `Interpreter` (typed events, moves, push bursts, hands, stones, summary, sentences, `wait_for`),
  `InterpreterCore` (the rules, without I/O), `Replay` (files and URLs, sync and async, real-time pacing), the shared
  session's controls with `dry_run`, its clock, and the sources with the live cameras (`factory.cameras()`,
  `factory.live()`, `Source.station`); watching a camera: `session.watch(camera)` (a time for a camera refused without
  a request), `session.until_live()` through its `connecting` and `warming` phases (`live_timeout`), and
  `session.picture(g)`, the JPEG a live frame was interpreted from; `session.seek()` reads the shared state first and
  refuses a seek on a camera without sending it.
- Mid level: `factory.streams` with frames over SSE that resume with `Last-Event-ID`, revisions, duplicates dropped,
  backfill of late layers, the long-poll and raw packets.
- Low level: one method for each of the 32 operations (`get_frame_picture` gives a `Picture`: `g` and `jpeg`), typed
  models of every schema, `FactoryError` with stable codes, and the codec for maps, bitsets, depth, surfaces, joints,
  stone tuples and coordinates. `open_video_media` reads media as a stream (`MediaStream`), for a large file;
  `get_video_media` refuses a whole-file reply with neither a length nor byte ranges, which would never end, with
  `not_seekable`.
- Streams count `limit` and `max_s` themselves and close the connection when either is reached (the 20.20 unit holds a
  finished stream about 10 s); `clock_on_connect=True` reads the shared state after every (re)connect, which the
  interpreter uses for wall times (the unit sends no `clock` event on connect).
- Every failure of the API, its streams and recordings is a `FactoryError`, httpx's included (`transport`, or
  `decode` for a body that does not decode or gzip data that is corrupt or truncated; a call on a closed client is
  `stream_closed`); a base URL that cannot work is refused when the client is made (`invalid_param`); a reply without
  the API's error body gets its code from the status, the same in every SDK; `FactoryError` pickles. Retries wait a `Retry-After` given in seconds or as a date.
- Backfill reads are due 1.5, 2.5 and 3.5 s after a frame arrived, each one request of at most 1 s
  (`BACKFILL_TIMEOUT`), so a late layer holds the frames behind it about 4.5 s at the most; the state read after a
  connect is one request of at most 1 s; lines and events of a stream are bounded (64 MiB) and a stream resumes after
  the last event it dispatched. `AsyncStream.aclose()` also works from another task, and ends that loop.
- Later loops over one `Interpreter` go on where the last one stopped (the rest of its frame first), and
  `Interpreter.track(stone_id)` / `InterpreterCore.track` give a stone's path (`TrackPoint`s, the newest 512 points
  of the 4096 most recently moved stones).
- A stream event that does not decode is skipped with a warning and the stream goes on (its id still counts, an
  interpreter loop still ends with the final update, and `wait_for` waits on); a replay skips the lines a reader does
  not understand (frame and side lines that do not decode too), and a read error (gzip data cut short, a dropped
  download) raises and gives no final update. `NaN` and `Infinity` are not JSON (Python's json would read them), and
  JSON nested deeper than Python reads is a line or event that does not decode, not a `RecursionError`. The header is
  the first line that is not blank, when it decodes by its table (a `source` that is no catalog entry is left out of
  it); a header that does not decode is skipped instead of failing the replay.
- Logging to the `pdigger_factory_sdk` logger (reconnects, backoff waits, late layers, retries), and
  `factory.interpreter(max_reconnects=...)`.

#### Testing and documentation

- `testing`: `FakeFactory`, a fake server from recordings and recorded replies with fault injection and a live
  camera emulated (`LIVE_CAMERA`: connecting, warming up and going live on the fake's time, a picture for each of the
  newest frames, no media), `FakeTime` (starting where the recording plays) and `use_fake`.
- The fake unit's camera follows build live3: a play connects again and warms up on the camera's next frames (after a
  break, `g` jumping by 5), only the newest 64 pictures are kept (`KEEP_PICTURES`), frames and raw packets are numbered
  as the unit numbers them, controls and errors come in the unit's order and words (a seek naming a recording is
  applied; a select of a camera with any time, null too, is refused), every change, each phase too, is a `clock`
  event, a stream on a paused camera waits for a control, a stream or a long-poll waiting for a phase moves the fake's
  clock on to it, and a fake with only its camera (no recording) answers the frames, the world and the long-poll from
  it, with nothing retained; `fake.advance(seconds)` and `fake.live_phase()`.
- Five [runnable tutorials](docs/tutorials/README.md) cover tray reports, the interpreter, frame data, live cameras
  and Python conventions. The first tutorial explains a real tray report and follows the uncertain changes back to
  their evidence.

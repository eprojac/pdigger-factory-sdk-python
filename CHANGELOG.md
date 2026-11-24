# Changelog

All notable changes to this SDK. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
version follows [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

The first release, for API v2.2: what the 20.20 unit serves (build `claude-20261123-speckle7`: four recordings, six
plant cameras, the stones' looks and the speckle answers).

### Added

- Tray reports (SDK-DESIGN.md 4.7), simple mode: one `TrayReport` per tray, from `Tray.report()`,
  `interpreter.tray_reports()` (as each tray leaves a unit) and `Replay.tray_reports()` (every tray of a recording):
  the stones removed, added and moved (`TrayChange`), when and where (each one's final place on the tray), and whether
  that is the tray's whole account, as text (`describe()`) and as data (`to_dict()`).
- The tray's open account as it leaves (`Tray.open`, `TrayOpen`, SDK-DESIGN.md 4.6.8), each fact's reason as a code
  (`TrayFact.code`), and the tray stones' `first_place`, `left_place` and `touched`, which the report reads; with the
  stones' looks, `identified` facts and the tray stones' `question`, `looks`, `by_look`, `look_lost` and `open`.
- The unit's speckle answers (SDK-DESIGN.md 4.6.12, `world.speckle` since build `claude-20261123-speckle3`), read as a
  tray's: `TraySpeckle` in `update.speckle`, `interpreter.speckle()` (the newest 1024) and `ReplayResult.speckle`, each
  tray stone's part as `TrayStone.speckle` (`StoneSpeckle`: `score`, `second`, `clean`, `found`, `pair`, `same`), and
  `describe_speckle_txt()`, the golden `speckle.txt`; `WorldLayer.speckle` (`WorldSpeckle`) is None when the answer
  does not fit by value.
- The leaving's speckle answer in every tray report (SDK-DESIGN.md 4.6.13, tray rules version 8): the stones it pairs
  are sure, confirmed on their world stone or linked again (`identified` facts with codes `speckle_same`,
  `speckle_moved` and `speckle_found`; `TrayStone.by_speckle`); each tray stone's `evidence` as its tray leaves
  (`found`, `not_found` or `undecided`); and every report change's `confidence` (`TrayChange.confidence`, from the
  measured constants `CONFIDENCE` and `MOVED_CONFIDENCE`), at the end of each change line of `describe()`.
- `FactoryClient`: the async client (httpx), with retries and full-jitter backoff, timeouts, a bearer token, extra
  headers, an injectable transport, and injectable time and randomness.
- Trays and tray stones (SDK-DESIGN.md 4.6): stone ids of the SDK's own, one per stone and never reused (a second stone
  first seen on a tray in the same tenth of a second under one world id gets `-2` at the end, a third `-3`), kept over
  each tray from its arrival to its leaving whatever ids the engine's tracker renames or reuses; tray facts that are
  certain or unclear, never revised (`update.tray_facts`, `interpreter.tray_facts()`, `Replay.tray_facts()`), every tray
  with its arrival and leaving points to jump between (`interpreter.trays()`, `Replay.trays()`, `Tray.window()`),
  `state.tray`, `Event.tray_stone`, `InterpreterCore.end()`, and the `trays` counts of the summary.
- Presence questions (SDK-DESIGN.md 4.6.1): a tray stone without a look chain that is not seen at two calm moments while
  nothing could have changed it is held in question (`TrayStone.question`, cause `unseen`; not sure, no fact) and is
  sure again once the world sees it again on its place with no hand at work, no held stone, no stone a hand took away
  and no push on the tray meanwhile, no other stone of its size within 0.12 of its place, and no look of its world stone
  that matches another stone in question or lost; otherwise its fact `unsure` comes where the question ends, `opened` at
  its doubt. A question settled on a calm moment that does not show the stone (at once at its doubt, or by a sight
  before that calm moment) holds only if the next calm moment that shows it, or the tray's last frame when the tray
  leaves first and that frame shows it, finds nothing at work since (else a plain `unsure` fact there). A stone with a
  look chain is never held in question: its doubt is a question only its looks settle. Frames the tray rules can not
  read (a frame without a world, a jump in `g`, a `gap` or `reset` of the stream, any revision, the moving frames of a
  tray that goes on) end the questions open then; `InterpreterCore.gap()` tells the core about a `gap` or `reset` event.
  A settled stone gets no `unsure` fact, one whose question ends gets its fact there, and the leaving counts its last
  snapshot's own frame only once for a stone nothing could have changed since its last sight (a stone not detected only
  there gets no `unsure` fact).
- Certain removals and a visit's continuation (SDK-DESIGN.md 4.6.1): a lost tray stone is removed for certain only when
  a hand touched its world id (a `lift`, `hinge`, `carry`, `place`, `drop` or `gone`, a `push` with a `side`, or a
  `delete` with why `hand` or `carried-away`) from the last calm moment before its loss, or from 3.0 s before it was
  last seen if that is earlier, up to its loss; else it stays lost, and a rename or the leaving pairs it, or the leaving
  reports it removed, unclear, with the open cause `no_hand`. A visit continues the open tray only when nine tenths of
  its first frame's stones were seen on it, so a new board that carries over many of the last board's world ids is a
  tray of its own. On the four published recordings: 52 certain removals, 49 of them right by the maintainers' camera
  check, and 628 trays with 532 reports.
- The leaving's evidence (SDK-DESIGN.md 4.6.1, Leaving): as a tray leaves, a lost tray stone is linked to a new world
  stone near it only with evidence that it is the same stone: their looks agree, or the world stone was first seen at or
  after the lost stone was last seen (less 3.0 s), no hand worked within 0.40 of either place since, and their looks do
  not disagree. Without it the lost stone is removed and the world stone added, both unclear (each the other's candidate
  in the open account), so a stone's id never passes to a stone that may be another: by the maintainers' camera check,
  every stone that left dry1's and dry2's trays is reported removed. An unidentified stone that the leaving point's
  world shows detected and not held is present as the tray leaves, at its place there (positive evidence at departure).
  So is one that world keeps there (lists it, not detected), not held, with less than 0.30 of its box under one other
  unheld stone's box there: by the maintainers' camera check, 32 of the 63 stones this adds on the four published
  recordings were there (dry1's tray 2 accounts for the 44 stones the video shows), and 25 are boxes on jul30's bare
  boards that are not there.
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
- Examples, docs, four tutorials (`docs/tutorials/`: high level, low level, live cameras, Python; every step a
  tested program), unit tests in Docker at 100 % line and branch coverage, and golden tests that compare every fixture
  recording with the reference outputs byte for byte.

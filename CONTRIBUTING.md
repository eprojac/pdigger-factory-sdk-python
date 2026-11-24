# Contributing

Thank you for helping. This SDK is one of four (Go, Python, Rust, TypeScript) that follow one contract,
[spec/SDK-DESIGN.md](spec/SDK-DESIGN.md), and must print the same text for the same recording. Behaviour is
therefore changed in all four at once, by the maintainers: open an issue or a pull request here. `spec/`, `fixtures/`,
`docs/BEHAVIOUR-20.20.md` and the shared pictures in `docs/img/` are shared copies; do not edit them here.

## Set up

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-test.txt
pip install --no-deps --no-build-isolation -e .
```

## Check

```sh
make check   # in your environment: format, lint, strict types, models, tests at 100 % coverage, API reference
make test    # the same in Docker with no network, as CI runs it
```

- Formatting and lint: `ruff format` and `ruff check` (line length 120).
- Types: `mypy --strict` over `src`, `tests`, `examples` and `tools`.
- Tests: `pytest` with branch coverage. Coverage must stay at 100 %. Tests never touch the network and never sleep for
  real: use `testing.FakeFactory`, its `FakeTime`, and the delay hooks to order races.
- Golden tests: `tests/test_golden.py` replays every recording of `fixtures/recordings` (the folder, not a list) and
  compares the events, activities and summary with `fixtures/golden` byte for byte, through `Replay` and through the
  live interpreter over the fake server. If they differ, the SDK is wrong, not the golden file.

## Code

- `src/pdigger_factory_sdk/models.py` is generated from `spec/openapi.json` by `tools/gen_models.py`; change the
  generator, then run `make models`.
- Text output never uses Python's float formatting: use `text.fmt` and `text.clock` (SDK-DESIGN.md, section 4.5).
- Every Python sample in `README.md` and `docs/` is an excerpt of a program in `examples/`, which the tests run.
  Change the example and copy the lines; `tests/test_examples.py` checks it.
- `docs/img/quickstart-replay.svg` is drawn from the code's real output by `make picture`; a test fails when it is
  stale.

## Pull requests

Keep them small, with tests, and update `CHANGELOG.md` under an "Unreleased" heading. CI runs the lint, the Docker
tests, the tests on Python 3.10, 3.11 and 3.13, the API reference and the package build.

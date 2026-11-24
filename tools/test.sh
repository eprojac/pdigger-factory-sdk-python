#!/bin/sh
# tools/test.sh: every check of `make test` and CI, in order. The Docker test image runs it with no network:
# formatting, lint, strict types, the unit and golden tests with 100 % line and branch coverage, and the API reference.
set -eu
cd "$(dirname "$0")/.."

echo "== ruff format"
ruff format --check .
echo "== ruff check"
ruff check .
echo "== mypy --strict"
mypy
echo "== the models match the spec"
python tools/gen_models.py --check
echo "== pytest (unit and golden tests, 100 % line and branch coverage)"
pytest -p no:cacheprovider --cov=pdigger_factory_sdk --cov-branch --cov-report=term-missing --cov-fail-under=100
echo "== the API reference builds"
out=$(mktemp -d)
sh tools/docs.sh "$out"
test -f "$out/pdigger_factory_sdk.html"
test -f "$out/pdigger_factory_sdk/testing.html"
rm -rf "$out"
echo "== all checks passed"

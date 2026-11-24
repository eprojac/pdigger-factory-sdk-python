# make test   the unit tests in Docker, with no network, as CI runs them (lint, types, tests at 100 % coverage, docs);
#             make test DOCKER_BUILD_FLAGS=--no-cache rebuilds the image from nothing
# make check  the same checks in your own environment (pip install -r requirements-test.txt -e . first)
# make integration  the opt-in tests in the same image, with the network: the read-only checks of the unit at
#             PDIGGER_FACTORY_URL and/or the complete recordings in the folder PDIGGER_FACTORY_RECORDINGS
# make docs   the API reference in site/ (pdoc)
# make picture  redraw docs/img/quickstart-replay.svg from the code's output
# make clean  remove caches, coverage data and build output

IMAGE := pdigger-factory-sdk-python-test
DOCKER_BUILD_FLAGS ?=
DOCKER_RUN_FLAGS ?=
PYTHON ?= python3

.PHONY: test image integration integration-env check docs picture models build clean

test: image
	docker run --rm --network none $(DOCKER_RUN_FLAGS) $(IMAGE)

image:
	docker build $(DOCKER_BUILD_FLAGS) -t $(IMAGE) -f Dockerfile.test .

# The unit's address must be one the container can reach (an IP address; .local names do not resolve in Docker).
# PDIGGER_FACTORY_TIMEOUT (seconds, default 60) bounds the unit checks.
integration: integration-env image
	docker run --rm $(DOCKER_RUN_FLAGS) \
		$(if $(PDIGGER_FACTORY_URL),-e PDIGGER_FACTORY_URL="$(PDIGGER_FACTORY_URL)") \
		$(if $(PDIGGER_FACTORY_TIMEOUT),-e PDIGGER_FACTORY_TIMEOUT="$(PDIGGER_FACTORY_TIMEOUT)") \
		$(if $(PDIGGER_FACTORY_RECORDINGS),-e PDIGGER_FACTORY_RECORDINGS=/recordings -v "$$(cd "$(PDIGGER_FACTORY_RECORDINGS)" && pwd)":/recordings:ro) \
		$(IMAGE) pytest -p no:cacheprovider -m integration tests/test_integration.py

# Checked before the image is built.
integration-env:
	@test -n "$(PDIGGER_FACTORY_URL)$(PDIGGER_FACTORY_RECORDINGS)" || { echo 'make integration: set PDIGGER_FACTORY_URL=http://<unit>:8120 and/or PDIGGER_FACTORY_RECORDINGS=<folder>' >&2; exit 2; }
	@test -z "$(PDIGGER_FACTORY_RECORDINGS)" || test -d "$(PDIGGER_FACTORY_RECORDINGS)" || { echo 'make integration: PDIGGER_FACTORY_RECORDINGS is not a folder' >&2; exit 2; }

check:
	sh tools/test.sh

docs:
	sh tools/docs.sh site

picture:
	$(PYTHON) tools/terminal_picture.py

models:
	$(PYTHON) tools/gen_models.py

build:
	$(PYTHON) -m build

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage .coverage.* coverage.xml htmlcov dist build site
	find . \( -name __pycache__ -o -name .mypy_cache -o -name .ruff_cache -o -name .pytest_cache \) -type d -prune \
		-exec rm -rf {} +

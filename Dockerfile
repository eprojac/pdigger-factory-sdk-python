# The SDK in a small image, to try it without installing Python packages:
#   docker build -t pdigger-factory-sdk-python .
#   docker run --rm -e PDIGGER_FACTORY_URL=http://192.0.2.20:8120 pdigger-factory-sdk-python   # the unit's IP:
#                                                       .local names do not resolve inside a container
#   docker run --rm pdigger-factory-sdk-python python examples/quickstart_replay.py    # needs internet access
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels .

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore
COPY --from=build /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels && useradd --create-home --uid 10001 app
WORKDIR /app
COPY examples ./examples
USER app
CMD ["python", "examples/quickstart_live.py"]

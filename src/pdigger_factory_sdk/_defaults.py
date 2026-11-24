"""Process-wide defaults that `testing.use_fake()` installs, so code that makes its own clients can be tested."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

__all__ = ["DEFAULTS", "Defaults"]


@dataclass(slots=True, frozen=True)
class Defaults:
    """What a client or replay uses when it is not given its own."""

    transport: Any = None
    """An httpx transport (async for `FactoryClient`, sync or async for `Replay` URLs)."""
    sleep: Callable[[float], Awaitable[None]] | None = None
    monotonic: Callable[[], float] | None = None
    wall: Callable[[], float] | None = None
    seed: int | None = None
    """A seed for the backoff's jitter."""


DEFAULTS: ContextVar[Defaults | None] = ContextVar("pdigger_factory_sdk_defaults", default=None)

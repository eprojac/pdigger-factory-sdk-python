"""`AsyncStream`: the async iterator every streaming method returns."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Generic, TypeVar

T = TypeVar("T")

__all__ = ["AsyncStream"]


class AsyncStream(Generic[T]):
    """An async iterator over items that arrive on an open connection.

    Iterate it with `async for`. It holds its HTTP connection until it ends, is closed with `aclose()` (also from
    another task, while a loop waits for the next item: that loop then ends), leaves an `async with` block, or the task
    iterating it is cancelled; every one of these closes the connection.

        async with factory.interpreter().events() as events:
            async for event in events:
                print(event.describe())
    """

    __slots__ = ("_closed", "_gen", "_step")

    def __init__(self, gen: AsyncGenerator[T, None]) -> None:
        self._gen = gen
        self._step: asyncio.Future[T] | None = None
        self._closed = False

    def __aiter__(self) -> AsyncStream[T]:
        return self

    async def __anext__(self) -> T:
        if self._closed:
            raise StopAsyncIteration
        # The generator steps in a task of its own, so that aclose() from another task can stop a step that waits.
        step = self._step = asyncio.ensure_future(self._gen.__anext__())
        try:
            await asyncio.wait((step,))
        except BaseException:  # this task was cancelled: so is the step, which closes the connection
            step.cancel()
            await asyncio.wait((step,))
            raise
        finally:
            self._step = None
        if step.cancelled():  # aclose() from another task
            raise StopAsyncIteration
        return step.result()

    async def aclose(self) -> None:
        """Stop the stream and close its connection, also while another task waits for its next item (that loop then
        ends). Iterating it afterwards ends at once."""
        self._closed = True
        step = self._step
        if step is not None:
            step.cancel()
            await asyncio.wait((step,))
        await self._gen.aclose()

    async def __aenter__(self) -> AsyncStream[T]:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def take(self, n: int) -> list[T]:
        """The next `n` items (fewer if the stream ends first); then the stream is closed."""
        out: list[T] = []
        try:
            while len(out) < n:
                try:
                    out.append(await self.__anext__())
                except StopAsyncIteration:
                    break
        finally:
            await self.aclose()
        return out

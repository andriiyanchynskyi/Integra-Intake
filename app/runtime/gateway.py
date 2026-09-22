"""Explicit bridge from a synchronous agent thread to the worker loop."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TypeVar


T = TypeVar("T")


class WorkerAsyncGateway:
    """Schedule one coroutine on the worker's owner loop."""

    def __init__(self, owner_loop: asyncio.AbstractEventLoop) -> None:
        self._owner_loop = owner_loop

    def call(self, awaitable: Coroutine[object, object, T]) -> T:
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._owner_loop:
            awaitable.close()
            raise RuntimeError(
                "gateway must be called from the synchronous agent thread"
            )
        try:
            future = asyncio.run_coroutine_threadsafe(
                awaitable, self._owner_loop
            )
        except BaseException:
            awaitable.close()
            raise
        return future.result()

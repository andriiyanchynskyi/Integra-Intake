import asyncio

import pytest

from app.runtime.gateway import WorkerAsyncGateway


@pytest.mark.asyncio
async def test_gateway_timeout_cancels_owner_loop_future() -> None:
    cancelled = asyncio.Event()

    async def blocked() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    gateway = WorkerAsyncGateway(asyncio.get_running_loop(), timeout_seconds=0.01)

    with pytest.raises(TimeoutError, match="gateway call timed out"):
        await asyncio.to_thread(gateway.call, blocked())

    await asyncio.wait_for(cancelled.wait(), timeout=0.5)

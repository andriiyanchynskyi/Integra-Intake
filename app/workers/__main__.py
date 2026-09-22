"""Process entry point for the Phase-7 worker."""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.workers.agent_worker import AgentWorker


async def main() -> None:
    await AgentWorker.from_settings(settings).serve()


if __name__ == "__main__":
    asyncio.run(main())

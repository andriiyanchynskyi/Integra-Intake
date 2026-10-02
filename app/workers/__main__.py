"""Process entry point for the asynchronous agent worker."""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.observability.logging import configure_json_logging
from app.workers.agent_worker import AgentWorker


async def main() -> None:
    configure_json_logging()
    await AgentWorker.from_settings(settings).serve()


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .config import load_config
from .listener import SignalListener
from .storage import Storage


async def amain() -> None:
    load_dotenv()
    cfg = load_config(Path("config.yaml"))
    logging.basicConfig(
        level=cfg.logging.level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    storage = Storage(cfg.storage.db_path)
    await storage.init()
    client = SignalListener(channel_id=cfg.discord.channel_id, storage=storage)
    try:
        await client.start(os.environ["DISCORD_USER_TOKEN"])
    finally:
        await storage.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()

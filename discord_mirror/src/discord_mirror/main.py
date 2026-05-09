from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .config import load_config
from .listener import Executor, SignalListener
from .parser import SignalParser
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
    parser = SignalParser(api_key=os.environ["ANTHROPIC_API_KEY"])

    mode = os.environ.get("EXECUTION_MODE", "paper")
    adapter = None
    executor: Executor
    if mode == "live":
        from .executor_metaapi import MetaApiAdapter, MetaApiExecutor

        adapter = MetaApiAdapter(
            token=os.environ["METAAPI_TOKEN"],
            account_id=os.environ["METAAPI_ACCOUNT_ID"],
            region=os.environ.get("METAAPI_REGION", "new-york"),
        )
        await adapter.connect()
        executor = MetaApiExecutor(adapter=adapter, storage=storage, cfg=cfg)
    else:
        from .executor_paper import PaperExecutor

        executor = PaperExecutor(storage=storage, cfg=cfg)

    client = SignalListener(
        channel_id=cfg.discord.channel_id,
        storage=storage,
        parser=parser,
        executor=executor,
    )
    try:
        await client.start(os.environ["DISCORD_USER_TOKEN"])
    finally:
        if adapter:
            await adapter.close()
        await storage.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()

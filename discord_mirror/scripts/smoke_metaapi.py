"""Smoke test for MetaApi adapter — requires a populated .env.

Verifies that we can connect to the demo account, read the balance,
and fetch an XAUUSD bid/ask. Prints results and exits.
"""

from __future__ import annotations

import asyncio
import os

from discord_mirror.executor_metaapi import MetaApiAdapter
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    adapter = MetaApiAdapter(
        token=os.environ["METAAPI_TOKEN"],
        account_id=os.environ["METAAPI_ACCOUNT_ID"],
        region=os.environ.get("METAAPI_REGION", "new-york"),
    )
    await adapter.connect()
    print("balance:", await adapter.balance())
    bid, ask = await adapter.get_price("XAUUSD")
    print("XAUUSD bid:", bid, "ask:", ask)
    await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Read-only MetaApi connectivity check. Places NO orders.

Verifies the token + account work, the account is deployed/connected,
and reports balance, open positions, and the gold symbol spec.
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi

load_dotenv(Path(__file__).resolve().parent / ".env")
TOKEN = os.environ["METAAPI_TOKEN"]
ACCOUNT_ID = os.environ["METAAPI_ACCOUNT_ID"]
SYMBOL = os.environ.get("TIMBOT_SYMBOL", "XAUUSD")


async def main() -> None:
    api = MetaApi(TOKEN)
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    print(f"account     : {account.name}  ({account.type})")
    print(f"state       : {account.state} / {account.connection_status}")

    if account.state in ("UNDEPLOYED", "DEPLOYING"):
        print("deploying account ...")
        await account.deploy()
    print("waiting for connection (first deploy can take a few minutes) ...")
    await account.wait_connected()

    conn = account.get_rpc_connection()
    await conn.connect()
    await conn.wait_synchronized()

    info = await conn.get_account_information()
    print(f"balance     : {info['balance']} {info['currency']}")
    print(f"equity      : {info['equity']}")
    print(f"leverage    : {info.get('leverage')}")

    positions = await conn.get_positions()
    print(f"open positions: {len(positions)}")

    try:
        spec = await conn.get_symbol_specification(SYMBOL)
        print(f"symbol {SYMBOL}: min lot {spec['minVolume']}, step {spec['volumeStep']}, max {spec['maxVolume']}")
    except Exception as e:
        print(f"WARNING: symbol {SYMBOL!r} not found — check TIMBOT_SYMBOL. ({e})")

    print("\nconnectivity OK — no orders placed.")


if __name__ == "__main__":
    asyncio.run(main())

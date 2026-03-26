#!/usr/bin/env python3
"""Diagnose MetaApi symbol availability for OANDA account.

Queries the connected OANDA demo account to check:
1. What symbol specifications are available
2. Whether EURUSD is recognized
3. What the actual symbol names look like
"""

import asyncio
import os
import sys

# Load env
from pathlib import Path

env_file = Path("/etc/novacore/novatrade.env")
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from metaapi_cloud_sdk import MetaApi  # noqa: E402


async def main():
    token = os.environ.get("METAAPI_TOKEN") or os.environ.get("META_API_TOKEN")
    account_id = os.environ.get("METAAPI_ACCOUNT_ID")
    region = os.environ.get("METAAPI_REGION", "london")

    if not token or not account_id:
        print("ERROR: METAAPI_TOKEN and METAAPI_ACCOUNT_ID must be set")
        sys.exit(1)

    print(f"Account ID: {account_id}")
    print(f"Region: {region}")

    api = MetaApi(token, {"region": region})
    account = await api.metatrader_account_api.get_account(account_id)

    print(f"Account state: {account.state}")
    print(f"Account type: {getattr(account, 'type', 'unknown')}")
    print(f"Platform: {getattr(account, 'platform', 'unknown')}")

    if account.state != "DEPLOYED":
        print("Account not deployed, deploying...")
        await account.deploy()
        await account.wait_deployed()

    await account.wait_connected()

    conn = account.get_rpc_connection()
    await conn.connect()
    await conn.wait_synchronized()

    print("\n=== Account Info ===")
    info = await conn.get_account_information()
    print(f"  Broker: {info.get('broker', 'unknown')}")
    print(f"  Server: {info.get('server', 'unknown')}")
    print(f"  Balance: {info.get('balance', 0)}")
    print(f"  Currency: {info.get('currency', 'unknown')}")
    print(f"  Platform: {info.get('platform', 'unknown')}")

    # Try various symbol formats
    test_symbols = ["EURUSD", "EUR/USD", "EURUSDm", "EURUSD.m", "EUR_USD", "eurusd"]
    print("\n=== Symbol Price Tests ===")
    for sym in test_symbols:
        try:
            price = await conn.get_symbol_price(sym)
            print(f"  {sym}: bid={price['bid']}, ask={price['ask']} ✅")
        except Exception as e:
            print(f"  {sym}: {type(e).__name__}: {e} ❌")

    # Try to get symbol specification
    print("\n=== Symbol Specification Tests ===")
    for sym in test_symbols:
        try:
            spec = await conn.get_symbol_specification(sym)
            print(
                f"  {sym}: description={spec.get('description', 'N/A')}, "
                f"digits={spec.get('digits', 'N/A')}, "
                f"contractSize={spec.get('contractSize', 'N/A')} ✅"
            )
        except Exception as e:
            print(f"  {sym}: {type(e).__name__}: {e} ❌")

    # Try to get symbols list if available
    print("\n=== Available Symbols (first 20) ===")
    try:
        symbols = await conn.get_symbols()
        print(f"  Total symbols: {len(symbols)}")
        # Filter for EUR pairs
        eur_syms = [s for s in symbols if "EUR" in s.upper() or "eur" in s]
        print(f"  EUR-containing symbols: {eur_syms[:20]}")
        # Show first 20 overall
        print(f"  First 20: {symbols[:20]}")
    except Exception as e:
        print(f"  get_symbols failed: {type(e).__name__}: {e}")

    # Close
    await conn.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())

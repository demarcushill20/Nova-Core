#!/usr/bin/env python3
"""Quick verification that the symbol fix resolves EURUSD → EURUSD.sim correctly."""

import asyncio
import os
import sys

# Ensure we're in the right directory
os.chdir("/home/nova/nova-core")

from novatrade.config import NovaTradeCfg  # noqa: E402


async def main():
    cfg = NovaTradeCfg.load()
    print(f"Config symbols: {cfg.symbols}")
    print(f"FTMO suffix: {cfg.ftmo.symbol_suffix!r}")

    broker_symbol = cfg.ftmo.resolve_symbol(cfg.symbols[0])
    print(f"Resolved: {cfg.symbols[0]} → {broker_symbol}")

    # Now test with MetaApi
    from novatrade.adapter.metaapi_provider import MetaApiAdapter

    adapter = MetaApiAdapter(config=cfg.metaapi)
    status = await adapter.connect()
    print(f"Connected: {status.connected} ({status.message})")

    if not status.connected:
        print("ERROR: Could not connect")
        sys.exit(1)

    # Test price with broker symbol
    try:
        price = await adapter.get_symbol_price(broker_symbol)
        print(f"Price for {broker_symbol}: bid={price.bid:.5f} ask={price.ask:.5f} ✅")
    except Exception as e:
        print(f"Price for {broker_symbol}: {e} ❌")

    # Test candles with broker symbol
    try:
        candles = await adapter.get_candles(broker_symbol, "5m", 5)
        print(f"Candles for {broker_symbol} M5: {len(candles)} bars ✅")
        if candles:
            c = candles[-1]
            print(f"  Latest: O={c.open:.5f} H={c.high:.5f} L={c.low:.5f} C={c.close:.5f}")
    except Exception as e:
        print(f"Candles for {broker_symbol}: {e} ❌")

    await adapter.disconnect()
    print("\nVerification complete.")


if __name__ == "__main__":
    asyncio.run(main())

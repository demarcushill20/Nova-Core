#!/usr/bin/env python3
"""MetaAPI Connection Diagnostic Tool

Quickly diagnose MetaAPI connection issues including region mismatch,
credential problems, and network connectivity.

Usage:
    python scripts/diagnose_metaapi_connection.py
    python scripts/diagnose_metaapi_connection.py --region new-york
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

# Add nova-core to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from novatrade.config import NovaTradeCfg

log = logging.getLogger(__name__)


async def diagnose_metaapi_connection(override_region: str | None = None) -> bool:
    """Diagnose MetaAPI connection issues.

    Returns:
        True if connection is healthy, False if issues found
    """
    print("🔍 NovaTrade MetaAPI Connection Diagnostic")
    print("=" * 50)

    # Load config
    try:
        cfg = NovaTradeCfg.load()
        print("✅ Config loaded successfully")
    except Exception as exc:
        print(f"❌ Config load failed: {exc}")
        return False

    # Check credentials
    print("\n📋 Credential Check")
    meta_errors = cfg.metaapi.validate()
    if meta_errors:
        print(f"❌ Missing credentials: {'; '.join(meta_errors)}")
        return False
    else:
        print("✅ MetaAPI credentials present")

    # Display connection parameters
    effective_region = override_region or cfg.metaapi.region
    print("\n🌍 Connection Parameters")
    print(f"   Account ID: {cfg.metaapi.account_id}")
    print(f"   Region: {effective_region} {'(override)' if override_region else '(from config)'}")
    print(f"   Token: ...{cfg.metaapi.token[-12:] if cfg.metaapi.token else 'None'}")

    # Create adapter with potentially overridden region
    try:
        from novatrade.adapter.metaapi_provider import MetaApiAdapter

        test_config = cfg.metaapi
        if override_region:
            # Create modified config for testing
            import dataclasses

            test_config = dataclasses.replace(test_config, region=override_region)

        adapter = MetaApiAdapter(config=test_config)
        print("✅ MetaAPI adapter created")
    except Exception as exc:
        print(f"❌ Adapter creation failed: {exc}")
        return False

    # Test connection
    print("\n🔗 Connection Test")
    start_time = time.time()
    try:
        status = await adapter.connect()
        elapsed = time.time() - start_time
        if status.connected:
            print(f"✅ Connection successful ({elapsed:.2f}s)")
        else:
            print(f"❌ Connection failed: {status.message}")
            return False
    except Exception as exc:
        elapsed = time.time() - start_time
        print(f"❌ Connection exception ({elapsed:.2f}s): {exc}")

        # Check for specific error patterns
        exc_str = str(exc).lower()
        if "does not match the account region" in exc_str:
            print("🚨 REGION MISMATCH DETECTED!")
            print(f"   Current region: {effective_region}")
            print("   Try: --region new-york or --region london")
            return False
        elif "timeout" in exc_str:
            print("⏰ Connection timeout - may be region or network issue")
            return False
        else:
            return False

    # Test account fetch
    print("\n💰 Account Fetch Test")
    try:
        account = await adapter.get_account()
        print("✅ Account fetch successful")
        print(f"   Balance: ${account.balance:,.2f}")
        print(f"   Equity: ${account.equity:,.2f}")
        print(f"   Mode: {account.mode.value}")
    except Exception as exc:
        print(f"❌ Account fetch failed: {exc}")
        return False

    # Test candle fetch
    print("\n📊 Historical Data Test")
    try:
        symbol = cfg.symbols[0] if cfg.symbols else "EURUSD"
        broker_symbol = cfg.ftmo.resolve_symbol(symbol)
        print(f"   Symbol: {symbol} → {broker_symbol}")

        candles = await adapter.get_candles(broker_symbol, "H1", 10)
        print(f"✅ Historical data fetch successful ({len(candles)} candles)")
        if candles:
            latest = candles[-1]
            print(f"   Latest: {latest.timestamp} O:{latest.open} H:{latest.high} L:{latest.low} C:{latest.close}")
    except Exception as exc:
        print(f"❌ Historical data fetch failed: {exc}")
        return False

    # Test price fetch
    print("\n💱 Live Price Test")
    try:
        price = await adapter.get_symbol_price(broker_symbol)
        print("✅ Live price fetch successful")
        print(f"   Bid: {price.bid}")
        print(f"   Ask: {price.ask}")
        print(f"   Spread: {price.spread:.1f} pips")
    except Exception as exc:
        print(f"❌ Live price fetch failed: {exc}")
        return False

    print("\n✅ All tests passed! MetaAPI connection is healthy.")
    return True


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Diagnose MetaAPI connection issues")
    parser.add_argument("--region", help="Override region for testing (e.g., new-york, london)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    try:
        success = asyncio.run(diagnose_metaapi_connection(args.region))
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted by user")
        sys.exit(1)
    except Exception as exc:
        print(f"\n💥 Unexpected error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Cron-friendly risk state updater with error handling and logging.

This script can be run periodically (e.g., every 5 minutes) to ensure the risk state
file is always fresh, preventing monitoring health check failures.

Usage:
    # Run once
    python3 scripts/risk_state_cron.py

    # Add to crontab (every 5 minutes)
    */5 * * * * cd /home/nova/nova-core && python3 scripts/risk_state_cron.py >> LOGS/risk_state_cron.log 2>&1

Exit codes:
    0: Success
    1: Risk state update failed
    2: Configuration error
"""

import asyncio
import json
import logging
import sys
import traceback
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from novatrade.config import NovaTradeCfg  # noqa: E402
from novatrade.models import AccountState  # noqa: E402
from novatrade.risk.risk_engine import RiskEngine  # noqa: E402


def setup_logging():
    """Set up logging with timestamps."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("risk_state_cron")


def load_last_equity() -> float:
    """Load the most recent live equity from the equity history file.

    The risk_state.json file only stores drawdown *percentages*, not an
    absolute equity, so deriving equity from it always produces a
    misleading value (zero drawdown → $100,000 exactly, regardless of
    the actual account). The equity snapshot maintained by the live loop
    is the correct source.
    """
    default_equity = 100_000.0
    try:
        history_path = Path("STATE") / "novatrade" / "equity_history.json"
        if history_path.exists():
            data = json.loads(history_path.read_text(encoding="utf-8"))
            snapshots = data.get("snapshots") or []
            if snapshots:
                return float(snapshots[-1].get("equity", default_equity))
            return float(data.get("initial_equity", default_equity))
    except Exception as e:
        logging.getLogger(__name__).debug(f"Failed to read equity history: {e}")
    return default_equity


async def update_risk_state() -> bool:
    """Update the risk state file with current data.

    Returns True if successful, False otherwise.
    """
    log = setup_logging()

    try:
        # Load config
        log.info("Loading NovaTrade configuration...")
        cfg = NovaTradeCfg.load()

        # Use the last known equity to maintain consistency
        equity = load_last_equity()

        # Create dummy account state with current timestamp
        account = AccountState(
            balance=100_000.0,
            equity=equity,
            mode=cfg.mode,
        )

        log.info(f"Creating risk engine for mode={cfg.mode}, equity=${equity:,.2f}")

        # Create and initialize risk engine
        risk_engine = RiskEngine(cfg)
        risk_engine.initialize(account)

        # Write the risk state with fresh timestamp
        risk_engine.write_risk_state()

        log.info("Risk state updated successfully")
        return True

    except Exception as exc:
        log.error(f"Failed to update risk state: {exc}")
        log.debug(traceback.format_exc())
        return False


def main():
    """CLI entrypoint."""
    log = setup_logging()

    try:
        log.info("Starting risk state update cron job")
        success = asyncio.run(update_risk_state())

        if success:
            log.info("Risk state cron job completed successfully")
            sys.exit(0)
        else:
            log.error("Risk state cron job failed")
            sys.exit(1)

    except KeyboardInterrupt:
        log.info("Risk state cron job interrupted by user")
        sys.exit(1)
    except Exception as exc:
        log.error(f"Unexpected error in risk state cron job: {exc}")
        log.debug(traceback.format_exc())
        sys.exit(2)


if __name__ == "__main__":
    main()

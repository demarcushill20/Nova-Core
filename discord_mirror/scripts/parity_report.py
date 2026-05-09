"""Daily parity report — compares raw messages vs parsed signals vs trade fills.

Usage:
    python scripts/parity_report.py [days_back]

Default is 1 day.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

from discord_mirror.parity import daily_report
from discord_mirror.storage import Storage


async def main(days: int = 1) -> None:
    s = Storage("data/signals.db")
    await s.init()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    print(json.dumps(await daily_report(s, since), indent=2))
    await s.close()


if __name__ == "__main__":
    days_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    asyncio.run(main(days_arg))

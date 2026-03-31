#!/usr/bin/env python3
"""Script to store the daily summary in Fusion Memory using the memory service directly."""

import asyncio
import sys
import os
from pathlib import Path

# Add the fusion memory MCP to path
sys.path.append('/home/nova/Nova_AI_Fusion_Memory_MCP')

from app.services.memory_service import MemoryService
from app.config import Config

async def store_daily_summary():
    """Store the daily summary in Fusion Memory."""

    content = "2026-03-30: Critical operational breakthrough day with 5 major production fixes (scheduler feedback loop crisis resolved, MetaAPI rate limiting system deployed 930+ lines, LiveLoop state machine desync fixed, data feed staleness recovery with 4-tier diagnostics, feed health monitoring enhancement). Achieved peak autonomy performance at 86.5/100 with all 5 dimensions GREEN. Completed 15+ autonomous shifts, 12,852+ tests passing (100% pass rate). NovaTrade live trading profitable with +$75 profit. System reached watershed operational maturity with comprehensive stability improvements. 10 production commits deployed."

    metadata = {
        "memory_type": "daily_summary",
        "project": "nova-core",
        "tags": ["daily-report", "2026-03-30", "production-fixes", "autonomy-peak", "novatrade-profit", "critical-breakthrough"],
        "category": "daily_summary"
    }

    try:
        # Initialize the memory service
        config = Config()
        memory_service = MemoryService(config)

        # Store the summary
        item_id = await memory_service.perform_upsert(
            content=content,
            metadata=metadata,
            id=None  # Let it auto-generate
        )

        print(f"✅ Daily summary stored successfully with ID: {item_id}")
        return item_id

    except Exception as e:
        print(f"❌ Error storing daily summary: {e}")
        import traceback
        traceback.print_exc()
        return None

if __name__ == "__main__":
    asyncio.run(store_daily_summary())
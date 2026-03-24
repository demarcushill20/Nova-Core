#!/usr/bin/env python3
"""Launch a full AutoResearch campaign seeded with IRB v4.2.

Loads the frozen snapshot 12197b08a4dca165 (12,279 H1 bars, 2024-03-25 → 2026-03-20)
and runs a 200-experiment campaign with L1 perturbation mutations + walk-forward
validation on new bests.

Usage:
    python scripts/run_v42_campaign.py [--max-experiments 200] [--wall-clock 7200]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure nova-core is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from novatrade.cli.commands.campaign_cmd import format_campaign_report, start_campaign
from novatrade.evaluation.gates import GateConfig
from novatrade.optimization.campaign_engine import CampaignConfig
from novatrade.optimization.walkforward import WalkForwardConfig

log = logging.getLogger("campaign.v42")

SNAPSHOT_DIR = Path("/home/nova/nova-core/data/snapshots")
SNAPSHOT_ID = "12197b08a4dca165"
CONFIG_PATH = Path("/home/nova/nova-core/configs/strategies/irb_v4_enhanced.yaml")


def load_snapshot_parquet(snapshot_id: str) -> tuple[list, list, dict]:
    """Load H1 + H4 candles from a frozen Parquet snapshot."""
    import pandas as pd

    from novatrade.models import Candle

    meta_path = SNAPSHOT_DIR / f"{snapshot_id}_meta.json"
    h1_path = SNAPSHOT_DIR / f"{snapshot_id}_h1.parquet"
    h4_path = SNAPSHOT_DIR / f"{snapshot_id}_h4.parquet"

    for p in (meta_path, h1_path, h4_path):
        if not p.exists():
            raise FileNotFoundError(f"Snapshot file missing: {p}")

    meta = json.loads(meta_path.read_text())

    def df_to_candles(df: pd.DataFrame, tf: str) -> list[Candle]:
        candles = []
        for _, row in df.iterrows():
            candles.append(
                Candle(
                    timestamp=float(
                        row.get("timestamp", row.name.timestamp() if hasattr(row.name, "timestamp") else 0)
                    ),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                    symbol=meta.get("symbol", "EURUSD"),
                    timeframe=tf,
                )
            )
        return candles

    h1_df = pd.read_parquet(h1_path)
    h4_df = pd.read_parquet(h4_path)

    h1_candles = df_to_candles(h1_df, "H1")
    h4_candles = df_to_candles(h4_df, "H4")

    return h1_candles, h4_candles, meta


def main() -> None:
    parser = argparse.ArgumentParser(description="IRB v4.2 AutoResearch Campaign")
    parser.add_argument("--max-experiments", type=int, default=200)
    parser.add_argument("--wall-clock", type=int, default=7200, help="Max seconds")
    parser.add_argument("--stagnation", type=int, default=20)
    parser.add_argument("--checkpoint-interval", type=int, default=25)
    parser.add_argument("--walkforward", action="store_true", default=True)
    parser.add_argument("--no-walkforward", dest="walkforward", action="store_false")
    parser.add_argument("--mutations", action="store_true", default=False)
    parser.add_argument(
        "--min-trades", type=int, default=20, help="Minimum trades for Stage A gate (default 20, production=50)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    log.info("Loading snapshot %s ...", SNAPSHOT_ID)
    h1_candles, h4_candles, meta = load_snapshot_parquet(SNAPSHOT_ID)
    log.info(
        "Loaded %d H1 + %d H4 candles (%s → %s)",
        len(h1_candles),
        len(h4_candles),
        meta.get("date_range_start", "?"),
        meta.get("date_range_end", "?"),
    )

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    campaign_id = f"v42_campaign_{ts}"

    wf_config = (
        WalkForwardConfig(
            train_bars=2190,  # ~3 months H1
            test_bars=730,  # ~1 month H1
            step_bars=365,  # ~2 weeks H1
            embargo_bars=5,
            holdout_bars=1095,  # ~6 weeks H1
        )
        if args.walkforward
        else None
    )

    # Relaxed gates for exploration: v4.2 produces ~32 trades on this dataset
    # with naturally concentrated PnL (top-3 = 66.7%). Production gates
    # (min_trades=50, concentration=45%) reject everything. We relax for
    # exploration and let walk-forward validation filter bad configs.
    gate_config = GateConfig(
        min_trades=args.min_trades,
        max_month_pnl_concentration=0.80,
        max_session_pnl_concentration=0.80,
    )

    campaign_config = CampaignConfig(
        campaign_id=campaign_id,
        max_experiments=args.max_experiments,
        max_wall_clock_seconds=float(args.wall_clock),
        stagnation_limit=args.stagnation,
        max_crashes=5,
        checkpoint_interval=args.checkpoint_interval,
        walkforward_on_new_best=args.walkforward,
        wf_config=wf_config,
        enable_mutations=args.mutations,
        gate_config=gate_config,
    )

    log.info(
        "Campaign %s: %d max experiments, %ds wall clock, WF=%s, mutations=%s",
        campaign_id,
        args.max_experiments,
        args.wall_clock,
        args.walkforward,
        args.mutations,
    )

    t0 = time.monotonic()
    result = start_campaign(
        config_path=CONFIG_PATH,
        campaign_config=campaign_config,
        h1_candles=h1_candles,
        h4_candles=h4_candles,
        dataset_hash=SNAPSHOT_ID,
    )
    elapsed = time.monotonic() - t0

    # Save summary
    out_dir = Path("/home/nova/nova-core/data/campaigns") / campaign_id
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "campaign_id": campaign_id,
        "snapshot_id": SNAPSHOT_ID,
        "config": str(CONFIG_PATH),
        "total_experiments": result.total_experiments,
        "best_experiment_id": result.best_experiment_id,
        "best_scout_score": result.best_scout_score,
        "best_config": result.best_config,
        "end_reason": result.end_reason,
        "elapsed_seconds": round(elapsed, 1),
        "improvements": result.improvements,
        "status_counts": result.status_counts,
    }
    summary_path = out_dir / "campaign_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    log.info("Summary saved to %s", summary_path)

    print(format_campaign_report(result))


if __name__ == "__main__":
    main()

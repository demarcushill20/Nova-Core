"""NovaTrade storage layer -- snapshots, experiment DB, artifacts, identity."""

from __future__ import annotations

from novatrade.storage.artifacts import (
    load_config_snapshot,
    save_config_snapshot,
    save_experiment_log,
    save_trade_log,
)
from novatrade.storage.data_snapshot import (
    DataSnapshot,
    fingerprint_candles,
    list_snapshots,
    load_snapshot,
    save_snapshot,
)
from novatrade.storage.experiment_db import ExperimentDB, ExperimentRecord
from novatrade.storage.experiment_identity import (
    compute_config_hash,
    compute_doctrine_hash,
    compute_experiment_id,
    compute_experiment_id_v2,
    get_engine_sha,
)

__all__ = [
    # data_snapshot
    "DataSnapshot",
    # experiment_db
    "ExperimentDB",
    "ExperimentRecord",
    # experiment_identity
    "compute_config_hash",
    "compute_doctrine_hash",
    "compute_experiment_id",
    "compute_experiment_id_v2",
    "fingerprint_candles",
    "get_engine_sha",
    "list_snapshots",
    # artifacts
    "load_config_snapshot",
    "load_snapshot",
    "save_config_snapshot",
    "save_experiment_log",
    "save_snapshot",
    "save_trade_log",
]

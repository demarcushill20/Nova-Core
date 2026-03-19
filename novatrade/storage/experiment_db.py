"""SQLite experiment storage -- canonical record for all experiments.

Provides durable, queryable storage for experiment results, gate outcomes,
metrics, and campaign lineage.  Uses stdlib sqlite3 with no ORM.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_VALID_STATUSES = frozenset(
    {
        "valid",
        "crashed",
        "gated_a",
        "gated_b",
        "gated_c",
        "ranked",
        "promoted",
        "rejected",
    }
)


@dataclass
class ExperimentRecord:
    """Single experiment result row."""

    experiment_id: str
    parent_experiment_id: str | None = None
    campaign_id: str | None = None
    strategy_config_hash: str = ""
    doctrine_hash: str = ""
    dataset_hash: str = ""
    engine_sha: str = ""
    gate_a_passed: bool | None = None
    gate_b_passed: bool | None = None
    gate_c_passed: bool | None = None
    gate_results_json: str = "{}"
    metrics_json: str = "{}"
    scout_score: float | None = None
    promotion_score: float | None = None
    trade_count: int = 0
    profit_factor: float | None = None
    max_drawdown_pct: float | None = None
    status: str = "valid"
    created_at: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())
    notes: str = ""

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status '{self.status}'. Must be one of: {sorted(_VALID_STATUSES)}")


_COLUMNS = [
    "experiment_id",
    "parent_experiment_id",
    "campaign_id",
    "strategy_config_hash",
    "doctrine_hash",
    "dataset_hash",
    "engine_sha",
    "gate_a_passed",
    "gate_b_passed",
    "gate_c_passed",
    "gate_results_json",
    "metrics_json",
    "scout_score",
    "promotion_score",
    "trade_count",
    "profit_factor",
    "max_drawdown_pct",
    "status",
    "created_at",
    "notes",
]

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id        TEXT PRIMARY KEY,
    parent_experiment_id TEXT,
    campaign_id          TEXT,
    strategy_config_hash TEXT NOT NULL,
    doctrine_hash        TEXT NOT NULL,
    dataset_hash         TEXT NOT NULL,
    engine_sha           TEXT NOT NULL,
    gate_a_passed        INTEGER,
    gate_b_passed        INTEGER,
    gate_c_passed        INTEGER,
    gate_results_json    TEXT NOT NULL DEFAULT '{}',
    metrics_json         TEXT NOT NULL DEFAULT '{}',
    scout_score          REAL,
    promotion_score      REAL,
    trade_count          INTEGER NOT NULL DEFAULT 0,
    profit_factor        REAL,
    max_drawdown_pct     REAL,
    status               TEXT NOT NULL DEFAULT 'valid',
    created_at           TEXT NOT NULL,
    notes                TEXT NOT NULL DEFAULT ''
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_campaign ON experiments(campaign_id);",
    "CREATE INDEX IF NOT EXISTS idx_status ON experiments(status);",
    "CREATE INDEX IF NOT EXISTS idx_scout ON experiments(scout_score);",
]


class ExperimentDB:
    """SQLite-backed experiment storage."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            self._conn.execute(idx_sql)
        self._conn.commit()

    # -- write --

    def insert(self, record: ExperimentRecord) -> None:
        """Insert an experiment record. Raises on duplicate experiment_id."""
        placeholders = ", ".join(["?"] * len(_COLUMNS))
        col_names = ", ".join(_COLUMNS)
        values = self._record_to_tuple(record)
        self._conn.execute(
            f"INSERT INTO experiments ({col_names}) VALUES ({placeholders})",  # noqa: S608 — col_names are from static _COLUMNS list
            values,
        )
        self._conn.commit()

    # -- read --

    def get(self, experiment_id: str) -> ExperimentRecord | None:
        """Fetch a single experiment by ID."""
        row = self._conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_best(
        self,
        campaign_id: str,
        metric: str = "scout_score",
    ) -> ExperimentRecord | None:
        """Return the top-scoring experiment in a campaign (non-NULL metric only)."""
        allowed_metrics = {"scout_score", "promotion_score", "profit_factor"}
        if metric not in allowed_metrics:
            raise ValueError(f"metric must be one of {sorted(allowed_metrics)}")
        row = self._conn.execute(
            f"SELECT * FROM experiments WHERE campaign_id = ? AND {metric} IS NOT NULL ORDER BY {metric} DESC LIMIT 1",  # noqa: S608 — metric is allowlisted above
            (campaign_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def list_by_campaign(
        self,
        campaign_id: str,
        limit: int = 100,
    ) -> list[ExperimentRecord]:
        """List experiments in a campaign, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM experiments WHERE campaign_id = ? ORDER BY created_at DESC LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count_by_campaign(self, campaign_id: str) -> int:
        """Count total experiments in a campaign."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        return int(row[0])

    def count_by_status(self, campaign_id: str) -> dict[str, int]:
        """Count experiments per status within a campaign."""
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM experiments WHERE campaign_id = ? GROUP BY status",
            (campaign_id,),
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    def get_leaderboard(
        self,
        campaign_id: str | None = None,
        limit: int = 20,
    ) -> list[ExperimentRecord]:
        """Top experiments ranked by scout_score (status=ranked or promoted)."""
        if campaign_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM experiments "
                "WHERE campaign_id = ? AND status IN ('ranked', 'promoted') "
                "AND scout_score IS NOT NULL "
                "ORDER BY scout_score DESC LIMIT ?",
                (campaign_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM experiments "
                "WHERE status IN ('ranked', 'promoted') "
                "AND scout_score IS NOT NULL "
                "ORDER BY scout_score DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def export_tsv(self, campaign_id: str, output_path: Path) -> int:
        """Export campaign experiments as Karpathy-compatible TSV. Returns row count."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        rows = self._conn.execute(
            "SELECT experiment_id, scout_score, profit_factor, max_drawdown_pct, "
            "trade_count, status, strategy_config_hash "
            "FROM experiments WHERE campaign_id = ? ORDER BY scout_score DESC",
            (campaign_id,),
        ).fetchall()

        header = "experiment_id\tscout_score\tprofit_factor\tmax_drawdown_pct\ttrade_count\tstatus\tconfig_hash"
        lines = [header]
        for row in rows:
            vals = [str(v) if v is not None else "" for v in row]
            lines.append("\t".join(vals))

        output_path.write_text("\n".join(lines) + "\n")
        return len(rows)

    # -- helpers --

    @staticmethod
    def _record_to_tuple(rec: ExperimentRecord) -> tuple:
        d = asdict(rec)
        return tuple(
            _bool_to_int(d[col]) if col.startswith("gate_") and col.endswith("_passed") else d[col] for col in _COLUMNS
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ExperimentRecord:
        d = dict(row)
        # SQLite stores bools as integers; convert back
        for gate_col in ("gate_a_passed", "gate_b_passed", "gate_c_passed"):
            val = d.get(gate_col)
            d[gate_col] = None if val is None else bool(val)
        return ExperimentRecord(**d)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self) -> ExperimentDB:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _bool_to_int(val: bool | None) -> int | None:
    if val is None:
        return None
    return 1 if val else 0

"""Reproducibility manifest — captures every version needed to reproduce a result.

Every backtest result should be reproducible from:
  strategy_config + dataset_snapshot + sacred_models + engine_version + doctrine_version

This module creates a frozen, serializable manifest that records all of those
inputs. The manifest is saved alongside campaign artifacts so any future
operator can verify exactly what produced a given result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from novatrade.evaluation.sacred_registry import SacredBundle, collect_sacred_hashes
from novatrade.storage.experiment_identity import (
    compute_config_hash,
    compute_doctrine_hash,
    get_engine_sha,
)


@dataclass(frozen=True)
class ReproducibilityManifest:
    """Immutable record of every input needed to reproduce a backtest.

    Two experiments with identical manifest_hash used identical inputs
    and should produce identical outputs (barring floating-point
    non-determinism, which is bounded and negligible for trading metrics).
    """

    # Strategy identity
    strategy_config_hash: str
    strategy_name: str
    strategy_version: str

    # Dataset identity
    dataset_hash: str
    dataset_symbol: str
    dataset_date_range: str

    # Engine identity
    engine_sha: str

    # Doctrine identity
    doctrine_hash: str

    # Sacred model bundle (all evaluation assumptions)
    sacred_bundle: dict = field(default_factory=dict)

    # Campaign context
    campaign_id: str = ""

    # Metadata
    created_at: str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())

    # Combined manifest hash — one hash to rule them all
    manifest_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def create_manifest(
    config_dict: dict,
    strategy_name: str,
    strategy_version: str,
    dataset_hash: str,
    dataset_symbol: str,
    dataset_date_range: str,
    doctrine_path: Path | str | None = None,
    doctrine_hash: str | None = None,
    campaign_id: str = "",
    sacred_bundle: SacredBundle | None = None,
) -> ReproducibilityManifest:
    """Build a reproducibility manifest from current state.

    Args:
        config_dict: Strategy config as a dictionary (for hashing).
        strategy_name: Human-readable strategy name.
        strategy_version: Strategy config version string.
        dataset_hash: Pre-computed dataset fingerprint (from data_snapshot).
        dataset_symbol: Trading pair (e.g. "EURUSD").
        dataset_date_range: Human-readable date range (e.g. "2020-01-01..2025-12-31").
        doctrine_path: Path to doctrine file (optional if doctrine_hash provided).
        doctrine_hash: Pre-computed doctrine hash (optional if path provided).
        campaign_id: Campaign identifier for grouping.
        sacred_bundle: Pre-collected sacred model hashes (collects defaults if None).

    Returns:
        Frozen ReproducibilityManifest with a deterministic manifest_hash.
    """
    cfg_hash = compute_config_hash(config_dict)
    eng_sha = get_engine_sha()

    if doctrine_hash is None and doctrine_path is not None:
        doc_hash = compute_doctrine_hash(Path(doctrine_path))
    elif doctrine_hash is not None:
        doc_hash = doctrine_hash
    else:
        doc_hash = "no_doctrine"

    bundle = sacred_bundle or collect_sacred_hashes()
    bundle_dict = bundle.to_dict()

    # Deterministic manifest hash from all inputs
    manifest_parts = f"{cfg_hash}{dataset_hash}{eng_sha}{doc_hash}{bundle.combined_hash}"
    m_hash = hashlib.sha256(manifest_parts.encode()).hexdigest()[:16]

    return ReproducibilityManifest(
        strategy_config_hash=cfg_hash,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        dataset_hash=dataset_hash,
        dataset_symbol=dataset_symbol,
        dataset_date_range=dataset_date_range,
        engine_sha=eng_sha,
        doctrine_hash=doc_hash,
        sacred_bundle=bundle_dict,
        campaign_id=campaign_id,
        manifest_hash=m_hash,
    )


def save_manifest(manifest: ReproducibilityManifest, output_dir: Path) -> Path:
    """Persist manifest as JSON alongside campaign artifacts.

    File: {output_dir}/manifest_{manifest_hash}.json
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"manifest_{manifest.manifest_hash}.json"
    path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return path


def load_manifest(path: Path) -> ReproducibilityManifest:
    """Load a manifest from JSON file."""
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed manifest JSON at {path}: {exc}") from exc
    return ReproducibilityManifest(**data)


def compare_manifests(a: ReproducibilityManifest, b: ReproducibilityManifest) -> dict[str, tuple[str, str]]:
    """Compare two manifests and return fields that differ.

    Returns a dict of {field_name: (a_value, b_value)} for mismatches.
    An empty dict means the manifests are identical.
    """
    diffs: dict[str, tuple[str, str]] = {}
    fields_to_check = [
        "strategy_config_hash",
        "dataset_hash",
        "engine_sha",
        "doctrine_hash",
        "manifest_hash",
    ]
    for f in fields_to_check:
        va = getattr(a, f)
        vb = getattr(b, f)
        if va != vb:
            diffs[f] = (str(va), str(vb))

    # Check sacred bundle combined hash
    a_combined = a.sacred_bundle.get("combined_hash", "")
    b_combined = b.sacred_bundle.get("combined_hash", "")
    if a_combined != b_combined:
        diffs["sacred_bundle.combined_hash"] = (a_combined, b_combined)

    return diffs

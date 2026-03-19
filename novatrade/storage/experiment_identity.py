"""Deterministic experiment identity from content hashes.

Every experiment is uniquely identified by the SHA-256 combination of its
strategy config, dataset, sacred model bundle, engine revision, and doctrine.
This guarantees that re-running the same experiment produces the same ID,
enabling deduplication and lineage tracking.

The experiment ID now includes ALL sacred model hashes (not just fill model)
so that any change to spread, slippage, commission, session calendar, intrabar
policy, or metrics computation causes a new experiment ID.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


def compute_experiment_id(
    config_hash: str,
    dataset_hash: str,
    fill_model_hash: str,
    engine_sha: str,
    doctrine_hash: str,
) -> str:
    """Derive a deterministic experiment ID from five content hashes.

    Returns the first 16 hex characters of the SHA-256 digest.

    Note: This is the original 5-tuple identity function, preserved for
    backward compatibility. New code should prefer compute_experiment_id_v2
    which includes ALL sacred model hashes.
    """
    combined = f"{config_hash}{dataset_hash}{fill_model_hash}{engine_sha}{doctrine_hash}"
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def compute_experiment_id_v2(
    config_hash: str,
    dataset_hash: str,
    sacred_combined_hash: str,
    engine_sha: str,
    doctrine_hash: str,
) -> str:
    """Derive a deterministic experiment ID from config + dataset + ALL sacred models.

    Unlike v1 (which only included fill_model_hash), this function uses the
    combined hash from the sacred registry, which covers fill, spread,
    slippage, commission, session calendar, intrabar policy, and metrics.

    Returns the first 16 hex characters of the SHA-256 digest.
    """
    combined = f"{config_hash}{dataset_hash}{sacred_combined_hash}{engine_sha}{doctrine_hash}"
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def compute_config_hash(config_dict: dict) -> str:
    """SHA-256 of JSON-serialized config (sorted keys).

    Returns the first 16 hex characters.
    """
    canonical = json.dumps(config_dict, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def compute_doctrine_hash(doctrine_path: Path) -> str:
    """SHA-256 of the doctrine file contents.

    Returns the first 16 hex characters.

    Raises FileNotFoundError if the doctrine file does not exist.
    """
    doctrine_path = Path(doctrine_path)
    if not doctrine_path.exists():
        raise FileNotFoundError(f"Doctrine file not found: {doctrine_path}")
    content = doctrine_path.read_bytes()
    return hashlib.sha256(content).hexdigest()[:16]


def get_engine_sha() -> str:
    """Get the current git HEAD SHA from /home/nova/nova-core.

    Returns the first 12 characters of the commit hash.
    Falls back to "unknown" if git is unavailable or the directory
    is not a git repository.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd="/home/nova/nova-core",
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:12]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return "unknown"

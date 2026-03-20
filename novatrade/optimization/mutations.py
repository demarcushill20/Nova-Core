"""Level 2/3 mutation generators and ancestry tracking.

P4.1: Level 2 filter mutations — toggle registered filters, threshold presets,
      exit variants, and session filters according to doctrine constraints.
P4.2: Level 3 structural templates — swap composition patterns from the
      registered template library.
P4.3: Ancestry tracking — record every mutation for lineage audit.
P4.4: Campaign integration helper — single entry point for the campaign engine
      to request a mutated candidate at the appropriate search level.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from novatrade.cli.config_schema import StrategyConfig
from novatrade.doctrine.schema import StrategyDoctrine
from novatrade.optimization.search_levels import (
    EXIT_RULE_VARIANTS,
    REGISTERED_COMPOSITIONS,
    REGISTERED_FILTERS,
    SESSION_FILTER_CHOICES,
    THRESHOLD_PRESETS,
    Level2Toggle,
    Level3Template,
    SearchLevel,
    SearchLevelConfig,
    apply_level2_toggles,
)
from novatrade.optimization.strategies import (
    generate_local_candidate,
)

log = logging.getLogger("novatrade.optimization.mutations")


# ---------------------------------------------------------------------------
# Default mutation budget — fraction of experiments allocated to each level
# ---------------------------------------------------------------------------


@dataclass
class MutationBudget:
    """Fraction of experiments allocated to each mutation level.

    Within a campaign, experiments are assigned to mutation levels based
    on these weights.  Level 1 is always the majority — parameter
    perturbation is the workhorse.  Level 2/3 are occasional probes.
    """

    level1_pct: float = 0.70  # parameter perturbation
    level2_pct: float = 0.20  # filter / rule toggles
    level3_pct: float = 0.10  # structural template swaps

    def __post_init__(self) -> None:
        total = self.level1_pct + self.level2_pct + self.level3_pct
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"Mutation budget must sum to ~1.0, got {total:.2f}")


DEFAULT_MUTATION_BUDGET = MutationBudget()


# ---------------------------------------------------------------------------
# P4.1: Level 2 — filter mutations
# ---------------------------------------------------------------------------


def generate_filter_mutation(
    base_params: dict[str, Any],
    doctrine: StrategyDoctrine | None = None,
    seed: int | None = None,
) -> tuple[dict[str, Any], list[Level2Toggle]]:
    """Generate a Level 2 mutation: toggle filters, presets, exit variants.

    Respects doctrine constraints:
    - Mandatory filters are never disabled.
    - Forbidden filters are never enabled.
    - Optional filters may be toggled.

    If no doctrine is provided, all registered filters are considered
    optional (unconstrained mode).

    Args:
        base_params: Current parameter dict (will not be mutated).
        doctrine: Optional strategy doctrine for filter constraints.
        seed: RNG seed for reproducibility.

    Returns:
        Tuple of (new_params, toggles_applied).
    """
    rng = random.Random(seed)  # noqa: S311 — not used for crypto

    mandatory = set(doctrine.filters_mandatory) if doctrine else set()
    forbidden = set(doctrine.filters_forbidden) if doctrine else set()
    optional = set(doctrine.filters_optional) if doctrine else set(REGISTERED_FILTERS.keys())

    # If doctrine doesn't specify optional, everything not mandatory/forbidden is optional
    if doctrine and not doctrine.filters_optional:
        optional = set(REGISTERED_FILTERS.keys()) - mandatory - forbidden

    toggles: list[Level2Toggle] = []

    # 1. Toggle 1-3 optional filters
    toggleable = list(optional - mandatory - forbidden)
    if toggleable:
        n_toggles = rng.randint(1, min(3, len(toggleable)))
        chosen = rng.sample(toggleable, n_toggles)
        for name in chosen:
            enabled = rng.choice([True, False])
            toggles.append(Level2Toggle(toggle_type="filter", name=name, enabled=enabled))

    # 2. Maybe apply a threshold preset (30% chance)
    if rng.random() < 0.30:
        preset = rng.choice(list(THRESHOLD_PRESETS.keys()))
        toggles.append(Level2Toggle(toggle_type="threshold_preset", name="preset", value=preset))

    # 3. Maybe switch exit variant (25% chance)
    if rng.random() < 0.25:
        variant = rng.choice(list(EXIT_RULE_VARIANTS.keys()))
        toggles.append(Level2Toggle(toggle_type="exit_variant", name=variant))

    # 4. Maybe change session filter (20% chance)
    if rng.random() < 0.20:
        session = rng.choice(SESSION_FILTER_CHOICES)
        toggles.append(Level2Toggle(toggle_type="session", name="session", value=session))

    # Ensure mandatory filters stay enabled
    for name in mandatory:
        toggles.append(Level2Toggle(toggle_type="filter", name=name, enabled=True))

    new_params = apply_level2_toggles(base_params, toggles)
    return new_params, toggles


# ---------------------------------------------------------------------------
# P4.2: Level 3 — structural template mutations
# ---------------------------------------------------------------------------

# Structural templates — pre-registered compositions the agent can swap to.
STRUCTURAL_TEMPLATES = list(REGISTERED_COMPOSITIONS.keys())


def generate_structural_mutation(
    base_params: dict[str, Any],
    seed: int | None = None,
) -> tuple[dict[str, Any], Level3Template]:
    """Generate a Level 3 mutation: swap the structural composition template.

    Picks a random registered composition and constrains the parameter
    space to that template's active indicators.  Parameters not belonging
    to the new template's indicators are dropped.

    Args:
        base_params: Current parameter dict (will not be mutated).
        seed: RNG seed for reproducibility.

    Returns:
        Tuple of (new_params, template_selected).
    """
    rng = random.Random(seed)  # noqa: S311 — not used for crypto
    composition = rng.choice(STRUCTURAL_TEMPLATES)
    template = Level3Template(composition=composition)

    # Get the params this template allows
    from novatrade.optimization.search_levels import get_active_params_for_template

    active_params = get_active_params_for_template(template)

    # Build new params — keep only what the template allows
    new_params = {}
    for key in active_params:
        if key in base_params:
            new_params[key] = base_params[key]
        else:
            # Use defaults from StrategyConfig bounds
            bounds = StrategyConfig.PARAMETER_BOUNDS
            if key in bounds:
                b = bounds[key]
                new_params[key] = (b.min_val + b.max_val) / 2

    # Also do a small perturbation on the surviving params
    perturbed = generate_local_candidate(new_params, params=active_params, scale=0.08, seed=seed)
    new_params.update(perturbed)

    return new_params, template


# ---------------------------------------------------------------------------
# P4.3: Ancestry tracking
# ---------------------------------------------------------------------------


@dataclass
class MutationRecord:
    """Single mutation event in experiment ancestry."""

    experiment_id: str
    parent_id: str | None
    mutation_type: str  # "level1_perturbation", "level2_filter", "level3_structural"
    mutation_details: dict[str, Any] = field(default_factory=dict)
    scout_score: float | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MutationRecord:
        return cls(**data)


class AncestryTracker:
    """Tracks mutation lineage across a campaign.

    Maintains a list of MutationRecords that captures:
    - Which experiment spawned which
    - What type of mutation was applied
    - The mutation details (toggles, template, perturbation scale)

    Supports JSON round-trip for persistence.
    """

    def __init__(self) -> None:
        self._records: list[MutationRecord] = []
        self._by_id: dict[str, MutationRecord] = {}

    def record(self, rec: MutationRecord) -> None:
        """Add a mutation record."""
        self._records.append(rec)
        self._by_id[rec.experiment_id] = rec

    def lineage(self, experiment_id: str) -> list[MutationRecord]:
        """Trace the full ancestry chain from experiment_id back to root.

        Returns list ordered root-first.
        """
        chain: list[MutationRecord] = []
        current = experiment_id
        seen: set[str] = set()

        while current and current in self._by_id and current not in seen:
            seen.add(current)
            rec = self._by_id[current]
            chain.append(rec)
            current = rec.parent_id  # type: ignore[assignment]

        chain.reverse()
        return chain

    def children(self, parent_id: str) -> list[MutationRecord]:
        """Get all direct children of a parent experiment."""
        return [r for r in self._records if r.parent_id == parent_id]

    @property
    def records(self) -> list[MutationRecord]:
        """All recorded mutations."""
        return list(self._records)

    def to_json(self) -> str:
        """Serialize all records to JSON."""
        return json.dumps([r.to_dict() for r in self._records], indent=2)

    @classmethod
    def from_json(cls, data: str) -> AncestryTracker:
        """Deserialize from JSON."""
        tracker = cls()
        for item in json.loads(data):
            tracker.record(MutationRecord.from_dict(item))
        return tracker

    def save(self, path: Path) -> None:
        """Save ancestry to a JSON file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json())

    @classmethod
    def load(cls, path: Path) -> AncestryTracker:
        """Load ancestry from a JSON file."""
        return cls.from_json(Path(path).read_text())

    def __len__(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# P4.4: Campaign integration helper
# ---------------------------------------------------------------------------


def compute_mutation_diff(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Compute the diff between two parameter dicts.

    Returns a dict with keys that changed, each mapping to
    {"before": old_val, "after": new_val}.
    """
    diff: dict[str, Any] = {}
    all_keys = set(before.keys()) | set(after.keys())
    for key in sorted(all_keys):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            diff[key] = {"before": old, "after": new}
    return diff


def select_mutation_level(
    experiment_index: int,
    total_experiments: int,
    budget: MutationBudget | None = None,
    search_level_config: SearchLevelConfig | None = None,
) -> SearchLevel:
    """Determine which mutation level to use for an experiment.

    Uses the budget to assign levels deterministically based on
    experiment index.  If search_level_config limits the max level,
    clamps down accordingly.

    Args:
        experiment_index: Zero-based experiment number.
        total_experiments: Total experiments in the campaign.
        budget: Mutation budget allocation.
        search_level_config: Optional level constraint.

    Returns:
        The SearchLevel to use.
    """
    b = budget or DEFAULT_MUTATION_BUDGET
    n = max(total_experiments, 1)

    # Determine allowed max level
    max_level = SearchLevel.LEVEL_3
    if search_level_config:
        max_level = search_level_config.level

    cut_l1 = round(b.level1_pct * n)
    cut_l2 = round((b.level1_pct + b.level2_pct) * n)

    if experiment_index < cut_l1:
        return SearchLevel.LEVEL_1
    if experiment_index < cut_l2:
        level = SearchLevel.LEVEL_2
    else:
        level = SearchLevel.LEVEL_3

    # Clamp to max allowed level
    level_order = {SearchLevel.LEVEL_1: 1, SearchLevel.LEVEL_2: 2, SearchLevel.LEVEL_3: 3}
    if level_order[level] > level_order[max_level]:
        return max_level
    return level


def generate_mutated_candidate(
    best_config: dict[str, Any],
    search_level: SearchLevel,
    doctrine: StrategyDoctrine | None = None,
    search_level_config: SearchLevelConfig | None = None,
    seed: int | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Generate a mutated candidate at the given search level.

    This is the main entry point for the campaign engine. It dispatches
    to the appropriate mutation generator based on the search level.

    Args:
        best_config: Current best parameter dict.
        search_level: Which level of mutation to apply.
        doctrine: Optional doctrine for filter constraints.
        search_level_config: Optional level config for param constraints.
        seed: RNG seed for reproducibility.

    Returns:
        Tuple of (new_params, mutation_type, mutation_details).
        - new_params: The mutated parameter dict.
        - mutation_type: String label ("level1_perturbation", "level2_filter",
          "level3_structural").
        - mutation_details: Dict with specifics of what was mutated.
    """
    if search_level == SearchLevel.LEVEL_1:
        # Determine which params are optimisable
        params = None
        if search_level_config and search_level_config.level == SearchLevel.LEVEL_3:
            params = search_level_config.get_optimisable_params()
        new_params = generate_local_candidate(best_config, params=params, scale=0.05, seed=seed)
        diff = compute_mutation_diff(best_config, new_params)
        return new_params, "level1_perturbation", {"diff": diff}

    if search_level == SearchLevel.LEVEL_2:
        new_params, toggles = generate_filter_mutation(
            best_config,
            doctrine=doctrine,
            seed=seed,
        )
        toggle_details = [
            {"type": t.toggle_type, "name": t.name, "enabled": t.enabled, "value": t.value} for t in toggles
        ]
        return new_params, "level2_filter", {"toggles": toggle_details}

    if search_level == SearchLevel.LEVEL_3:
        new_params, template = generate_structural_mutation(best_config, seed=seed)
        return (
            new_params,
            "level3_structural",
            {
                "composition": template.composition,
                "indicator_overrides": template.indicator_overrides,
            },
        )

    # Fallback — shouldn't happen but safe default
    new_params = generate_local_candidate(best_config, scale=0.05, seed=seed)
    diff = compute_mutation_diff(best_config, new_params)
    return new_params, "level1_perturbation", {"diff": diff}

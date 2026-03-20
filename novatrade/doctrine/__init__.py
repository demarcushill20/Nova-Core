"""Strategy doctrine — structured format for strategy specifications.

Public API:
    StrategyDoctrine — the canonical doctrine model
    concept_to_doctrine — translate concept text to doctrine
    doctrine_to_config — bridge doctrine to StrategyConfig
"""

from novatrade.doctrine.bridge import (
    config_to_doctrine_params,
    doctrine_to_config,
    validate_doctrine_config_compatibility,
)
from novatrade.doctrine.schema import (
    DoctrineCampaign,
    DoctrineData,
    DoctrineExecution,
    DoctrineValidation,
    ParamBound,
    StrategyDoctrine,
)
from novatrade.doctrine.translator import (
    concept_to_doctrine,
    detect_strategy_family,
)

__all__ = [
    "DoctrineCampaign",
    "DoctrineData",
    "DoctrineExecution",
    "DoctrineValidation",
    "ParamBound",
    "StrategyDoctrine",
    "concept_to_doctrine",
    "config_to_doctrine_params",
    "detect_strategy_family",
    "doctrine_to_config",
    "validate_doctrine_config_compatibility",
]

"""Strategy doctrine — structured format for strategy specifications.

Public API:
    StrategyDoctrine — the canonical doctrine model
    concept_to_doctrine — translate concept text to doctrine
    doctrine_to_config — bridge doctrine to StrategyConfig
    get_doctrine_template — load a curated template
"""

from novatrade.doctrine.bridge import (
    config_to_doctrine_params,
    doctrine_to_config,
    validate_doctrine_config_compatibility,
)
from novatrade.doctrine.library import (
    create_doctrine_from_template,
    get_doctrine_template,
    list_doctrine_templates,
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
    "create_doctrine_from_template",
    "detect_strategy_family",
    "doctrine_to_config",
    "get_doctrine_template",
    "list_doctrine_templates",
    "validate_doctrine_config_compatibility",
]

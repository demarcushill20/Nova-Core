"""NovaTrade — provider-neutral automated trading subsystem for NovaCore.

NovaTrade sources high-credibility trading ideas, translates them into strict
rule-based systems, validates under statistical scrutiny, and deploys only
prop-firm-safe strategies.  NovaCore remains the executive orchestrator.

This package defines the domain models, adapter contracts, risk gates, and
execution logic.  All broker interactions go through the MT5Adapter boundary
so the core logic stays provider-neutral.
"""

__version__ = "0.1.0"

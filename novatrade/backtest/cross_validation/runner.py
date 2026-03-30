"""Cross-validation orchestrator — runs multiple engines and compares results."""

from __future__ import annotations

import logging
import time

from novatrade.backtest.cross_validation.base_adapter import BaseEngineAdapter
from novatrade.backtest.cross_validation.comparator import CrossValidator
from novatrade.backtest.cross_validation.types import (
    ConsensusReport,
    DivergenceThresholds,
    EngineId,
    EngineResult,
)
from novatrade.backtest.environment import DEFAULT_ENVIRONMENT, BacktestEnvironment
from novatrade.models import Candle

log = logging.getLogger("novatrade.cross_validation")

# ------------------------------------------------------------------
# Adapter registry
# ------------------------------------------------------------------

ADAPTER_REGISTRY: dict[EngineId, type[BaseEngineAdapter]] = {}


def register_adapter(engine_id: EngineId, cls: type[BaseEngineAdapter]) -> None:
    ADAPTER_REGISTRY[engine_id] = cls


def get_adapter(engine_id: EngineId) -> BaseEngineAdapter:
    cls = ADAPTER_REGISTRY.get(engine_id)
    if cls is None:
        raise KeyError(f"No adapter registered for {engine_id.value}")
    return cls()


# Auto-register built-in adapters
def _auto_register() -> None:
    from novatrade.backtest.cross_validation.backtestingpy_adapter import BacktestingPyAdapter
    from novatrade.backtest.cross_validation.nautilus_adapter import NautilusAdapter
    from novatrade.backtest.cross_validation.nova_adapter import NovaEngineAdapter
    from novatrade.backtest.cross_validation.vectorbt_adapter import VectorbtAdapter

    register_adapter(EngineId.NOVA, NovaEngineAdapter)
    register_adapter(EngineId.BACKTESTING_PY, BacktestingPyAdapter)
    register_adapter(EngineId.VECTORBT, VectorbtAdapter)
    register_adapter(EngineId.NAUTILUS, NautilusAdapter)


_auto_register()


# ------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------


class CrossValidationRunner:
    """Orchestrates multi-engine backtests + comparison.

    Usage::

        runner = CrossValidationRunner()
        report = runner.run(h1_candles, h4_candles, env)
        print(report.summary)
    """

    def __init__(
        self,
        engines: list[EngineId] | None = None,
        thresholds: DivergenceThresholds | None = None,
    ):
        self.requested_engines = engines
        self.thresholds = thresholds

    def run(
        self,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        env: BacktestEnvironment | None = None,
    ) -> ConsensusReport:
        """Run cross-validation across all requested engines."""
        env = env or DEFAULT_ENVIRONMENT
        adapters = self._discover_adapters()

        if not adapters:
            return ConsensusReport(
                summary="No engines available.",
                timestamp=time.time(),
            )

        log.info(
            "Starting cross-validation with %d engines: %s",
            len(adapters),
            [a.engine_id.value for a in adapters],
        )

        results: list[EngineResult] = []
        for adapter in adapters:
            result = self._run_single(adapter, h1_candles, h4_candles, env)
            results.append(result)
            status = "OK" if result.error is None else f"FAILED: {result.error}"
            log.info(
                "  %s: %s (%.2fs)",
                adapter.engine_id.value,
                status,
                result.elapsed_seconds,
            )

        validator = CrossValidator(thresholds=self.thresholds)
        return validator.compare(results)

    def _discover_adapters(self) -> list[BaseEngineAdapter]:
        """Instantiate requested adapters, filter to available ones."""
        engine_ids = self.requested_engines or list(ADAPTER_REGISTRY.keys())
        adapters: list[BaseEngineAdapter] = []

        for eid in engine_ids:
            try:
                adapter = get_adapter(eid)
                if adapter.is_available():
                    adapters.append(adapter)
                else:
                    log.info("Engine %s not available (not installed)", eid.value)
            except KeyError:
                log.warning("Unknown engine: %s", eid.value)

        return adapters

    def _run_single(
        self,
        adapter: BaseEngineAdapter,
        h1_candles: list[Candle],
        h4_candles: list[Candle],
        env: BacktestEnvironment,
    ) -> EngineResult:
        """Run a single adapter with error handling."""
        try:
            return adapter.run(h1_candles, h4_candles, env)
        except Exception as exc:
            log.exception("Engine %s crashed", adapter.engine_id.value)
            return EngineResult(
                engine=adapter.engine_id,
                engine_version=adapter.engine_version,
                error=f"Unhandled exception: {exc}",
            )

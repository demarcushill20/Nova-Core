"""NovaTrade runtime caller layer — Phases 8-9.

Provides the operational shell around the IRB demo-run stack:
  - Webhook ingress for TradingView-style alerts
  - Monitoring loop caller (resolves B-P7-1)
  - Dry-run adapter stub and gating
  - Launch gate with dry_run / active_ready / active_demo modes
  - Operator health/status/readiness endpoints
  - Rollback to dry-run capability
  - Runner entrypoint wiring everything together
"""

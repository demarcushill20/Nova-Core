"""NovaTrade runtime caller layer — Phase 8.

Provides the minimal operational shell around the IRB demo-run stack:
  - Webhook ingress for TradingView-style alerts
  - Monitoring loop caller (resolves B-P7-1)
  - Dry-run adapter stub and gating
  - Operator health/status endpoint
  - Runner entrypoint wiring everything together
"""

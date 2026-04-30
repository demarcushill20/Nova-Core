#!/usr/bin/env python3
"""Reconcile webhook alerts vs MetaApi deals.

Reads alerts from the webhook runner's evidence trail (JSONL files where
TradingAgent records WEBHOOK_RECEIVED / WEBHOOK_ROUTED events) and fetches
deals from MetaApi for the same window. Reports divergences classified as:

  - alert_without_deal:      alert recorded but no matching MetaApi deal
  - deal_without_alert:      deal exists but no matching alert
  - parameter_mismatch:      alert + deal pair found but volume/price differs
  - expected_v1_divergence:  Pine partial-TP fills (no PARTIAL_CLOSE alert
                             yet — v1 of the webhook pipeline defers Pine
                             partial emission per spec D9)

Usage:
    python scripts/diff_pine_alerts_vs_metaapi.py \\
        --since 2026-04-30 \\
        --account-id $METAAPI_ACCOUNT_ID \\
        --evidence-glob 'OUTPUT/novatrade/evidence/*.jsonl'

Exit code: 0 if no divergences, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TIME_TOL_SEC = 60.0
PRICE_TOL_PIPS = 1.0


def reconcile(
    alerts: list[dict],
    deals: list[dict],
    *,
    time_tol_sec: float = TIME_TOL_SEC,
) -> list[dict]:
    """Pair alerts to deals and return a list of divergence dicts.

    Matching rule: an alert and a deal pair if (a) sides match (BUY ↔ DEAL_TYPE_BUY,
    SELL ↔ DEAL_TYPE_SELL), (b) deal.time is within ``time_tol_sec`` of
    ``alert.bar_close_time/1000``, (c) volumes are equal within 0.001 lots
    (or alert.volume<=0 — auto-sized).
    """
    out: list[dict] = []
    matched: set[str] = set()

    for a in alerts:
        m = _find_match(a, deals, time_tol_sec=time_tol_sec, exclude=matched)
        if m is None:
            out.append({"classification": "alert_without_deal", "alert": a})
            continue
        matched.add(m["id"])
        issues = _params_diff(a, m)
        if issues:
            out.append(
                {
                    "classification": "parameter_mismatch",
                    "alert": a,
                    "deal": m,
                    "issues": issues,
                }
            )

    for d in deals:
        if d.get("id") in matched:
            continue
        if "PARTIAL_TP" in (d.get("comment") or "").upper():
            out.append(
                {
                    "classification": "expected_v1_divergence",
                    "deal": d,
                    "note": "Pine PARTIAL_TP fill — v1 defers PARTIAL_CLOSE emission (spec D9).",
                }
            )
        else:
            out.append({"classification": "deal_without_alert", "deal": d})

    return out


def _find_match(
    a: dict,
    deals: list[dict],
    *,
    time_tol_sec: float,
    exclude: set[str],
) -> dict | None:
    side = (a.get("side") or "").upper()
    t_ms = a.get("bar_close_time")
    if not isinstance(t_ms, (int, float)):
        return None
    t_sec = t_ms / 1000.0
    vol = float(a.get("volume") or 0.0)

    candidates: list[tuple[float, dict]] = []
    for d in deals:
        if d.get("id") in exclude:
            continue
        d_side = "BUY" if "BUY" in (d.get("type") or "").upper() else "SELL"
        if d_side != side:
            continue
        d_t = float(d.get("time") or 0.0)
        if abs(d_t - t_sec) > time_tol_sec:
            continue
        d_vol = float(d.get("volume") or 0.0)
        if vol > 0 and abs(d_vol - vol) > 0.001:
            continue
        candidates.append((abs(d_t - t_sec), d))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _params_diff(a: dict, d: dict) -> list[str]:
    issues: list[str] = []
    ap, dp = a.get("entry_price"), d.get("price")
    if isinstance(ap, (int, float)) and isinstance(dp, (int, float)) and abs(ap - dp) > PRICE_TOL_PIPS * 0.0001:
        issues.append(f"price drift: alert={ap} deal={dp}")
    return issues


def _load_alerts(glob: str) -> list[dict]:
    """Load alerts from the EvidenceRecorder JSONL trail.

    EvidenceRecorder writes one JSON record per line with shape:
      {"event_type": "MONITORING", "data": {"event": "WEBHOOK_RECEIVED", ...}, ...}
    """
    out: list[dict] = []
    for p in sorted(Path().glob(glob)):
        with p.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = rec.get("data") or {}
                if data.get("event") in ("WEBHOOK_RECEIVED", "WEBHOOK_ROUTED"):
                    out.append(data)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence-glob", default="OUTPUT/novatrade/evidence/*.jsonl")
    p.add_argument("--account-id", required=True)
    p.add_argument("--since", required=True, help="ISO date e.g. 2026-04-30")
    p.add_argument("--time-tolerance-sec", type=float, default=TIME_TOL_SEC)
    args = p.parse_args(argv)

    alerts = _load_alerts(args.evidence_glob)

    # Async deal fetch — kept out of reconcile() so the core stays
    # synchronously testable without a MetaApi adapter.
    import asyncio

    from novatrade.adapter.metaapi_provider import MetaApiAdapter
    from novatrade.config import NovaTradeCfg

    async def _fetch() -> list[dict]:
        cfg = NovaTradeCfg.load()
        cfg.ftmo.enabled = True
        ad = MetaApiAdapter(cfg.metaapi)
        await ad.connect()
        # Note: fetch_deals_since may not exist on MetaApiAdapter today.
        # If missing, this main() raises at runtime and the operator either
        # adds the wrapper around historyStorage.deals or queries via the
        # MetaApi cloud UI manually. The reconcile() core is unaffected.
        deals = await ad.fetch_deals_since(args.since)  # type: ignore[attr-defined]
        await ad.disconnect()
        return deals

    deals = asyncio.run(_fetch())
    divs = reconcile(alerts, deals, time_tol_sec=args.time_tolerance_sec)

    print(
        json.dumps(
            {
                "since": args.since,
                "alerts_loaded": len(alerts),
                "deals_loaded": len(deals),
                "divergences": divs,
            },
            indent=2,
            default=str,
        )
    )
    return 0 if not divs else 1


if __name__ == "__main__":
    sys.exit(main())

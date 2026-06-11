# Autoresearch loop

A disciplined, Karpathy-style **propose → backtest → score → refine** loop for
automated strategy discovery — built so the **scoring function is the
anti-overfitting machine**, not an afterthought.

> Naive automated search on market data is a mirage generator. Run 10,000
> backtests, pick the best, and you get a spectacular in-sample curve that is
> pure luck (the multiple-testing problem). This loop exists to *not* do that.

## Run

```bash
python -m novatrade.research.autoresearch.run --rounds 2 --cost 0.30
# -> tiered leaderboard + OUTPUT/autoresearch/leaderboard.json
```

Data: `data/candles/eurusd_15m.parquet` (auto-built from HistData M1 if absent).

## The four anti-overfitting guards

1. **Sealed hold-out** — the most-recent 30% of history. Selection *only* sees
   `train`; survivors touch the hold-out exactly once. This is the binding
   filter — candidates with great train Sharpe but collapsing OOS are rejected.
2. **Sub-period consistency** — fraction of calendar years with positive net R.
   A Sharpe concentrated in one regime is fragile and fails here.
3. **Deflated Sharpe Ratio** (López de Prado) — discounts the best result for the
   loop's *total trial count*. The loop counts its own attempts, so the DSR bar
   gets stricter the harder it searches. (Reuses
   `novatrade.backtest.research.walkforward.deflated_sharpe_ratio`.)
4. **Mechanism gate** — a candidate only reaches **deploy** tier if its family is
   `grounded` (a structural reason: flow / session seasonality). Ungrounded
   families (indicator patterns) cap at **watch**, however good the curve looks.

## Tiers

| tier | meaning |
|------|---------|
| **deploy** | passed train Sharpe + positive-year consistency + DSR, confirmed on the sealed hold-out, **and** mechanism-grounded |
| **watch** | strong but ungrounded (no structural reason) — or not yet hold-out-confirmed |
| **reject** | failed a gate (the large majority) |

## Validation (it must rediscover the known truth)

On EURUSD it autonomously:
- **rediscovers** the validated edge — top deploy candidate is `hour_drift,
  direction=-1, hour=11` (short EUR 11:00 UTC), and it found that a **2-hour
  hold (11:00–13:00)** beats the hand-built 1-hour version (train Sharpe 1.21,
  hold-out OOS **1.30**, 93% positive years);
- **rejects** the cost-mirage families — every `mr_fade` (train Sharpe −1.6 to
  −3.0) and `breakout` (−0.05 to −0.89) candidate;
- **catches overfits** — a drift candidate with train Sharpe **1.50** was
  rejected because its hold-out fell to 0.24. The loop does not trust its
  luckiest draw.

If it couldn't do those three things, it couldn't be trusted to find anything new.

## Known limitations

- **DSR saturates** with ~14 years of daily observations: any positive Sharpe is
  "significant" even after the trial penalty, so DSR ≈ 1.00 for all real
  candidates and the **sealed hold-out + positive-year consistency are the real
  binding filters**. DSR matters more with shorter samples / fewer observations.
- **Stop handling in the drift families is conservative** (hourly worst-case),
  not tick-level. A `deploy` candidate still gets a tick-accurate confirmation
  outside this loop before it goes near a demo.
- The search space is a starting set (drift + two control families). The point
  is the *scoring contract*, not the breadth — see below.

## The LLM-proposer layer (the "Karpathy" loop) — `proposer.py`

An LLM reads the interim leaderboard and proposes the next batch of candidates,
scored through the same gauntlet:

```bash
python -m novatrade.research.autoresearch.run --rounds 2 --llm   # needs ANTHROPIC_API_KEY
```

`run_search(..., proposer=...)` takes any proposer; `LLMProposer(complete_fn)` is
transport-agnostic (Anthropic SDK via `anthropic_complete_fn`, the `claude` CLI,
a sub-agent, or a test stub). Two invariants keep the **judge incorruptible**:

1. **`grounded` is forced from family membership, never from the LLM** — a
   proposer cannot label a `breakout` "structural" to slip past the mechanism
   gate. `parse_proposals` enforces this.
2. The LLM may only parameterise EXISTING families; genuinely new mechanisms come
   back as free-text `new_family_request`s for a human/coding-agent to implement
   (it cannot inject a backtest function).

**Demonstrated:** a Claude proposer offered 10 economically-plausible candidates
(WM/R-fix flow, ECB fix, US own-hours, post-fix reversion). The gauntlet rejected
**all** of them — the two that looked good in-sample (train Sharpe 0.6-0.7)
collapsed on the sealed hold-out (OOS −0.5). A persuasive narrative cannot beat
the hold-out. The proposer expands the search; the judge keeps it honest.

To add a family (so the LLM can target it): implement `_bt_<family>(ds, params)
-> DataFrame[entry_time, gross_pips, stop_pips]` in `families.py`, register it in
`_FAMILIES` + `FAMILY_PARAM_KEYS`, and add it to `GROUNDED_FAMILIES` only if it
has a real structural mechanism.

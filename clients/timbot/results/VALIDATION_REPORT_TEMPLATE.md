# TIMBOT — Demo Validation Report

- **Client:** TIMBOT
- **Instrument / timeframe:** ____
- **Demo window:** ____ → ____
- **Demo account / broker:** ____
- **Prepared by:** Demarcus Hill
- **Date:** ____

## 1. Summary verdict

> One paragraph: did the demo track the backtest? Ready for the client's next
> step, or does it need tuning?

## 2. Backtest vs demo

| Metric          | Backtest | Demo | Delta |
|-----------------|----------|------|-------|
| Closed trades   |          |      |       |
| Win rate %      |          |      |       |
| Profit factor   |          |      |       |
| Net return %    |          |      |       |
| Max drawdown %  |          |      |       |
| Avg R / trade   |          |      |       |

## 3. Divergence analysis

Where demo differed from backtest and why (slippage, intrabar fills, missed
alerts, broker rejects):

-

## 4. Execution integrity checks

| Check                                          | Pass/Fail | Notes |
|------------------------------------------------|-----------|-------|
| Entries only on 4H bias flip + in session      |           |       |
| 50% partial executed at ~2R                    |           |       |
| Stop moved to break-even after partial         |           |       |
| Runner exited at HTF 78.6% target (or BE)      |           |       |
| No duplicate orders / wrong sizing             |           |       |
| Every entry traceable to a documented signal   |           |       |

## 5. Acceptance criteria

| Criterion                                   | Met? |
|----------------------------------------------|------|
| Demo win rate within ~10% of backtest        |      |
| Demo profit factor >= 1                      |      |
| No execution bugs                            |      |
| All entries traceable                        |      |

## 6. Recommendation

-

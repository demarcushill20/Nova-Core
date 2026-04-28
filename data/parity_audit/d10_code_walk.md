# D10 Code-Walk: Same-Bar TRAILING_STOP Edge

## Files reviewed
- Pine: `configs/pinescript/irb_v5_stag.pine:717-757` (S_LONG exit management)
- Python: `novatrade/backtest/engine.py:849-1028` (`_manage_position` stop-check + ratchet)

## Pine semantics (lines 725-733)
```pine
float ema_stop_long = trail_ema
cur_stop := math.max(nz(cur_stop, ema_stop_long), ema_stop_long)
strategy.exit("Long Exit", "Long", stop = cur_stop)
```
- At bar close: ratchet `cur_stop` to `max(prev, ema_stop_long)`.
- `strategy.exit` registers a stop order at the post-ratchet `cur_stop`.
- The order fires INTRA-BAR against this bar's high/low. Fill price = post-ratchet
  `cur_stop`, OR `bar.open` if the bar gaps through (Pine `strategy.exit` gap-fill
  semantics: if the trigger price is already passed at the open, the order
  executes at the open).

## Python semantics (current, after Phases 1-5)
1. (D1 toggle on) Optional pre-ratchet branch at `_manage_position:879-880` —
   `_ratchet_trail_only(...)` ratchets `pos.current_stop` BEFORE the stop-loss
   check.
2. Stop-loss check at `_manage_position:891-904` — `bar.low <= pos.current_stop`
   (long) or `bar.high >= pos.current_stop` (short). Exit at `pos.current_stop`
   OR `bar.open` if D12 toggle is on AND the bar gaps through
   (`bar.open < pos.current_stop` for longs, `bar.open > pos.current_stop` for
   shorts).
3. (Other exit logic — peak_fav tracking 906-912, STAG 918-927, partial-exit
   929-943, time_stop 945-952, breakeven 955-971, trail-delay 973-975.)
4. EMA-trail / ATR-trail ratchet at `_manage_position:980-1027` — if `new_trail`
   tightens, update `pos.current_stop`. Then check
   `bar.low <= NEW pos.current_stop AND bar.low > old_stop` (long, line 993) or
   `bar.high >= NEW pos.current_stop AND bar.high < old_stop` (short, line 1002;
   ATR variants at 1017, 1026). Exit at `pos.current_stop` (no gap-fill
   semantic).

## Equivalence cases (long-side)
| Case | Condition | Python behavior | Pine behavior | Verdict |
|---|---|---|---|---|
| A | `bar.low <= old_stop` | Step 2 exits at `old_stop` (pre-ratchet `pos.current_stop`) | Exits at post-ratchet `cur_stop` (>= `old_stop`) | DIVERGENT — covered by D1 |
| B | `old_stop < bar.low <= new_stop` | Step 4 exits at `new_stop` (line 993 strict-gate `bar.low > old_stop` is satisfied) | Exits at post-ratchet `cur_stop` (= `new_stop`) | EQUIVALENT |
| C | `bar.low > new_stop` | No exit | No exit | EQUIVALENT |

(Short-side mirror: `bar.high` vs stop, `<` vs `>` flipped at lines 1002/1026 —
same equivalence structure.)

## D10 Verdict
**Tier-3 verified-equivalent** for Cases B and C. D10's strict
`bar.low > old_stop` gate (line 993; mirror at 1002, 1017, 1026) excludes only
the `bar.low == old_stop` case. That case is handled at Case A by the prior
stop-check at line 892 (which uses `<=` and includes equality), so no exits are
silently dropped. Once D1 fix is applied, Case A becomes equivalent to Pine too.

## Open question (flagged for D12 follow-up)
The trailing-stop fire branches at `_manage_position:994, 1003, 1018, 1027`
exit unconditionally at `pos.current_stop` — they do NOT have D12-style gap-fill
semantics. The D12 toggle (`d12_gap_fill_at_open`, lines 894 / 901) only patches
the INITIAL stop-loss check at lines 891-904.

If Pine's `strategy.exit` gap-fills at `bar.open` when the bar gaps through the
post-ratchet `cur_stop`, the trailing-fire branch should too. This is
technically a sub-case of D12 (gap-through + trailing-stop), but the current
toggle name is scoped to a single call-site.

Recommendation for Phase 10 audit findings:
- Either extend the existing `d12_gap_fill_at_open` toggle to also cover the
  trailing branches (one toggle name, two call-sites: initial-stop and
  trailing-fire).
- Or add a new toggle `d12b_trail_gap_fill_at_open` for the trailing-stop
  variant so the two semantics can be measured independently.

For Phase 6 audit: **flag in audit doc, no Phase 6 code change.**

## Status
- documented (Tier-3 verified-equivalent under D1 + D12 assumptions)
- Open: D12 trailing-stop sub-case (deferred to Phase 10 / fix plan)

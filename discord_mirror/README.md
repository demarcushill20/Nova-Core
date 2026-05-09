# Discord Signal Mirror

Listens to a single Discord channel via a throwaway selfbot account, parses forex signals with Claude Sonnet 4.6, and mirrors trades on an isolated MetaApi demo account at 1% risk per signal. Fully separate from the NovaTrade vault_native engine.

See `~/nova-core/PLANS/discord-signal-mirror-v1.md` for the full plan.

## Run

```bash
cp .env.example .env       # fill DISCORD_USER_TOKEN, ANTHROPIC_API_KEY, METAAPI_*
cp config.yaml.example config.yaml  # set discord.channel_id
.venv/bin/python -m discord_mirror.main
```

Mode controlled by `EXECUTION_MODE` in `.env` (`paper` | `live`).

## Architecture

```
Discord channel message
  → SignalListener.on_message               (discord.py-self)
  → Storage.log_raw_message                 → raw_messages table
  → SignalParser.parse                      (Sonnet 4.6 + prompt caching)
  → Storage.log_parsed_signal               → signals table
  → Executor.execute (paper | metaapi)
    → allocate_positions                    (1% risk, multi-TP split)
    → log_paper_fill / log_metaapi_fill     → trades table
  → StatusHandler.handle (on TP/SL events)
    → SignalStateMachine                    (TP1→BE, close on full TP/SL)
    → modify_position_sl / close_position   (live mode only)
```

## Live escalation gate

Before flipping `EXECUTION_MODE=live`, the operator must manually review:

1. Run `python scripts/parity_report.py 7` and confirm:
   - `raw_messages` > 0
   - `open_signals` matches eyeballed count of trade calls in the channel for the last 7 days
   - `trades / open_signals` matches the average TP count per signal (typically 5–8)
2. Inspect 10 random signals end-to-end:

   ```sql
   SELECT s.id, s.symbol, s.direction, s.sl, s.tps_json, s.state,
          (SELECT COUNT(*) FROM trades t WHERE t.signal_id = s.id) AS n_trades
   FROM signals s ORDER BY RANDOM() LIMIT 10;
   ```

   For each, verify the parser's structured output matches the raw message and trade count == TP count.
3. Confirm SL→BE fired on at least one TP1_HIT event by inspecting the `trades.state` column for `BE` rows.
4. Only then: set `EXECUTION_MODE=live` in `.env` and restart.

Paper mode for ≥1 week, then operator approval, then live demo.

## Tests

```bash
.venv/bin/pytest tests/                     # unit tests (fast)
ANTHROPIC_API_KEY=sk-... .venv/bin/pytest   # also runs live parser tests
.venv/bin/python scripts/smoke_metaapi.py   # MetaApi connectivity smoke (needs .env)
```

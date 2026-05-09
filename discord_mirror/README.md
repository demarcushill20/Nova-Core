# Discord Signal Mirror

Listens to a single Discord channel via a throwaway selfbot account, parses forex signals with Claude Haiku 4.5, and mirrors trades on an isolated MetaApi demo account at 1% risk per signal. Fully separate from the NovaTrade vault_native engine.

See `~/nova-core/PLANS/discord-signal-mirror-v1.md` for the full plan.

## Run

```bash
cp .env.example .env
cp config.yaml.example config.yaml
.venv/bin/python -m discord_mirror.main
```

Mode controlled by `EXECUTION_MODE` in `.env` (`paper` | `live`).

# Discord Signal Mirror Bot Implementation Plan

> **For agentic workers:** use the `implementation-team` skill to execute this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Vault tracker note: `10-plans/plan-discord-signal-mirror-v1.md`.

**Goal:** Build an isolated bot that listens to a single Discord channel via a throwaway selfbot account, parses forex signals with Claude Haiku 4.5, and mirrors trades on a fresh MetaApi demo account at fixed 1% account risk per signal.

**Architecture:** Three-stage async pipeline: (1) `selfcord.py` listener writes every raw message to SQLite, (2) Haiku 4.5 parser extracts structured signals + status updates with prompt caching, (3) MetaApi executor opens N positions whose combined risk equals 1% of balance, sized off SL distance, with SL→BE after TP1 hits. Fully isolated under `~/nova-core/discord_mirror/` — no shared state, no imports, no credential overlap with NovaTrade vault_native.

**Tech Stack:** Python 3.11, selfcord.py, anthropic SDK (Haiku 4.5 + prompt caching), metaapi-cloud-sdk, aiosqlite, pydantic v2, pytest + pytest-asyncio.

---

## Phases

- [ ] **Phase 0: Scaffold & MetaApi demo** — directory tree, deps, env templates, provisioned MetaApi demo
- [ ] **Phase 1: Discord listener + raw message log** — selfcord listener writing every channel message to SQLite
- [ ] **Phase 2: LLM parser** — Haiku 4.5 parses raw messages → structured Signal/StatusUpdate models
- [ ] **Phase 3: Paper executor + sizing** — 1% risk math, multi-TP splitting, simulated fills logged to SQLite
- [ ] **Phase 4: MetaApi paper-mode + state** — real demo account fills, signal state machine, SL→BE on TP1
- [ ] **Phase 5: Parity log + review gate** — daily report, manual operator gate before any live escalation

---

## File Structure

```
~/nova-core/discord_mirror/
├── .env.example
├── .gitignore
├── README.md
├── config.yaml.example
├── requirements.txt
├── pyproject.toml
├── scripts/
│   ├── smoke_metaapi.py
│   └── parity_report.py
├── src/discord_mirror/
│   ├── __init__.py
│   ├── config.py            # YAML + env loader (pydantic)
│   ├── storage.py           # aiosqlite repo: raw_messages, signals, trades
│   ├── listener.py          # selfcord.Client subclass, on_message → storage
│   ├── parser.py            # Haiku 4.5 prompt + JSON parse
│   ├── models.py            # ParsedSignal, StatusUpdate
│   ├── sizing.py            # 1% risk → multi-TP lot allocation + symbol registry
│   ├── state.py             # signal state machine (OPEN, TP1_HIT, CLOSED)
│   ├── executor_paper.py    # paper mode: logs simulated fills
│   ├── executor_metaapi.py  # MetaApi adapter + executor
│   ├── status_handler.py    # TP/SL update → SL→BE / close
│   ├── parity.py            # daily parity report
│   └── main.py              # entrypoint, asyncio wiring
└── tests/
    ├── conftest.py
    ├── test_storage.py
    ├── test_config.py
    ├── test_models.py
    ├── test_parser.py
    ├── test_sizing.py
    ├── test_state.py
    ├── test_executor_paper.py
    ├── test_status_handler.py
    └── fixtures/
        ├── config.yaml
        └── sample_messages.json
```

One responsibility per file. Listener never parses, parser never executes, executor never reads Discord. State changes flow through `state.py`. Storage is the only async-IO layer.

---

## Task 0.1: Create directory structure and .gitignore

**Files:** Create `discord_mirror/.gitignore`, `discord_mirror/README.md`

- [ ] **Step 1: Create directory tree**

```bash
mkdir -p ~/nova-core/discord_mirror/{src/discord_mirror,tests/fixtures,data,logs,scripts}
touch ~/nova-core/discord_mirror/src/discord_mirror/__init__.py
touch ~/nova-core/discord_mirror/tests/__init__.py
```

- [ ] **Step 2: Write .gitignore**

```
.env
*.db
*.db-journal
data/
logs/
__pycache__/
*.pyc
.pytest_cache/
.venv/
*.egg-info/
```

- [ ] **Step 3: Write README.md**

```markdown
# Discord Signal Mirror

Listens to a single Discord channel via a throwaway selfbot account, parses forex signals with Claude Haiku 4.5, and mirrors trades on an isolated MetaApi demo account at 1% risk per signal. Fully separate from the NovaTrade vault_native engine.

See `~/nova-core/PLANS/discord-signal-mirror-v1.md` for the full plan.

## Run

\`\`\`bash
cp .env.example .env
cp config.yaml.example config.yaml
.venv/bin/python -m discord_mirror.main
\`\`\`

Mode controlled by `EXECUTION_MODE` in `.env` (`paper` | `live`).
```

- [ ] **Step 4: Commit**

```bash
cd ~/nova-core
git add discord_mirror/.gitignore discord_mirror/README.md \
        discord_mirror/src/discord_mirror/__init__.py discord_mirror/tests/__init__.py
git commit -m "chore(discord_mirror): scaffold project skeleton"
```

---

## Task 0.2: Python dependencies + venv

**Files:** Create `discord_mirror/requirements.txt`, `discord_mirror/pyproject.toml`

- [ ] **Step 1: Write requirements.txt**

```
selfcord.py==0.3.2
anthropic>=0.40.0
metaapi-cloud-sdk>=27.0.0
aiosqlite>=0.20.0
pydantic>=2.5
pydantic-settings>=2.1
pyyaml>=6.0
python-dotenv>=1.0
pytest>=8.0
pytest-asyncio>=0.23
pytest-mock>=3.12
```

- [ ] **Step 2: Write pyproject.toml**

```toml
[project]
name = "discord-mirror"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.setuptools.packages.find]
where = ["src"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

- [ ] **Step 3: Create venv and install**

```bash
cd ~/nova-core/discord_mirror
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -e .
.venv/bin/python -c "import selfcord, anthropic, metaapi_cloud_sdk, aiosqlite, pydantic, yaml; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
cd ~/nova-core
git add discord_mirror/requirements.txt discord_mirror/pyproject.toml
git commit -m "chore(discord_mirror): add deps + pyproject"
```

---

## Task 0.3: .env.example and config.yaml.example

**Files:** Create `discord_mirror/.env.example`, `discord_mirror/config.yaml.example`

- [ ] **Step 1: Write .env.example**

```
# Discord throwaway-account user token (selfbot). NEVER commit the real .env.
DISCORD_USER_TOKEN=

# Anthropic API key (Claude Haiku 4.5)
ANTHROPIC_API_KEY=

# MetaApi cloud demo (FRESH account — must NOT be vault_native account)
METAAPI_TOKEN=
METAAPI_ACCOUNT_ID=
METAAPI_REGION=new-york

# Mode: paper | live
EXECUTION_MODE=paper
```

- [ ] **Step 2: Write config.yaml.example**

```yaml
discord:
  channel_id: 0   # numeric channel id of the signal channel

risk:
  account_risk_pct: 0.01   # 1% per signal
  min_lot: 0.01
  lot_step: 0.01

symbol_map:
  GOLD: XAUUSD
  SILVER: XAGUSD
  US30: US30
  NAS100: NAS100
  EURUSD: EURUSD
  GBPUSD: GBPUSD

state:
  move_sl_to_be_after: TP1

storage:
  db_path: data/signals.db

logging:
  level: INFO
  path: logs/discord_mirror.log
```

- [ ] **Step 3: Commit**

```bash
cd ~/nova-core
git add discord_mirror/.env.example discord_mirror/config.yaml.example
git commit -m "chore(discord_mirror): add env + config templates"
```

---

## Task 0.4: Provision MetaApi demo account (operator-manual)

**Files:** none (operator action)

This task must complete before Phase 4. Implementer cannot do it autonomously — it requires operator interaction with the MetaApi dashboard.

- [ ] **Step 1: Sign in to https://app.metaapi.cloud/**

- [ ] **Step 2: Create a fresh MT5 demo account** at any broker that exposes XAUUSD (e.g., MetaQuotes-Demo). Account size 10,000 USD, leverage 1:100. Save broker login/password/server.

- [ ] **Step 3: Add the account to MetaApi** — "Add account" → MT5 → paste login/password/server → region `new-york`. Wait for state `DEPLOYED`.

- [ ] **Step 4: Generate a NEW token** scoped to ONLY this account ID. Do NOT reuse the vault_native token.

- [ ] **Step 5: Populate .env**

```bash
cp ~/nova-core/discord_mirror/.env.example ~/nova-core/discord_mirror/.env
# Edit .env: METAAPI_TOKEN, METAAPI_ACCOUNT_ID, METAAPI_REGION, ANTHROPIC_API_KEY, DISCORD_USER_TOKEN
```

- [ ] **Step 6: Smoke-test connectivity**

```bash
cd ~/nova-core/discord_mirror
.venv/bin/python -c "
import asyncio, os
from dotenv import load_dotenv
load_dotenv()
from metaapi_cloud_sdk import MetaApi

async def main():
    api = MetaApi(os.environ['METAAPI_TOKEN'])
    acct = await api.metatrader_account_api.get_account(os.environ['METAAPI_ACCOUNT_ID'])
    info = await acct.get_account_information()
    print('state:', acct.state, 'balance:', info['balance'])

asyncio.run(main())
"
```

Expected: `state: DEPLOYED balance: 10000.0` (or close).

---

## Task 1.1: SQLite storage — schema + repo (TDD)

**Files:** Create `src/discord_mirror/storage.py`, `tests/test_storage.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_storage.py
from datetime import datetime, timezone
import pytest
from discord_mirror.storage import Storage

@pytest.fixture
async def store(tmp_path):
    s = Storage(tmp_path / "test.db")
    await s.init()
    yield s
    await s.close()

async def test_log_raw_message_round_trips(store):
    msg_id = await store.log_raw_message(
        discord_message_id="111", channel_id="222", author="J",
        content="BUY GOLD\nSL 4535\nTP1 4546",
        ts=datetime(2026, 4, 30, 12, 15, tzinfo=timezone.utc),
    )
    assert msg_id > 0
    rows = await store.list_recent_raw_messages(limit=10)
    assert len(rows) == 1
    assert rows[0]["content"].startswith("BUY GOLD")
```

- [ ] **Step 2: Run (fails)**

```bash
cd ~/nova-core/discord_mirror && .venv/bin/pytest tests/test_storage.py -v
```

- [ ] **Step 3: Implement minimal storage**

```python
# src/discord_mirror/storage.py
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_message_id TEXT NOT NULL UNIQUE,
    channel_id TEXT NOT NULL,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_raw_messages_ts ON raw_messages(ts);
"""

class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    async def log_raw_message(self, *, discord_message_id, channel_id, author, content, ts: datetime) -> int:
        cur = await self._db.execute(
            "INSERT INTO raw_messages (discord_message_id, channel_id, author, content, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (discord_message_id, channel_id, author, content, ts.isoformat()),
        )
        await self._db.commit()
        return cur.lastrowid

    async def list_recent_raw_messages(self, limit: int = 50) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM raw_messages ORDER BY ts DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]
```

- [ ] **Step 4: Run (passes)**

```bash
.venv/bin/pytest tests/test_storage.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/discord_mirror/storage.py tests/test_storage.py
git commit -m "feat(discord_mirror): SQLite storage with raw_messages table"
```

---

## Task 1.2: Config loader (TDD)

**Files:** Create `src/discord_mirror/config.py`, `tests/fixtures/config.yaml`, `tests/test_config.py`

- [ ] **Step 1: Fixture**

```yaml
# tests/fixtures/config.yaml
discord:
  channel_id: 999888777
risk:
  account_risk_pct: 0.01
  min_lot: 0.01
  lot_step: 0.01
symbol_map:
  GOLD: XAUUSD
  US30: US30
state:
  move_sl_to_be_after: TP1
storage:
  db_path: data/signals.db
logging:
  level: INFO
  path: logs/discord_mirror.log
```

- [ ] **Step 2: Failing test**

```python
# tests/test_config.py
from pathlib import Path
from discord_mirror.config import load_config

def test_loads_config_and_maps_symbol():
    cfg = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    assert cfg.discord.channel_id == 999888777
    assert cfg.risk.account_risk_pct == 0.01
    assert cfg.symbol_map["GOLD"] == "XAUUSD"
    assert cfg.state.move_sl_to_be_after == "TP1"
```

- [ ] **Step 3: Run (fails)**

```bash
.venv/bin/pytest tests/test_config.py -v
```

- [ ] **Step 4: Implement**

```python
# src/discord_mirror/config.py
from __future__ import annotations
from pathlib import Path
import yaml
from pydantic import BaseModel, Field

class DiscordCfg(BaseModel):
    channel_id: int

class RiskCfg(BaseModel):
    account_risk_pct: float = Field(gt=0, lt=0.1)
    min_lot: float = 0.01
    lot_step: float = 0.01

class StateCfg(BaseModel):
    move_sl_to_be_after: str = "TP1"

class StorageCfg(BaseModel):
    db_path: str = "data/signals.db"

class LoggingCfg(BaseModel):
    level: str = "INFO"
    path: str = "logs/discord_mirror.log"

class Config(BaseModel):
    discord: DiscordCfg
    risk: RiskCfg
    symbol_map: dict[str, str]
    state: StateCfg
    storage: StorageCfg
    logging: LoggingCfg

def load_config(path: str | Path) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f)
    return Config(**data)
```

- [ ] **Step 5: Run + commit**

```bash
.venv/bin/pytest tests/test_config.py -v
git add src/discord_mirror/config.py tests/test_config.py tests/fixtures/config.yaml
git commit -m "feat(discord_mirror): pydantic config loader"
```

---

## Task 1.3: Selfcord listener — minimum viable raw-message capture

**Files:** Create `src/discord_mirror/listener.py`, `src/discord_mirror/main.py`

No isolated unit test — value is in integration. Smoke test in Step 4 is verification.

- [ ] **Step 1: Implement listener**

```python
# src/discord_mirror/listener.py
from __future__ import annotations
import logging
from datetime import timezone
import selfcord
from .storage import Storage

log = logging.getLogger(__name__)

class SignalListener(selfcord.Client):
    def __init__(self, *, channel_id: int, storage: Storage, **kwargs):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.storage = storage

    async def on_ready(self):
        log.info("Listener ready as %s — channel %s", self.user, self.channel_id)

    async def on_message(self, message: selfcord.Message):
        if message.channel.id != self.channel_id:
            return
        ts = message.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            await self.storage.log_raw_message(
                discord_message_id=str(message.id),
                channel_id=str(message.channel.id),
                author=str(message.author),
                content=message.content,
                ts=ts,
            )
            log.info("Logged %s from %s (%d chars)", message.id, message.author, len(message.content))
        except Exception:
            log.exception("Failed to log message %s", message.id)
```

- [ ] **Step 2: Implement minimal entrypoint**

```python
# src/discord_mirror/main.py
from __future__ import annotations
import asyncio, logging, os
from pathlib import Path
from dotenv import load_dotenv
from .config import load_config
from .storage import Storage
from .listener import SignalListener

async def amain():
    load_dotenv()
    cfg = load_config(Path("config.yaml"))
    logging.basicConfig(level=cfg.logging.level,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    storage = Storage(cfg.storage.db_path)
    await storage.init()
    client = SignalListener(channel_id=cfg.discord.channel_id, storage=storage)
    try:
        await client.start(os.environ["DISCORD_USER_TOKEN"])
    finally:
        await storage.close()

def main():
    asyncio.run(amain())

if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Operator config**

```bash
cd ~/nova-core/discord_mirror
cp config.yaml.example config.yaml
# operator: edit config.yaml — set discord.channel_id
# operator: edit .env — set DISCORD_USER_TOKEN
mkdir -p data logs
```

- [ ] **Step 4: Smoke-test 2 minutes**

```bash
.venv/bin/python -m discord_mirror.main &
PID=$!; sleep 120; kill $PID
.venv/bin/python -c "
import asyncio
from discord_mirror.config import load_config
from discord_mirror.storage import Storage
async def m():
    cfg = load_config('config.yaml')
    s = Storage(cfg.storage.db_path); await s.init()
    rows = await s.list_recent_raw_messages(5)
    print(f'{len(rows)} rows')
    for r in rows: print(r['ts'], r['author'], r['content'][:60])
    await s.close()
asyncio.run(m())
"
```

Expected: listener reaches `on_ready`, no tracebacks. 0 rows acceptable on a quiet channel.

- [ ] **Step 5: Commit**

```bash
git add src/discord_mirror/listener.py src/discord_mirror/main.py
git commit -m "feat(discord_mirror): selfcord listener writing raw messages to SQLite"
```

---

## Task 2.1: Pydantic signal models (TDD)

**Files:** Create `src/discord_mirror/models.py`, `tests/test_models.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_models.py
import pytest
from pydantic import ValidationError
from discord_mirror.models import ParsedSignal, SignalAction, Direction, StatusUpdate

def test_open_signal_round_trips():
    s = ParsedSignal(action=SignalAction.OPEN, direction=Direction.BUY,
                     symbol="GOLD", sl=4535.0, tps=[4546.0, 4550.0], confidence=0.95)
    assert s.action == SignalAction.OPEN
    assert s.tps == [4546.0, 4550.0]

def test_open_requires_tps():
    with pytest.raises(ValidationError):
        ParsedSignal(action=SignalAction.OPEN, direction=Direction.BUY,
                     symbol="GOLD", sl=4535.0, tps=[])

def test_open_requires_sl():
    with pytest.raises(ValidationError):
        ParsedSignal(action=SignalAction.OPEN, direction=Direction.BUY,
                     symbol="GOLD", sl=None, tps=[4546.0])

def test_status_update_tp_hit():
    u = StatusUpdate(kind="TP_HIT", tp_index=1, raw_text="TP1 SMACKED!!!")
    assert u.tp_index == 1

def test_tp_hit_requires_index():
    with pytest.raises(ValidationError):
        StatusUpdate(kind="TP_HIT", tp_index=None, raw_text="?")
```

- [ ] **Step 2: Run (fails)**

```bash
.venv/bin/pytest tests/test_models.py -v
```

- [ ] **Step 3: Implement**

```python
# src/discord_mirror/models.py
from __future__ import annotations
from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class SignalAction(str, Enum):
    OPEN = "OPEN"
    NONE = "NONE"

class ParsedSignal(BaseModel):
    action: SignalAction
    direction: Optional[Direction] = None
    symbol: Optional[str] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    tps: list[float] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.5)
    notes: str = ""

    @model_validator(mode="after")
    def _open_required(self):
        if self.action == SignalAction.OPEN:
            if self.direction is None or self.symbol is None or self.sl is None or not self.tps:
                raise ValueError("OPEN signal requires direction, symbol, sl, and at least one tp")
        return self

class StatusUpdate(BaseModel):
    kind: Literal["TP_HIT", "ALL_TPS_HIT", "SL_HIT", "CLOSED", "NONE"]
    tp_index: Optional[int] = None
    raw_text: str = ""

    @model_validator(mode="after")
    def _tp_hit_needs_index(self):
        if self.kind == "TP_HIT" and self.tp_index is None:
            raise ValueError("TP_HIT requires tp_index")
        return self
```

- [ ] **Step 4: Run + commit**

```bash
.venv/bin/pytest tests/test_models.py -v
git add src/discord_mirror/models.py tests/test_models.py
git commit -m "feat(discord_mirror): pydantic signal + status models"
```

---

## Task 2.2: LLM parser with Haiku 4.5 + prompt caching

**Files:** Create `src/discord_mirror/parser.py`, `tests/fixtures/sample_messages.json`, `tests/test_parser.py`

- [ ] **Step 1: Fixtures**

```json
[
  {"id": "msg1", "content": "BUY GOLD\nSL 4535\nTP1 4546, TP2 4550, TP3 4555, TP4 4560, TP5 4565, TP6 4570, TP7 4580, TP9 4600", "expected_kind": "open"},
  {"id": "msg2", "content": "@everyone BUY GOLD\nSL 4606\nTP1 4622, TP2 4625, TP3 4630, TP4 4635, TP5 4640, TP6 4650", "expected_kind": "open"},
  {"id": "msg3", "content": "TP1 SMACKED!!! @everyone", "expected_kind": "tp_hit", "expected_tp_index": 1},
  {"id": "msg4", "content": "ALL TPs SMASHED!!! @everyone", "expected_kind": "all_tps_hit"},
  {"id": "msg5", "content": "GM team!", "expected_kind": "none"}
]
```

- [ ] **Step 2: Failing test**

```python
# tests/test_parser.py
import json, os, pytest
from pathlib import Path
from discord_mirror.parser import SignalParser
from discord_mirror.models import SignalAction, Direction

pytestmark = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                                 reason="needs ANTHROPIC_API_KEY")

@pytest.fixture
def fixtures():
    return json.loads((Path(__file__).parent / "fixtures" / "sample_messages.json").read_text())

@pytest.fixture
def parser():
    return SignalParser(api_key=os.environ["ANTHROPIC_API_KEY"])

async def test_parses_open_signal_msg1(parser, fixtures):
    msg = next(f for f in fixtures if f["id"] == "msg1")
    result = await parser.parse(msg["content"])
    assert result.signal is not None
    assert result.signal.action == SignalAction.OPEN
    assert result.signal.direction == Direction.BUY
    assert result.signal.symbol == "GOLD"
    assert result.signal.sl == 4535.0
    assert len(result.signal.tps) == 8

async def test_parses_tp_hit(parser, fixtures):
    msg = next(f for f in fixtures if f["id"] == "msg3")
    result = await parser.parse(msg["content"])
    assert result.status is not None
    assert result.status.kind == "TP_HIT"
    assert result.status.tp_index == 1

async def test_parses_all_tps(parser, fixtures):
    msg = next(f for f in fixtures if f["id"] == "msg4")
    result = await parser.parse(msg["content"])
    assert result.status is not None
    assert result.status.kind == "ALL_TPS_HIT"

async def test_parses_chitchat_as_none(parser, fixtures):
    msg = next(f for f in fixtures if f["id"] == "msg5")
    result = await parser.parse(msg["content"])
    assert result.signal is None
    assert result.status is None
```

- [ ] **Step 3: Run (fails)**

```bash
.venv/bin/pytest tests/test_parser.py -v
```

- [ ] **Step 4: Implement**

```python
# src/discord_mirror/parser.py
from __future__ import annotations
import json
from dataclasses import dataclass
from anthropic import AsyncAnthropic
from .models import ParsedSignal, StatusUpdate

SYSTEM_PROMPT = """You parse forex/commodity trade signal messages from a Discord channel.

Output STRICT JSON with this schema:
{
  "signal": {
    "action": "OPEN" | "NONE",
    "direction": "BUY" | "SELL" | null,
    "symbol": string | null,
    "entry": number | null,
    "sl": number | null,
    "tps": [number, ...],
    "confidence": number,
    "notes": string
  } | null,
  "status": {
    "kind": "TP_HIT" | "ALL_TPS_HIT" | "SL_HIT" | "CLOSED" | "NONE",
    "tp_index": number | null,
    "raw_text": string
  } | null
}

Rules:
1. If the message opens a new trade (BUY/SELL X with SL and TPs), set "signal" with action=OPEN.
2. If the message reports a TP/SL outcome ("TP1 SMACKED", "ALL TPs SMASHED", "stopped out", "closed"), set "status".
3. If both apply, set both. If neither, set both to null.
4. TPs may be labelled non-contiguously (TP1..TP7, TP9 with TP8 missing) — list them in order, all of them.
5. Symbol normalization: keep what was written. Executor maps to broker symbols.
6. Numbers: parse as floats; strip commas.
7. NEVER invent values not in the message. If SL is missing on an OPEN, set action=NONE and explain in notes.
8. Output only the JSON object — no prose, no markdown fences."""

@dataclass
class ParseResult:
    signal: ParsedSignal | None
    status: StatusUpdate | None

class SignalParser:
    def __init__(self, *, api_key: str, model: str = "claude-haiku-4-5-20251001"):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def parse(self, content: str) -> ParseResult:
        resp = await self.client.messages.create(
            model=self.model, max_tokens=512,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json").strip()
        data = json.loads(text)
        sig_data = data.get("signal")
        signal = ParsedSignal(**sig_data) if sig_data and sig_data.get("action") != "NONE" else None
        st_data = data.get("status")
        status = StatusUpdate(**st_data) if st_data and st_data.get("kind") != "NONE" else None
        return ParseResult(signal=signal, status=status)
```

- [ ] **Step 5: Run + commit**

```bash
ANTHROPIC_API_KEY=$(grep ANTHROPIC_API_KEY .env | cut -d= -f2) \
  .venv/bin/pytest tests/test_parser.py -v
git add src/discord_mirror/parser.py tests/test_parser.py tests/fixtures/sample_messages.json
git commit -m "feat(discord_mirror): Haiku 4.5 LLM parser with cached system prompt"
```

---

## Task 2.3: Persist parsed signals + wire parser into listener

**Files:** Modify `src/discord_mirror/storage.py`, `listener.py`, `main.py`, `tests/test_storage.py`

- [ ] **Step 1: Failing storage test**

```python
# tests/test_storage.py — append
from datetime import datetime, timezone

async def test_log_parsed_signal(store):
    raw_id = await store.log_raw_message(
        discord_message_id="333", channel_id="222", author="J",
        content="BUY GOLD\nSL 4535\nTP1 4546", ts=datetime.now(timezone.utc),
    )
    sid = await store.log_parsed_signal(raw_id, {
        "action": "OPEN", "direction": "BUY", "symbol": "GOLD",
        "sl": 4535.0, "tps": [4546.0], "confidence": 0.9,
    })
    assert sid > 0
    rows = await store.list_open_signals()
    assert len(rows) == 1 and rows[0]["symbol"] == "GOLD"
```

- [ ] **Step 2: Run (fails)**

```bash
.venv/bin/pytest tests/test_storage.py -v
```

- [ ] **Step 3: Extend storage**

Replace SCHEMA in `storage.py` with:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_message_id TEXT NOT NULL UNIQUE,
    channel_id TEXT NOT NULL,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    ts TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_raw_messages_ts ON raw_messages(ts);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_message_id INTEGER NOT NULL REFERENCES raw_messages(id),
    action TEXT NOT NULL,
    direction TEXT,
    symbol TEXT,
    entry REAL,
    sl REAL,
    tps_json TEXT NOT NULL,
    confidence REAL,
    state TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state);
"""
```

Add to `Storage` class:

```python
    async def log_parsed_signal(self, raw_message_id: int, signal: dict) -> int:
        import json as _json
        cur = await self._db.execute(
            "INSERT INTO signals (raw_message_id, action, direction, symbol, entry, sl, tps_json, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (raw_message_id, signal["action"], signal.get("direction"), signal.get("symbol"),
             signal.get("entry"), signal.get("sl"),
             _json.dumps(signal.get("tps", [])), signal.get("confidence", 0.5)),
        )
        await self._db.commit()
        return cur.lastrowid

    async def list_open_signals(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM signals WHERE state = 'OPEN' ORDER BY id DESC")
        return [dict(r) for r in await cur.fetchall()]

    async def update_signal_state(self, signal_id: int, state: str):
        await self._db.execute("UPDATE signals SET state = ? WHERE id = ?", (state, signal_id))
        await self._db.commit()
```

- [ ] **Step 4: Run storage test (passes)**

```bash
.venv/bin/pytest tests/test_storage.py -v
```

- [ ] **Step 5: Replace listener with parser-aware version**

```python
# src/discord_mirror/listener.py
from __future__ import annotations
import logging
from datetime import timezone
import selfcord
from .storage import Storage
from .parser import SignalParser

log = logging.getLogger(__name__)

class SignalListener(selfcord.Client):
    def __init__(self, *, channel_id: int, storage: Storage,
                 parser: SignalParser | None = None, **kwargs):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.storage = storage
        self.parser = parser

    async def on_ready(self):
        log.info("Listener ready as %s — channel %s", self.user, self.channel_id)

    async def on_message(self, message: selfcord.Message):
        if message.channel.id != self.channel_id:
            return
        ts = message.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            raw_id = await self.storage.log_raw_message(
                discord_message_id=str(message.id), channel_id=str(message.channel.id),
                author=str(message.author), content=message.content, ts=ts,
            )
        except Exception:
            log.exception("Failed to log raw message %s", message.id)
            return
        if not self.parser:
            return
        try:
            result = await self.parser.parse(message.content)
        except Exception:
            log.exception("Parser failed on message %s", message.id)
            return
        if result.signal is not None:
            sid = await self.storage.log_parsed_signal(raw_id, result.signal.model_dump())
            log.info("Signal id=%s %s %s SL=%s TPs=%s",
                     sid, result.signal.direction, result.signal.symbol,
                     result.signal.sl, result.signal.tps)
        if result.status is not None:
            log.info("Status: %s tp_index=%s", result.status.kind, result.status.tp_index)
```

- [ ] **Step 6: Update main.py**

```python
# src/discord_mirror/main.py — replace amain body
async def amain():
    load_dotenv()
    cfg = load_config(Path("config.yaml"))
    logging.basicConfig(level=cfg.logging.level,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    storage = Storage(cfg.storage.db_path)
    await storage.init()
    from .parser import SignalParser
    parser = SignalParser(api_key=os.environ["ANTHROPIC_API_KEY"])
    client = SignalListener(channel_id=cfg.discord.channel_id, storage=storage, parser=parser)
    try:
        await client.start(os.environ["DISCORD_USER_TOKEN"])
    finally:
        await storage.close()
```

- [ ] **Step 7: Smoke-test + commit**

```bash
.venv/bin/python -m discord_mirror.main &
PID=$!; sleep 120; kill $PID
git add src/discord_mirror/storage.py src/discord_mirror/listener.py \
        src/discord_mirror/main.py tests/test_storage.py
git commit -m "feat(discord_mirror): parse signals via Haiku, persist to signals table"
```

---

## Task 3.1: Sizing math — 1% risk → multi-TP allocation (TDD)

**Files:** Create `src/discord_mirror/sizing.py`, `tests/test_sizing.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_sizing.py
import pytest
from discord_mirror.sizing import allocate_positions, PositionPlan, SymbolSpec

GOLD = SymbolSpec(symbol="XAUUSD", contract_size=100.0,
                  tick_size=0.01, tick_value=1.0, min_lot=0.01, lot_step=0.01)

def test_single_tp_uses_full_risk():
    # SL distance 20 → 2000 ticks → risk_per_lot $2000. 1% of $10k = $100 → lot 0.05.
    plans = allocate_positions(balance=10_000.0, account_risk_pct=0.01, direction="BUY",
                                entry=4500.0, sl=4480.0, tps=[4520.0], symbol=GOLD)
    assert len(plans) == 1
    assert plans[0].lot == pytest.approx(0.05, abs=0.001)
    assert plans[0].tp == 4520.0
    assert plans[0].sl == 4480.0

def test_three_tps_split():
    plans = allocate_positions(balance=10_000.0, account_risk_pct=0.01, direction="BUY",
                                entry=4500.0, sl=4480.0,
                                tps=[4520.0, 4530.0, 4540.0], symbol=GOLD)
    assert len(plans) == 3
    # Per-leg ideal 0.05/3 ≈ 0.0167 → rounds DOWN to 0.01.
    total = sum(p.lot for p in plans)
    assert total <= pytest.approx(0.05, abs=0.001)
    assert all(p.lot == 0.01 for p in plans)

def test_below_min_lot_returns_empty():
    plans = allocate_positions(balance=1_000.0, account_risk_pct=0.01, direction="BUY",
                                entry=4500.0, sl=4480.0, tps=[4520.0, 4530.0], symbol=GOLD)
    assert plans == []

def test_sell_direction():
    plans = allocate_positions(balance=10_000.0, account_risk_pct=0.01, direction="SELL",
                                entry=4500.0, sl=4520.0, tps=[4480.0], symbol=GOLD)
    assert len(plans) == 1
    assert plans[0].lot == pytest.approx(0.05, abs=0.001)
    assert plans[0].direction == "SELL"

def test_zero_sl_distance_returns_empty():
    plans = allocate_positions(balance=10_000.0, account_risk_pct=0.01, direction="BUY",
                                entry=4500.0, sl=4500.0, tps=[4520.0], symbol=GOLD)
    assert plans == []
```

- [ ] **Step 2: Run (fails)**

```bash
.venv/bin/pytest tests/test_sizing.py -v
```

- [ ] **Step 3: Implement**

```python
# src/discord_mirror/sizing.py
from __future__ import annotations
from dataclasses import dataclass
from math import floor

@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    contract_size: float
    tick_size: float
    tick_value: float
    min_lot: float = 0.01
    lot_step: float = 0.01

@dataclass(frozen=True)
class PositionPlan:
    direction: str
    symbol: str
    entry: float
    sl: float
    tp: float
    lot: float

def _round_lot(lot: float, step: float, min_lot: float) -> float:
    if lot < min_lot:
        return 0.0
    return floor(lot / step) * step

def allocate_positions(*, balance, account_risk_pct, direction,
                       entry, sl, tps, symbol: SymbolSpec) -> list[PositionPlan]:
    if not tps:
        return []
    risk_usd = balance * account_risk_pct
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return []
    ticks = sl_distance / symbol.tick_size
    risk_per_lot = ticks * symbol.tick_value
    if risk_per_lot <= 0:
        return []
    total_lot = risk_usd / risk_per_lot
    n = len(tps)
    per_lot = total_lot / n
    rounded = _round_lot(per_lot, symbol.lot_step, symbol.min_lot)
    if rounded <= 0:
        return []
    return [PositionPlan(direction=direction, symbol=symbol.symbol,
                         entry=entry, sl=sl, tp=tp, lot=rounded)
            for tp in tps]
```

- [ ] **Step 4: Run + commit**

```bash
.venv/bin/pytest tests/test_sizing.py -v
git add src/discord_mirror/sizing.py tests/test_sizing.py
git commit -m "feat(discord_mirror): 1% risk multi-TP position allocator"
```

---

## Task 3.2: Symbol spec registry

**Files:** Modify `src/discord_mirror/sizing.py`, `tests/test_sizing.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_sizing.py — append
from discord_mirror.sizing import get_symbol_spec

def test_registry_has_xauusd():
    spec = get_symbol_spec("XAUUSD")
    assert spec.contract_size == 100.0
    assert spec.tick_size == 0.01

def test_registry_has_eurusd():
    spec = get_symbol_spec("EURUSD")
    assert spec.contract_size == 100_000.0
    assert spec.tick_value == 10.0

def test_registry_unknown_raises():
    with pytest.raises(KeyError):
        get_symbol_spec("FAKEPAIR")
```

- [ ] **Step 2: Run (fails)**

```bash
.venv/bin/pytest tests/test_sizing.py -v
```

- [ ] **Step 3: Add registry**

Append to `sizing.py`:

```python
_REGISTRY: dict[str, SymbolSpec] = {
    "XAUUSD": SymbolSpec("XAUUSD", contract_size=100.0, tick_size=0.01, tick_value=1.0),
    "XAGUSD": SymbolSpec("XAGUSD", contract_size=5000.0, tick_size=0.001, tick_value=5.0),
    "EURUSD": SymbolSpec("EURUSD", contract_size=100_000.0, tick_size=0.0001, tick_value=10.0),
    "GBPUSD": SymbolSpec("GBPUSD", contract_size=100_000.0, tick_size=0.0001, tick_value=10.0),
    "US30":   SymbolSpec("US30", contract_size=1.0, tick_size=1.0, tick_value=1.0),
    "NAS100": SymbolSpec("NAS100", contract_size=1.0, tick_size=0.1, tick_value=0.1),
}

def get_symbol_spec(symbol: str) -> SymbolSpec:
    return _REGISTRY[symbol]
```

- [ ] **Step 4: Run + commit**

```bash
.venv/bin/pytest tests/test_sizing.py -v
git add src/discord_mirror/sizing.py tests/test_sizing.py
git commit -m "feat(discord_mirror): symbol spec registry"
```

---

## Task 3.3: Paper executor + trades table

**Files:** Create `src/discord_mirror/executor_paper.py`, `tests/test_executor_paper.py`. Modify `storage.py`, `tests/test_storage.py`.

- [ ] **Step 1: Failing storage test**

```python
# tests/test_storage.py — append
async def test_log_paper_fill(store):
    raw_id = await store.log_raw_message(discord_message_id="r", channel_id="c",
                                          author="a", content="x", ts=datetime.now(timezone.utc))
    sid = await store.log_parsed_signal(raw_id, {
        "action": "OPEN", "direction": "BUY", "symbol": "GOLD",
        "sl": 4535.0, "tps": [4546.0], "confidence": 0.9,
    })
    fill_id = await store.log_paper_fill(signal_id=sid, direction="BUY", symbol="XAUUSD",
                                          entry=4540.0, sl=4535.0, tp=4546.0, lot=0.05)
    assert fill_id > 0
```

- [ ] **Step 2: Run (fails)**

```bash
.venv/bin/pytest tests/test_storage.py -v
```

- [ ] **Step 3: Extend SCHEMA + add method**

Append to SCHEMA:

```python
SCHEMA = SCHEMA + """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    mode TEXT NOT NULL,
    broker_order_id TEXT,
    direction TEXT NOT NULL,
    symbol TEXT NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    lot REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'OPEN',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades(signal_id);
"""
```

Add method:

```python
    async def log_paper_fill(self, *, signal_id, direction, symbol,
                              entry, sl, tp, lot) -> int:
        cur = await self._db.execute(
            "INSERT INTO trades (signal_id, mode, direction, symbol, entry, sl, tp, lot) "
            "VALUES (?, 'paper', ?, ?, ?, ?, ?, ?)",
            (signal_id, direction, symbol, entry, sl, tp, lot),
        )
        await self._db.commit()
        return cur.lastrowid
```

- [ ] **Step 4: Implement paper executor**

```python
# src/discord_mirror/executor_paper.py
from __future__ import annotations
import logging
from .config import Config
from .models import ParsedSignal
from .sizing import allocate_positions, get_symbol_spec
from .storage import Storage

log = logging.getLogger(__name__)

class PaperExecutor:
    def __init__(self, *, storage: Storage, cfg: Config, balance: float = 10_000.0):
        self.storage = storage
        self.cfg = cfg
        self.balance = balance

    def _broker_symbol(self, signal_symbol: str) -> str:
        return self.cfg.symbol_map.get(signal_symbol, signal_symbol)

    async def execute(self, signal_id: int, signal: ParsedSignal) -> int:
        broker_symbol = self._broker_symbol(signal.symbol)
        try:
            spec = get_symbol_spec(broker_symbol)
        except KeyError:
            log.warning("No symbol spec for %s; skipping", broker_symbol)
            return 0
        # Stand-in entry: SL + 10% of SL→TP1 distance.
        if signal.direction.value == "BUY":
            entry = signal.sl + (signal.tps[0] - signal.sl) * 0.1
        else:
            entry = signal.sl - (signal.sl - signal.tps[0]) * 0.1
        plans = allocate_positions(balance=self.balance,
                                    account_risk_pct=self.cfg.risk.account_risk_pct,
                                    direction=signal.direction.value, entry=entry,
                                    sl=signal.sl, tps=signal.tps, symbol=spec)
        if not plans:
            log.warning("Signal %s allocated 0 positions", signal_id)
            return 0
        for p in plans:
            await self.storage.log_paper_fill(signal_id=signal_id, direction=p.direction,
                                               symbol=p.symbol, entry=p.entry,
                                               sl=p.sl, tp=p.tp, lot=p.lot)
            log.info("PAPER FILL sig=%s %s %s lot=%s entry=%s sl=%s tp=%s",
                     signal_id, p.direction, p.symbol, p.lot, p.entry, p.sl, p.tp)
        return len(plans)
```

- [ ] **Step 5: Test executor**

```python
# tests/test_executor_paper.py
from datetime import datetime, timezone
import pytest
from discord_mirror.executor_paper import PaperExecutor
from discord_mirror.models import ParsedSignal, SignalAction, Direction
from discord_mirror.config import Config, DiscordCfg, RiskCfg, StateCfg, StorageCfg, LoggingCfg
from discord_mirror.storage import Storage

@pytest.fixture
def cfg():
    return Config(
        discord=DiscordCfg(channel_id=1),
        risk=RiskCfg(account_risk_pct=0.01),
        symbol_map={"GOLD": "XAUUSD"},
        state=StateCfg(),
        storage=StorageCfg(db_path=":memory:"),
        logging=LoggingCfg(),
    )

async def test_paper_executor_logs_fills(tmp_path, cfg):
    s = Storage(tmp_path / "t.db"); await s.init()
    raw_id = await s.log_raw_message(discord_message_id="r", channel_id="c",
                                      author="a", content="x", ts=datetime.now(timezone.utc))
    sid = await s.log_parsed_signal(raw_id, {
        "action": "OPEN", "direction": "BUY", "symbol": "GOLD",
        "sl": 4480.0, "tps": [4520.0, 4530.0], "confidence": 0.9,
    })
    sig = ParsedSignal(action=SignalAction.OPEN, direction=Direction.BUY,
                       symbol="GOLD", sl=4480.0, tps=[4520.0, 4530.0])
    ex = PaperExecutor(storage=s, cfg=cfg, balance=10_000.0)
    n = await ex.execute(sid, sig)
    assert n == 2
    cur = await s._db.execute("SELECT COUNT(*) FROM trades WHERE signal_id = ?", (sid,))
    assert (await cur.fetchone())[0] == 2
    await s.close()
```

- [ ] **Step 6: Run + commit**

```bash
.venv/bin/pytest tests/test_executor_paper.py tests/test_storage.py -v
git add src/discord_mirror/executor_paper.py tests/test_executor_paper.py \
        src/discord_mirror/storage.py tests/test_storage.py
git commit -m "feat(discord_mirror): paper executor with multi-TP fills"
```

---

## Task 3.4: Wire paper executor into listener pipeline

**Files:** Modify `listener.py`, `main.py`

- [ ] **Step 1: Add executor to listener**

Replace `SignalListener` `__init__` and `on_message`:

```python
# src/discord_mirror/listener.py — replace class body
class SignalListener(selfcord.Client):
    def __init__(self, *, channel_id, storage, parser=None, executor=None, **kwargs):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.storage = storage
        self.parser = parser
        self.executor = executor

    async def on_ready(self):
        log.info("Listener ready as %s — channel %s", self.user, self.channel_id)

    async def on_message(self, message: selfcord.Message):
        if message.channel.id != self.channel_id:
            return
        ts = message.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            raw_id = await self.storage.log_raw_message(
                discord_message_id=str(message.id), channel_id=str(message.channel.id),
                author=str(message.author), content=message.content, ts=ts,
            )
        except Exception:
            log.exception("Failed to log raw message %s", message.id); return
        if not self.parser:
            return
        try:
            result = await self.parser.parse(message.content)
        except Exception:
            log.exception("Parser failed on %s", message.id); return
        if result.signal is not None:
            sid = await self.storage.log_parsed_signal(raw_id, result.signal.model_dump())
            log.info("Signal id=%s %s %s", sid, result.signal.direction, result.signal.symbol)
            if self.executor and result.signal.action.value == "OPEN":
                await self.executor.execute(sid, result.signal)
        if result.status is not None:
            log.info("Status: %s tp_index=%s", result.status.kind, result.status.tp_index)
```

- [ ] **Step 2: Update main.py**

```python
# src/discord_mirror/main.py — replace amain
async def amain():
    load_dotenv()
    cfg = load_config(Path("config.yaml"))
    logging.basicConfig(level=cfg.logging.level,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    storage = Storage(cfg.storage.db_path)
    await storage.init()
    from .parser import SignalParser
    from .executor_paper import PaperExecutor
    parser = SignalParser(api_key=os.environ["ANTHROPIC_API_KEY"])
    executor = PaperExecutor(storage=storage, cfg=cfg)
    client = SignalListener(channel_id=cfg.discord.channel_id, storage=storage,
                             parser=parser, executor=executor)
    try:
        await client.start(os.environ["DISCORD_USER_TOKEN"])
    finally:
        await storage.close()
```

- [ ] **Step 3: Smoke-test + commit**

```bash
.venv/bin/python -m discord_mirror.main &
PID=$!; sleep 120; kill $PID
git add src/discord_mirror/listener.py src/discord_mirror/main.py
git commit -m "feat(discord_mirror): wire paper executor into listener pipeline"
```

---

## Task 4.1: Signal state machine (TDD)

**Files:** Create `src/discord_mirror/state.py`, `tests/test_state.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_state.py
from discord_mirror.state import SignalStateMachine, SignalState

def test_initial_state_is_open():
    sm = SignalStateMachine(tp_count=3)
    assert sm.state == SignalState.OPEN
    assert sm.tps_hit == 0

def test_tp1_hit_advances_and_signals_be():
    sm = SignalStateMachine(tp_count=3)
    actions = sm.on_tp_hit(1)
    assert sm.state == SignalState.TP1_HIT
    kinds = [a.kind for a in actions]
    assert "MOVE_SL_TO_BE" in kinds
    assert "CLOSE_TP1" in kinds

def test_all_tp_indices_close():
    sm = SignalStateMachine(tp_count=3)
    sm.on_tp_hit(1); sm.on_tp_hit(2); sm.on_tp_hit(3)
    assert sm.state == SignalState.CLOSED

def test_all_tps_smashed_message_closes():
    sm = SignalStateMachine(tp_count=3)
    actions = sm.on_all_tps_hit()
    assert sm.state == SignalState.CLOSED
    assert "CLOSE_ALL" in [a.kind for a in actions]

def test_sl_hit_closes():
    sm = SignalStateMachine(tp_count=3)
    actions = sm.on_sl_hit()
    assert sm.state == SignalState.CLOSED
    assert "RECORD_SL" in [a.kind for a in actions]

def test_duplicate_tp_hit_idempotent():
    sm = SignalStateMachine(tp_count=3)
    sm.on_tp_hit(1)
    actions = sm.on_tp_hit(1)
    assert actions == []
```

- [ ] **Step 2: Run (fails)**

```bash
.venv/bin/pytest tests/test_state.py -v
```

- [ ] **Step 3: Implement**

```python
# src/discord_mirror/state.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

class SignalState(str, Enum):
    OPEN = "OPEN"
    TP1_HIT = "TP1_HIT"
    TPN_HIT = "TPN_HIT"
    CLOSED = "CLOSED"

@dataclass
class StateAction:
    kind: str
    tp_index: int | None = None

@dataclass
class SignalStateMachine:
    tp_count: int
    state: SignalState = SignalState.OPEN
    tps_hit: int = 0
    _hit_indices: set[int] = field(default_factory=set)

    def on_tp_hit(self, tp_index: int) -> list[StateAction]:
        if tp_index in self._hit_indices or self.state == SignalState.CLOSED:
            return []
        self._hit_indices.add(tp_index)
        self.tps_hit = len(self._hit_indices)
        actions = [StateAction(kind="CLOSE_TP1" if tp_index == 1 else "CLOSE_TPN",
                                tp_index=tp_index)]
        if tp_index == 1 and self.state == SignalState.OPEN:
            actions.append(StateAction(kind="MOVE_SL_TO_BE"))
            self.state = SignalState.TP1_HIT
        elif self.state == SignalState.OPEN:
            self.state = SignalState.TPN_HIT
        if self.tps_hit >= self.tp_count:
            self.state = SignalState.CLOSED
        return actions

    def on_all_tps_hit(self) -> list[StateAction]:
        if self.state == SignalState.CLOSED:
            return []
        self.state = SignalState.CLOSED
        return [StateAction(kind="CLOSE_ALL")]

    def on_sl_hit(self) -> list[StateAction]:
        if self.state == SignalState.CLOSED:
            return []
        self.state = SignalState.CLOSED
        return [StateAction(kind="RECORD_SL")]
```

- [ ] **Step 4: Run + commit**

```bash
.venv/bin/pytest tests/test_state.py -v
git add src/discord_mirror/state.py tests/test_state.py
git commit -m "feat(discord_mirror): signal state machine with TP1→BE rule"
```

---

## Task 4.2: MetaApi adapter — connect, balance, ticks, orders

**Files:** Create `src/discord_mirror/executor_metaapi.py`, `scripts/smoke_metaapi.py`

- [ ] **Step 1: Implement adapter**

```python
# src/discord_mirror/executor_metaapi.py
from __future__ import annotations
import asyncio, logging
from metaapi_cloud_sdk import MetaApi

log = logging.getLogger(__name__)

class MetaApiAdapter:
    def __init__(self, *, token: str, account_id: str, region: str = "new-york"):
        self._api = MetaApi(token)
        self._account_id = account_id
        self._region = region
        self._account = None
        self._connection = None

    async def connect(self):
        self._account = await self._api.metatrader_account_api.get_account(self._account_id)
        if self._account.state != "DEPLOYED":
            await self._account.deploy()
        await self._account.wait_connected()
        self._connection = self._account.get_streaming_connection()
        await self._connection.connect()
        await self._connection.wait_synchronized()
        log.info("MetaApi connected; state=%s", self._account.state)

    async def balance(self) -> float:
        info = await self._account.get_account_information()
        return float(info["balance"])

    async def get_price(self, symbol: str) -> tuple[float, float]:
        terminal = self._connection.terminal_state
        price = terminal.price(symbol)
        if price is None:
            await self._connection.subscribe_to_market_data(symbol)
            await asyncio.sleep(2)
            price = terminal.price(symbol)
        if price is None:
            raise RuntimeError(f"No price for {symbol}")
        return float(price["bid"]), float(price["ask"])

    async def place_market(self, *, symbol, direction, lot, sl, tp, comment="") -> str:
        method = (self._connection.create_market_buy_order if direction == "BUY"
                  else self._connection.create_market_sell_order)
        result = await method(symbol, lot, sl, tp, {"comment": comment[:31]})
        return str(result.get("orderId") or result.get("positionId") or result)

    async def modify_position_sl(self, position_id: str, new_sl: float):
        await self._connection.modify_position(position_id, stop_loss=new_sl)

    async def close_position(self, position_id: str):
        await self._connection.close_position(position_id)

    async def close(self):
        if self._connection:
            await self._connection.close()
```

- [ ] **Step 2: Smoke-test script**

```python
# scripts/smoke_metaapi.py
import asyncio, os
from dotenv import load_dotenv
from discord_mirror.executor_metaapi import MetaApiAdapter

async def main():
    load_dotenv()
    a = MetaApiAdapter(token=os.environ["METAAPI_TOKEN"],
                       account_id=os.environ["METAAPI_ACCOUNT_ID"],
                       region=os.environ.get("METAAPI_REGION", "new-york"))
    await a.connect()
    print("balance:", await a.balance())
    bid, ask = await a.get_price("XAUUSD")
    print("XAUUSD bid:", bid, "ask:", ask)
    await a.close()

asyncio.run(main())
```

- [ ] **Step 3: Run + commit**

```bash
.venv/bin/python scripts/smoke_metaapi.py
git add src/discord_mirror/executor_metaapi.py scripts/smoke_metaapi.py
git commit -m "feat(discord_mirror): MetaApi adapter (connect, balance, prices, orders)"
```

---

## Task 4.3: MetaApi executor + mode switch

**Files:** Modify `executor_metaapi.py`, `storage.py`, `main.py`

- [ ] **Step 1: Append `MetaApiExecutor` to `executor_metaapi.py`**

```python
from .config import Config
from .models import ParsedSignal
from .sizing import allocate_positions, get_symbol_spec
from .storage import Storage

class MetaApiExecutor:
    def __init__(self, *, adapter: MetaApiAdapter, storage: Storage, cfg: Config):
        self.adapter = adapter
        self.storage = storage
        self.cfg = cfg

    def _broker_symbol(self, signal_symbol: str) -> str:
        return self.cfg.symbol_map.get(signal_symbol, signal_symbol)

    async def execute(self, signal_id: int, signal: ParsedSignal) -> int:
        broker_symbol = self._broker_symbol(signal.symbol)
        try:
            spec = get_symbol_spec(broker_symbol)
        except KeyError:
            log.warning("No symbol spec for %s; skipping", broker_symbol)
            return 0
        balance = await self.adapter.balance()
        bid, ask = await self.adapter.get_price(broker_symbol)
        entry = ask if signal.direction.value == "BUY" else bid
        plans = allocate_positions(balance=balance,
                                    account_risk_pct=self.cfg.risk.account_risk_pct,
                                    direction=signal.direction.value, entry=entry,
                                    sl=signal.sl, tps=signal.tps, symbol=spec)
        if not plans:
            log.warning("Signal %s allocated 0 positions", signal_id)
            return 0
        for p in plans:
            try:
                order_id = await self.adapter.place_market(
                    symbol=p.symbol, direction=p.direction, lot=p.lot,
                    sl=p.sl, tp=p.tp, comment=f"sig{signal_id}",
                )
            except Exception:
                log.exception("place_market failed sig=%s tp=%s", signal_id, p.tp)
                continue
            await self.storage.log_metaapi_fill(
                signal_id=signal_id, broker_order_id=order_id,
                direction=p.direction, symbol=p.symbol, entry=entry,
                sl=p.sl, tp=p.tp, lot=p.lot,
            )
            log.info("METAAPI FILL sig=%s order=%s %s %s lot=%s sl=%s tp=%s",
                     signal_id, order_id, p.direction, p.symbol, p.lot, p.sl, p.tp)
        return len(plans)
```

- [ ] **Step 2: Add storage methods**

```python
    async def log_metaapi_fill(self, *, signal_id, broker_order_id, direction,
                                symbol, entry, sl, tp, lot) -> int:
        cur = await self._db.execute(
            "INSERT INTO trades (signal_id, mode, broker_order_id, direction, "
            "symbol, entry, sl, tp, lot) "
            "VALUES (?, 'metaapi', ?, ?, ?, ?, ?, ?, ?)",
            (signal_id, broker_order_id, direction, symbol, entry, sl, tp, lot),
        )
        await self._db.commit()
        return cur.lastrowid

    async def list_open_trades_for_signal(self, signal_id: int) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM trades WHERE signal_id = ? AND state = 'OPEN'", (signal_id,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def update_trade_state(self, trade_id: int, *, state: str, sl: float | None = None):
        if sl is not None:
            await self._db.execute("UPDATE trades SET state = ?, sl = ? WHERE id = ?",
                                    (state, sl, trade_id))
        else:
            await self._db.execute("UPDATE trades SET state = ? WHERE id = ?", (state, trade_id))
        await self._db.commit()
```

- [ ] **Step 3: Wire mode switch in main.py**

```python
# src/discord_mirror/main.py — replace amain
async def amain():
    load_dotenv()
    cfg = load_config(Path("config.yaml"))
    logging.basicConfig(level=cfg.logging.level,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    storage = Storage(cfg.storage.db_path)
    await storage.init()
    from .parser import SignalParser
    parser = SignalParser(api_key=os.environ["ANTHROPIC_API_KEY"])

    mode = os.environ.get("EXECUTION_MODE", "paper")
    adapter = None
    if mode == "live":
        from .executor_metaapi import MetaApiAdapter, MetaApiExecutor
        adapter = MetaApiAdapter(
            token=os.environ["METAAPI_TOKEN"],
            account_id=os.environ["METAAPI_ACCOUNT_ID"],
            region=os.environ.get("METAAPI_REGION", "new-york"),
        )
        await adapter.connect()
        executor = MetaApiExecutor(adapter=adapter, storage=storage, cfg=cfg)
    else:
        from .executor_paper import PaperExecutor
        executor = PaperExecutor(storage=storage, cfg=cfg)

    client = SignalListener(channel_id=cfg.discord.channel_id, storage=storage,
                             parser=parser, executor=executor)
    try:
        await client.start(os.environ["DISCORD_USER_TOKEN"])
    finally:
        if adapter:
            await adapter.close()
        await storage.close()
```

- [ ] **Step 4: Smoke-test live mode + commit**

```bash
EXECUTION_MODE=live .venv/bin/python -m discord_mirror.main &
PID=$!; sleep 60; kill $PID
git add src/discord_mirror/executor_metaapi.py src/discord_mirror/storage.py \
        src/discord_mirror/main.py
git commit -m "feat(discord_mirror): MetaApi executor with mode switch (paper|live)"
```

---

## Task 4.4: Status update handler — TP1→BE, close on SL/all-TPs

**Files:** Create `src/discord_mirror/status_handler.py`, `tests/test_status_handler.py`. Modify `listener.py`, `main.py`.

- [ ] **Step 1: Failing test**

```python
# tests/test_status_handler.py
import pytest
from unittest.mock import AsyncMock
from discord_mirror.status_handler import StatusHandler
from discord_mirror.models import StatusUpdate
from discord_mirror.state import SignalStateMachine

class FakeStorage:
    def __init__(self):
        self.trades = [
            {"id": 1, "broker_order_id": "o1", "direction": "BUY",
             "symbol": "XAUUSD", "entry": 4500.0, "sl": 4480.0, "tp": 4520.0, "state": "OPEN"},
            {"id": 2, "broker_order_id": "o2", "direction": "BUY",
             "symbol": "XAUUSD", "entry": 4500.0, "sl": 4480.0, "tp": 4530.0, "state": "OPEN"},
        ]
        self.updates = []
    async def list_open_trades_for_signal(self, sid):
        return [t for t in self.trades if t["state"] == "OPEN"]
    async def update_trade_state(self, tid, *, state, sl=None):
        self.updates.append((tid, state, sl))
        for t in self.trades:
            if t["id"] == tid:
                t["state"] = state
                if sl is not None:
                    t["sl"] = sl

async def test_tp1_hit_closes_lowest_and_moves_remaining_sl_to_be():
    storage = FakeStorage()
    adapter = AsyncMock()
    sm = SignalStateMachine(tp_count=2)
    h = StatusHandler(storage=storage, adapter=adapter)
    await h.handle(signal_id=99, sm=sm,
                    update=StatusUpdate(kind="TP_HIT", tp_index=1, raw_text="TP1 SMACKED"))
    adapter.close_position.assert_any_await("o1")
    adapter.modify_position_sl.assert_any_await("o2", 4500.0)

async def test_all_tps_closes_everything():
    storage = FakeStorage()
    adapter = AsyncMock()
    sm = SignalStateMachine(tp_count=2)
    h = StatusHandler(storage=storage, adapter=adapter)
    await h.handle(signal_id=99, sm=sm,
                    update=StatusUpdate(kind="ALL_TPS_HIT", raw_text="ALL TPs SMASHED"))
    adapter.close_position.assert_any_await("o1")
    adapter.close_position.assert_any_await("o2")

async def test_paper_mode_no_adapter_calls():
    storage = FakeStorage()
    sm = SignalStateMachine(tp_count=2)
    h = StatusHandler(storage=storage, adapter=None)
    await h.handle(signal_id=99, sm=sm,
                    update=StatusUpdate(kind="TP_HIT", tp_index=1, raw_text="?"))
    assert any(u[1] == "BE" for u in storage.updates)
```

- [ ] **Step 2: Run (fails)**

```bash
.venv/bin/pytest tests/test_status_handler.py -v
```

- [ ] **Step 3: Implement**

```python
# src/discord_mirror/status_handler.py
from __future__ import annotations
import logging
from .models import StatusUpdate
from .state import SignalStateMachine

log = logging.getLogger(__name__)

class StatusHandler:
    def __init__(self, *, storage, adapter):
        self.storage = storage
        self.adapter = adapter

    async def handle(self, *, signal_id: int, sm: SignalStateMachine, update: StatusUpdate):
        if update.kind == "TP_HIT":
            actions = sm.on_tp_hit(update.tp_index)
        elif update.kind == "ALL_TPS_HIT":
            actions = sm.on_all_tps_hit()
        elif update.kind == "SL_HIT":
            actions = sm.on_sl_hit()
        else:
            return
        open_trades = await self.storage.list_open_trades_for_signal(signal_id)
        for action in actions:
            if action.kind == "MOVE_SL_TO_BE":
                for t in open_trades:
                    if t["state"] != "OPEN":
                        continue
                    if self.adapter:
                        try:
                            await self.adapter.modify_position_sl(t["broker_order_id"], t["entry"])
                        except Exception:
                            log.exception("modify_position_sl failed for trade %s", t["id"])
                            continue
                    await self.storage.update_trade_state(t["id"], state="BE", sl=t["entry"])
            elif action.kind in ("CLOSE_TP1", "CLOSE_TPN"):
                if not open_trades:
                    continue
                is_buy = open_trades[0]["direction"] == "BUY"
                ordered = sorted(open_trades, key=lambda t: t["tp"], reverse=not is_buy)
                idx = (action.tp_index or 1) - 1
                target = ordered[idx] if idx < len(ordered) else ordered[-1]
                if self.adapter:
                    try:
                        await self.adapter.close_position(target["broker_order_id"])
                    except Exception:
                        log.exception("close_position failed for trade %s", target["id"])
                        continue
                await self.storage.update_trade_state(target["id"], state="CLOSED")
            elif action.kind == "CLOSE_ALL":
                for t in open_trades:
                    if self.adapter:
                        try:
                            await self.adapter.close_position(t["broker_order_id"])
                        except Exception:
                            log.exception("close_position failed for trade %s", t["id"])
                            continue
                    await self.storage.update_trade_state(t["id"], state="CLOSED")
            elif action.kind == "RECORD_SL":
                for t in open_trades:
                    await self.storage.update_trade_state(t["id"], state="CLOSED")
        log.info("Handled %s for signal %s; %d actions", update.kind, signal_id, len(actions))
```

- [ ] **Step 4: Wire into listener**

Replace `SignalListener` to add `status_handler` and per-signal state machines:

```python
# src/discord_mirror/listener.py — full replacement
from __future__ import annotations
import logging
from datetime import timezone
import selfcord
from .storage import Storage
from .parser import SignalParser
from .state import SignalStateMachine

log = logging.getLogger(__name__)

class SignalListener(selfcord.Client):
    def __init__(self, *, channel_id, storage, parser=None, executor=None,
                 status_handler=None, **kwargs):
        super().__init__(**kwargs)
        self.channel_id = channel_id
        self.storage = storage
        self.parser = parser
        self.executor = executor
        self.status_handler = status_handler
        self._state_machines: dict[int, SignalStateMachine] = {}
        self._latest_signal_id: int | None = None

    async def on_ready(self):
        log.info("Listener ready as %s — channel %s", self.user, self.channel_id)

    async def on_message(self, message: selfcord.Message):
        if message.channel.id != self.channel_id:
            return
        ts = message.created_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            raw_id = await self.storage.log_raw_message(
                discord_message_id=str(message.id), channel_id=str(message.channel.id),
                author=str(message.author), content=message.content, ts=ts,
            )
        except Exception:
            log.exception("Failed to log raw message %s", message.id); return
        if not self.parser:
            return
        try:
            result = await self.parser.parse(message.content)
        except Exception:
            log.exception("Parser failed on %s", message.id); return
        if result.signal is not None:
            sid = await self.storage.log_parsed_signal(raw_id, result.signal.model_dump())
            self._state_machines[sid] = SignalStateMachine(tp_count=len(result.signal.tps))
            self._latest_signal_id = sid
            log.info("Signal id=%s %s %s", sid, result.signal.direction, result.signal.symbol)
            if self.executor and result.signal.action.value == "OPEN":
                await self.executor.execute(sid, result.signal)
        if result.status is not None and self.status_handler and self._latest_signal_id is not None:
            sm = self._state_machines.get(self._latest_signal_id)
            if sm:
                await self.status_handler.handle(signal_id=self._latest_signal_id,
                                                  sm=sm, update=result.status)
```

- [ ] **Step 5: Wire into main.py**

```python
# In amain, after executor wiring (and before SignalListener construction):
    from .status_handler import StatusHandler
    status_handler = StatusHandler(storage=storage, adapter=adapter)

    client = SignalListener(channel_id=cfg.discord.channel_id, storage=storage,
                             parser=parser, executor=executor,
                             status_handler=status_handler)
```

- [ ] **Step 6: Run + commit**

```bash
.venv/bin/pytest tests/test_status_handler.py -v
git add src/discord_mirror/status_handler.py src/discord_mirror/listener.py \
        src/discord_mirror/main.py tests/test_status_handler.py
git commit -m "feat(discord_mirror): status handler — TP1→BE, close on TP/SL/ALL events"
```

---

## Task 5.1: Daily parity report

**Files:** Create `src/discord_mirror/parity.py`, `scripts/parity_report.py`

- [ ] **Step 1: Implement**

```python
# src/discord_mirror/parity.py
from __future__ import annotations
from .storage import Storage

async def daily_report(storage: Storage, since_iso: str) -> dict:
    db = storage._db
    cur = await db.execute("SELECT COUNT(*) FROM raw_messages WHERE ts >= ?", (since_iso,))
    raw = (await cur.fetchone())[0]
    cur = await db.execute(
        "SELECT COUNT(*) FROM signals WHERE created_at >= ? AND action = 'OPEN'", (since_iso,))
    open_signals = (await cur.fetchone())[0]
    cur = await db.execute(
        "SELECT COUNT(*), SUM(lot) FROM trades WHERE created_at >= ?", (since_iso,))
    row = await cur.fetchone()
    trades = row[0] or 0
    total_lot = row[1] or 0.0
    cur = await db.execute(
        "SELECT state, COUNT(*) FROM trades WHERE created_at >= ? GROUP BY state", (since_iso,))
    states = {r[0]: r[1] for r in await cur.fetchall()}
    cur = await db.execute(
        "SELECT mode, COUNT(*) FROM trades WHERE created_at >= ? GROUP BY mode", (since_iso,))
    modes = {r[0]: r[1] for r in await cur.fetchall()}
    return {"since": since_iso, "raw_messages": raw, "open_signals": open_signals,
            "trades": trades, "total_lot": round(total_lot, 4),
            "trade_states": states, "trade_modes": modes}
```

- [ ] **Step 2: CLI script**

```python
# scripts/parity_report.py
import asyncio, json, sys
from datetime import datetime, timezone, timedelta
from discord_mirror.storage import Storage
from discord_mirror.parity import daily_report

async def main(days: int = 1):
    s = Storage("data/signals.db"); await s.init()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    print(json.dumps(await daily_report(s, since), indent=2))
    await s.close()

if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1))
```

- [ ] **Step 3: Run + commit**

```bash
.venv/bin/python scripts/parity_report.py 7
git add src/discord_mirror/parity.py scripts/parity_report.py
git commit -m "feat(discord_mirror): daily parity report"
```

---

## Task 5.2: Operator review-gate runbook

**Files:** Modify `~/nova-core/discord_mirror/README.md`

- [ ] **Step 1: Append review-gate section**

```markdown
## Live escalation gate

Before flipping `EXECUTION_MODE=live`, operator must manually review:

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
3. Confirm SL→BE fired on at least one TP1_HIT event by inspecting `trades.state` for `BE` rows.
4. Only then: set `EXECUTION_MODE=live` in `.env` and restart.

Paper mode for ≥1 week, then operator approval, then live demo.
```

- [ ] **Step 2: Commit**

```bash
git add discord_mirror/README.md
git commit -m "docs(discord_mirror): live-escalation review gate runbook"
```

---

## Self-review

- **Spec coverage:** all 6 phases mapped to tasks. Sample messages from operator (msg1–msg5) used as parser fixtures. Sizing math implements the 1% rule + multi-TP split. SL→BE rule wired in state machine + status handler. MetaApi isolation enforced by separate `.env` and credential-scoping in Task 0.4.
- **Type consistency:** `ParsedSignal.action` = `SignalAction` enum; `direction` = `Direction` enum; `StatusUpdate.kind` = Literal. State machine = `SignalState` enum + `StateAction`. Both executors share `(signal_id: int, signal: ParsedSignal) -> int` signature. `PositionPlan` from `allocate_positions` consumed by both. Storage tables: `raw_messages`, `signals`, `trades` — consistent across all tasks.
- **No placeholders:** every code block is complete and runnable.
- **Risk note:** Task 4.3's MetaApi executor hits a real (demo) broker. Demo isolation is the entire point of Task 0.4 — verify `METAAPI_ACCOUNT_ID` is the fresh demo account, NOT vault_native, before running Step 4 of Task 4.3.
- **Discord ToS risk:** the throwaway account may be banned and/or kicked from the signal server. Acceptable per operator decision. Listener is read-only (no sends, no reactions, no typing).

---

## Execution Handoff

Plan complete. Vault tracker note: `10-plans/plan-discord-signal-mirror-v1.md` (status: backlog, progress: 0/6). Full task content: this file. Ready to execute via `implementation-team`.

Recommended next step: `/worktree` to isolate this work, then `implementation-team` picks up Phase 0.

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

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalAction(str, Enum):
    OPEN = "OPEN"
    NONE = "NONE"


class ParsedSignal(BaseModel):
    action: SignalAction
    direction: Direction | None = None
    symbol: str | None = None
    entry: float | None = None
    sl: float | None = None
    tps: list[float] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1, default=0.5)
    notes: str = ""

    @model_validator(mode="after")
    def _open_required_fields(self):
        if self.action == SignalAction.OPEN and (
            self.direction is None or self.symbol is None or self.sl is None or not self.tps
        ):
            raise ValueError("OPEN signal requires direction, symbol, sl, and at least one tp")
        return self


class StatusUpdate(BaseModel):
    kind: Literal["TP_HIT", "ALL_TPS_HIT", "SL_HIT", "CLOSED", "NONE"]
    tp_index: int | None = None
    raw_text: str = ""

    @model_validator(mode="after")
    def _tp_hit_needs_index(self):
        if self.kind == "TP_HIT" and self.tp_index is None:
            raise ValueError("TP_HIT requires tp_index")
        return self

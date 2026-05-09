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
        actions: list[StateAction] = [
            StateAction(
                kind="CLOSE_TP1" if tp_index == 1 else "CLOSE_TPN",
                tp_index=tp_index,
            )
        ]
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

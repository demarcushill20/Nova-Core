from discord_mirror.state import SignalState, SignalStateMachine


def test_initial_state_is_open():
    sm = SignalStateMachine(tp_count=3)
    assert sm.state == SignalState.OPEN
    assert sm.tps_hit == 0


def test_tp1_hit_advances_and_signals_be():
    sm = SignalStateMachine(tp_count=3)
    actions = sm.on_tp_hit(1)
    assert sm.state == SignalState.TP1_HIT
    assert sm.tps_hit == 1
    kinds = [a.kind for a in actions]
    assert "MOVE_SL_TO_BE" in kinds
    assert "CLOSE_TP1" in kinds


def test_all_tp_indices_close():
    sm = SignalStateMachine(tp_count=3)
    sm.on_tp_hit(1)
    sm.on_tp_hit(2)
    sm.on_tp_hit(3)
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


def test_tp2_hit_first_does_not_signal_be():
    # If TP2 hits before TP1 (unusual but possible), no SL→BE action.
    sm = SignalStateMachine(tp_count=3)
    actions = sm.on_tp_hit(2)
    kinds = [a.kind for a in actions]
    assert "MOVE_SL_TO_BE" not in kinds
    assert "CLOSE_TPN" in kinds

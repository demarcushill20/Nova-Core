from datetime import datetime, timedelta, timezone

from discord_mirror.coalescer import RawMsg, coalesce

T0 = datetime(2026, 5, 8, 2, 40, 0, tzinfo=timezone.utc)


def msg(rid: int, content: str, *, offset_s: int = 0, author: str = "J") -> RawMsg:
    return RawMsg(raw_id=rid, author=author, content=content, ts=T0 + timedelta(seconds=offset_s))


def test_three_message_signal_coalesces():
    out = list(
        coalesce(
            [
                msg(1, "@everyone BUY GOLD", offset_s=0),
                msg(2, "SL 4700", offset_s=6),
                msg(3, "TP1 4715, TP2 4720", offset_s=28),
            ]
        )
    )
    assert len(out) == 1
    g = out[0]
    assert g.is_group
    assert g.raw_ids == [1, 2, 3]
    assert "BUY GOLD" in g.combined_content
    assert "SL 4700" in g.combined_content
    assert "TP1 4715" in g.combined_content


def test_status_message_does_not_join_signal_buffer():
    out = list(
        coalesce(
            [
                msg(1, "@everyone BUY GOLD", offset_s=0),
                msg(2, "SL 4700", offset_s=5),
                msg(3, "TP1 SMACKED!!! @everyone", offset_s=30),  # status, must not append
                msg(4, "TP1 4715, TP2 4720", offset_s=45),  # signal buffer continuation
            ]
        )
    )
    # Expect: [BUY+SL group], [TP1 SMACKED standalone], [TP1 4715 standalone]
    assert len(out) == 3
    assert out[0].raw_ids == [1, 2]
    assert out[1].raw_ids == [3] and "SMACKED" in out[1].combined_content
    # The TP1 4715 message arrives after status was emitted; buffer was flushed,
    # so it's now a standalone (not part of a trigger).
    assert out[2].raw_ids == [4]


def test_followup_outside_window_flushes():
    out = list(
        coalesce(
            [
                msg(1, "@everyone BUY GOLD", offset_s=0),
                msg(2, "SL 4700", offset_s=70),  # outside 60s window
            ]
        )
    )
    assert len(out) == 2
    assert out[0].raw_ids == [1]
    assert out[1].raw_ids == [2]


def test_different_author_flushes_buffer():
    out = list(
        coalesce(
            [
                msg(1, "@everyone BUY GOLD", offset_s=0, author="J"),
                msg(2, "random chatter", offset_s=10, author="A"),
                msg(3, "SL 4700", offset_s=20, author="J"),
            ]
        )
    )
    # Buffer flushed when A's message arrives. SL alone is standalone (no trigger).
    assert len(out) == 3
    assert out[0].raw_ids == [1]
    assert out[1].raw_ids == [2]
    assert out[2].raw_ids == [3]


def test_new_trigger_flushes_previous():
    out = list(
        coalesce(
            [
                msg(1, "@everyone BUY GOLD", offset_s=0),
                msg(2, "@everyone SELL EURUSD", offset_s=20),  # new trigger
                msg(3, "SL 1.0800", offset_s=25),
            ]
        )
    )
    # First buffer (just msg 1) is flushed when msg 2 starts a new trigger
    assert len(out) == 2
    assert out[0].raw_ids == [1]
    assert out[1].raw_ids == [2, 3]


def test_chitchat_yields_standalone():
    out = list(
        coalesce(
            [
                msg(1, "GM team!", offset_s=0),
                msg(2, "Hold that thang", offset_s=10),
            ]
        )
    )
    assert [g.raw_ids for g in out] == [[1], [2]]


def test_case_sensitive_trigger_avoids_false_positives():
    # "Buying", "sells out", "buyer" should NOT be treated as triggers
    out = list(
        coalesce(
            [
                msg(1, "Buying again at 4578", offset_s=0),
                msg(2, "Time to close gold sells out", offset_s=10),
                msg(3, "the buyer is gone", offset_s=20),
            ]
        )
    )
    assert all(not g.is_group for g in out)
    assert len(out) == 3

from pathlib import Path

from discord_mirror.config import load_config


def test_loads_config_and_maps_symbol():
    cfg = load_config(Path(__file__).parent / "fixtures" / "config.yaml")
    assert cfg.discord.channel_id == 999888777
    assert cfg.risk.account_risk_pct == 0.01
    assert cfg.symbol_map["GOLD"] == "XAUUSD"
    assert cfg.state.move_sl_to_be_after == "TP1"

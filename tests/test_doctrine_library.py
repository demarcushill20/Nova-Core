"""Tests for novatrade.doctrine.library — curated doctrine templates."""

from __future__ import annotations

import pytest

from novatrade.doctrine.library import (
    _RECOMMENDED,
    _TEMPLATE_BUILDERS,
    create_doctrine_from_template,
    get_doctrine_template,
    list_doctrine_templates,
)
from novatrade.doctrine.schema import StrategyDoctrine

# ---------------------------------------------------------------------------
# Template listing
# ---------------------------------------------------------------------------


class TestListDoctrineTemplates:
    """Test the template listing API."""

    def test_returns_all_templates(self) -> None:
        templates = list_doctrine_templates()
        assert len(templates) == 6

    def test_each_template_has_required_keys(self) -> None:
        templates = list_doctrine_templates()
        for t in templates:
            assert "name" in t
            assert "description" in t
            assert "pairs" in t
            assert "timeframes" in t

    def test_known_templates_present(self) -> None:
        names = {t["name"] for t in list_doctrine_templates()}
        expected = {
            "irb_conservative",
            "irb_aggressive",
            "irb_scalper",
            "mean_reversion_bollinger",
            "breakout_donchian",
            "trend_ema_cross",
        }
        assert names == expected

    def test_descriptions_non_empty(self) -> None:
        templates = list_doctrine_templates()
        for t in templates:
            assert len(t["description"]) > 10, f"Template '{t['name']}' has empty description"

    def test_pairs_non_empty(self) -> None:
        templates = list_doctrine_templates()
        for t in templates:
            assert len(t["pairs"]) > 0, f"Template '{t['name']}' has no recommended pairs"

    def test_timeframes_non_empty(self) -> None:
        templates = list_doctrine_templates()
        for t in templates:
            assert len(t["timeframes"]) > 0, f"Template '{t['name']}' has no recommended timeframes"


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------


class TestGetDoctrineTemplate:
    """Test loading individual templates."""

    @pytest.mark.parametrize(
        "name",
        [
            "irb_conservative",
            "irb_aggressive",
            "irb_scalper",
            "mean_reversion_bollinger",
            "breakout_donchian",
            "trend_ema_cross",
        ],
    )
    def test_all_templates_load_successfully(self, name: str) -> None:
        doctrine = get_doctrine_template(name)
        assert isinstance(doctrine, StrategyDoctrine)
        assert doctrine.name  # non-empty name

    @pytest.mark.parametrize(
        "name",
        [
            "irb_conservative",
            "irb_aggressive",
            "irb_scalper",
            "mean_reversion_bollinger",
            "breakout_donchian",
            "trend_ema_cross",
        ],
    )
    def test_all_templates_have_mutable_params(self, name: str) -> None:
        doctrine = get_doctrine_template(name)
        assert len(doctrine.mutable_params) > 0

    @pytest.mark.parametrize(
        "name",
        [
            "irb_conservative",
            "irb_aggressive",
            "irb_scalper",
            "mean_reversion_bollinger",
            "breakout_donchian",
            "trend_ema_cross",
        ],
    )
    def test_all_templates_have_immutable_params(self, name: str) -> None:
        doctrine = get_doctrine_template(name)
        assert len(doctrine.immutable_params) > 0

    @pytest.mark.parametrize(
        "name",
        [
            "irb_conservative",
            "irb_aggressive",
            "irb_scalper",
            "mean_reversion_bollinger",
            "breakout_donchian",
            "trend_ema_cross",
        ],
    )
    def test_all_templates_have_valid_campaign_settings(self, name: str) -> None:
        doctrine = get_doctrine_template(name)
        assert doctrine.campaign.max_experiments > 0
        assert doctrine.campaign.stagnation_limit > 0
        assert doctrine.campaign.wall_clock_limit_hours > 0

    def test_unknown_template_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="Unknown doctrine template"):
            get_doctrine_template("nonexistent_strategy")

    def test_error_message_lists_available(self) -> None:
        with pytest.raises(KeyError) as exc_info:
            get_doctrine_template("bad_name")
        assert "irb_conservative" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Template-specific properties
# ---------------------------------------------------------------------------


class TestIRBConservative:
    """Verify IRB conservative template properties."""

    def test_setup_type(self) -> None:
        d = get_doctrine_template("irb_conservative")
        assert d.setup_type == "reversal"

    def test_strict_validation(self) -> None:
        d = get_doctrine_template("irb_conservative")
        assert d.validation.max_drawdown_pct <= 15.0
        assert d.validation.min_profit_factor >= 1.3

    def test_low_risk_per_trade(self) -> None:
        d = get_doctrine_template("irb_conservative")
        risk = d.immutable_params["risk_per_trade_pct"]
        assert isinstance(risk, (int, float))
        assert risk <= 1.0

    def test_mandatory_filters(self) -> None:
        d = get_doctrine_template("irb_conservative")
        assert "trend_alignment" in d.filters_mandatory
        assert "adx_strength" in d.filters_mandatory


class TestIRBAggressive:
    """Verify IRB aggressive template properties."""

    def test_wider_drawdown_limit(self) -> None:
        d = get_doctrine_template("irb_aggressive")
        conservative = get_doctrine_template("irb_conservative")
        assert d.validation.max_drawdown_pct > conservative.validation.max_drawdown_pct

    def test_more_experiments(self) -> None:
        d = get_doctrine_template("irb_aggressive")
        conservative = get_doctrine_template("irb_conservative")
        assert d.campaign.max_experiments >= conservative.campaign.max_experiments


class TestIRBScalper:
    """Verify IRB scalper template properties."""

    def test_shorter_timeframes(self) -> None:
        d = get_doctrine_template("irb_scalper")
        assert "M5" in d.data.timeframes or "M15" in d.data.timeframes

    def test_more_bars_required(self) -> None:
        d = get_doctrine_template("irb_scalper")
        assert d.data.min_bars >= 5000

    def test_session_filter_mandatory(self) -> None:
        d = get_doctrine_template("irb_scalper")
        assert "session_london_ny_overlap" in d.filters_mandatory


class TestMeanReversionBollinger:
    """Verify Bollinger mean reversion template."""

    def test_setup_type(self) -> None:
        d = get_doctrine_template("mean_reversion_bollinger")
        assert d.setup_type == "mean_reversion"

    def test_has_bollinger_params(self) -> None:
        d = get_doctrine_template("mean_reversion_bollinger")
        assert "bb_period" in d.mutable_params
        assert "bb_std_dev" in d.mutable_params

    def test_has_rsi_params(self) -> None:
        d = get_doctrine_template("mean_reversion_bollinger")
        assert "rsi_period" in d.mutable_params
        assert "rsi_oversold" in d.mutable_params
        assert "rsi_overbought" in d.mutable_params


class TestBreakoutDonchian:
    """Verify Donchian breakout template."""

    def test_setup_type(self) -> None:
        d = get_doctrine_template("breakout_donchian")
        assert d.setup_type == "breakout"

    def test_has_lookback_period(self) -> None:
        d = get_doctrine_template("breakout_donchian")
        assert "lookback_period" in d.mutable_params

    def test_has_volume_confirmation(self) -> None:
        d = get_doctrine_template("breakout_donchian")
        assert "volume_confirmation_mult" in d.mutable_params


class TestTrendEMACross:
    """Verify EMA cross trend template."""

    def test_setup_type(self) -> None:
        d = get_doctrine_template("trend_ema_cross")
        assert d.setup_type == "trend_following"

    def test_has_ema_params(self) -> None:
        d = get_doctrine_template("trend_ema_cross")
        assert "fast_ema" in d.mutable_params
        assert "slow_ema" in d.mutable_params

    def test_trend_alignment_mandatory(self) -> None:
        d = get_doctrine_template("trend_ema_cross")
        assert "trend_alignment" in d.filters_mandatory


# ---------------------------------------------------------------------------
# Template overrides
# ---------------------------------------------------------------------------


class TestCreateDoctrineFromTemplate:
    """Test creating doctrines with overrides."""

    def test_pair_override(self) -> None:
        d = create_doctrine_from_template("irb_conservative", pair="GBPUSD")
        assert d.data.pair == "GBPUSD"

    def test_timeframe_override(self) -> None:
        d = create_doctrine_from_template("irb_conservative", timeframe="M15")
        assert "M15" in d.data.timeframes

    def test_timeframe_derives_confirmation(self) -> None:
        d = create_doctrine_from_template("irb_conservative", timeframe="M15")
        assert d.data.timeframes == ["M15", "H1"]

    def test_timeframe_h4_derives_d1(self) -> None:
        d = create_doctrine_from_template("irb_conservative", timeframe="H4")
        assert d.data.timeframes == ["H4", "D1"]

    def test_name_override(self) -> None:
        d = create_doctrine_from_template("irb_conservative", name="My Custom IRB")
        assert d.name == "My Custom IRB"

    def test_description_override(self) -> None:
        d = create_doctrine_from_template("irb_conservative", description="Custom desc")
        assert d.description == "Custom desc"

    def test_pair_and_timeframe_together(self) -> None:
        d = create_doctrine_from_template("breakout_donchian", pair="USDJPY", timeframe="D1")
        assert d.data.pair == "USDJPY"
        assert d.data.timeframes == ["D1", "W1"]

    def test_unknown_template_raises(self) -> None:
        with pytest.raises(KeyError):
            create_doctrine_from_template("nonexistent")

    def test_override_preserves_other_fields(self) -> None:
        original = get_doctrine_template("irb_conservative")
        overridden = create_doctrine_from_template("irb_conservative", pair="GBPUSD")
        # Mutable params should be preserved
        assert overridden.mutable_params == original.mutable_params
        # Immutable params should be preserved
        assert overridden.immutable_params == original.immutable_params
        # Campaign settings preserved
        assert overridden.campaign == original.campaign

    def test_version_override(self) -> None:
        d = create_doctrine_from_template("irb_conservative", version="2.0.0")
        assert d.version == "2.0.0"

    def test_returns_valid_doctrine(self) -> None:
        """The returned doctrine passes Pydantic validation."""
        d = create_doctrine_from_template(
            "mean_reversion_bollinger",
            pair="AUDUSD",
            timeframe="H4",
        )
        # If validation failed, we would not get here
        assert isinstance(d, StrategyDoctrine)
        assert d.content_hash()  # non-empty hash = valid


# ---------------------------------------------------------------------------
# Content hash uniqueness
# ---------------------------------------------------------------------------


class TestTemplateUniqueness:
    """Verify that different templates produce different content hashes."""

    def test_all_templates_have_unique_hashes(self) -> None:
        hashes = set()
        for name in _TEMPLATE_BUILDERS:
            doctrine = get_doctrine_template(name)
            h = doctrine.content_hash()
            assert h not in hashes, f"Duplicate hash for template '{name}'"
            hashes.add(h)

    def test_override_changes_hash(self) -> None:
        original = get_doctrine_template("irb_conservative")
        modified = create_doctrine_from_template("irb_conservative", pair="GBPUSD")
        assert original.content_hash() != modified.content_hash()


# ---------------------------------------------------------------------------
# Recommendations data
# ---------------------------------------------------------------------------


class TestRecommendations:
    """Test that every template has recommendations metadata."""

    def test_all_builders_have_recommendations(self) -> None:
        for name in _TEMPLATE_BUILDERS:
            assert name in _RECOMMENDED, f"Template '{name}' missing from _RECOMMENDED"

    def test_recommendations_have_pairs(self) -> None:
        for _name, info in _RECOMMENDED.items():
            assert "pairs" in info
            assert len(info["pairs"]) > 0

    def test_recommendations_have_timeframes(self) -> None:
        for _name, info in _RECOMMENDED.items():
            assert "timeframes" in info
            assert len(info["timeframes"]) > 0

    def test_recommendations_have_descriptions(self) -> None:
        for _name, info in _RECOMMENDED.items():
            assert "description" in info
            assert len(info["description"]) > 20

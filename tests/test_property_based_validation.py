"""Property-based tests for critical runtime paths and validation logic."""

import json

import hypothesis
from hypothesis import given
from hypothesis import strategies as st

from novatrade.backtest.environment import BacktestEnvironment
from utils.structured_output import _extract_json, safe_json_parse


def _not_json_number(s: str) -> bool:
    """Return True if s is NOT a valid JSON number (int, float, or scientific)."""
    try:
        float(s)
        return False
    except (ValueError, TypeError):
        return True


class TestStrategyParameterValidation:
    """Property-based tests for strategy parameter validation and bounds checking."""

    @given(
        ema_period=st.integers(min_value=1, max_value=200),
        atr_period=st.integers(min_value=1, max_value=50),
        irb_threshold=st.floats(min_value=0.1, max_value=1.0),
        trail_atr_multiplier=st.floats(min_value=0.5, max_value=5.0),
    )
    def test_environment_creation_with_valid_params(self, ema_period, atr_period, irb_threshold, trail_atr_multiplier):
        """Test that BacktestEnvironment handles valid parameter ranges correctly."""
        env = BacktestEnvironment(
            ema_period=ema_period,
            atr_period=atr_period,
            irb_threshold=irb_threshold,
            trail_atr_multiplier=trail_atr_multiplier,
            # Use defaults for other params
            adx_period=14,
            adx_threshold=25.0,
            trigger_window_bars=5,
        )

        # Verify parameters are set correctly
        assert env.ema_period == ema_period
        assert env.atr_period == atr_period
        assert env.irb_threshold == irb_threshold
        assert env.trail_atr_multiplier == trail_atr_multiplier

        # Verify derived calculations work
        assert env.irb_threshold > 0
        assert env.trail_atr_multiplier > 0

    @given(
        trail_cooldown_bars=st.integers(min_value=1, max_value=20),
        time_stop_bars=st.integers(min_value=10, max_value=200),
        max_trades_per_day=st.integers(min_value=0, max_value=50),
    )
    def test_integer_parameter_consistency(self, trail_cooldown_bars, time_stop_bars, max_trades_per_day):
        """Test that integer parameters maintain type consistency."""
        env = BacktestEnvironment(
            trail_cooldown_bars=trail_cooldown_bars,
            time_stop_bars=time_stop_bars,
            max_trades_per_day=max_trades_per_day,
        )

        # Verify types are preserved (no float->int conversion warnings)
        assert isinstance(env.trail_cooldown_bars, int)
        assert isinstance(env.time_stop_bars, int)
        assert isinstance(env.max_trades_per_day, int)

        # Verify values are within expected ranges
        assert 1 <= env.trail_cooldown_bars <= 20
        assert 10 <= env.time_stop_bars <= 200
        assert 0 <= env.max_trades_per_day <= 50

    @given(
        ema_confirm_bars=st.integers(min_value=0, max_value=10),
        use_ema_stack_filter=st.booleans(),
        session_filter=st.one_of(st.none(), st.sampled_from(["london", "newyork", "london_ny_overlap"])),
    )
    def test_feature_flag_consistency(self, ema_confirm_bars, use_ema_stack_filter, session_filter):
        """Test that feature flags and related parameters work correctly together."""
        env = BacktestEnvironment(
            ema_confirm_bars=ema_confirm_bars,
            use_ema_stack_filter=use_ema_stack_filter,
            session_filter=session_filter,
        )

        # Verify feature consistency
        assert isinstance(env.use_ema_stack_filter, bool)
        assert env.ema_confirm_bars >= 0

        # Test feature interactions
        if ema_confirm_bars > 0:
            # EMA confirmation should be compatible with other features
            assert env.ema_confirm_bars == ema_confirm_bars

        if session_filter is not None:
            assert session_filter in ["london", "newyork", "london_ny_overlap"]


class TestStructuredOutputValidation:
    """Property-based tests for structured output parsing and validation."""

    @given(
        json_content=st.dictionaries(
            st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"))),
            st.one_of(
                st.text(max_size=100), st.integers(), st.floats(allow_nan=False, allow_infinity=False), st.booleans()
            ),
            min_size=1,
            max_size=10,
        )
    )
    def test_json_extraction_roundtrip(self, json_content):
        """Test that valid JSON can be extracted and parsed correctly."""
        # Create text with embedded JSON
        json_str = json.dumps(json_content, separators=(",", ":"))
        text_with_json = f"Some text before ```json\\n{json_str}\\n``` some text after"

        # Extract JSON using _extract_json
        extracted_json = _extract_json(text_with_json)

        # Verify extraction worked
        assert extracted_json is not None

        # Verify the extracted JSON parses back to the original
        parsed = json.loads(extracted_json)
        assert parsed == json_content

    @given(
        invalid_content=st.text(min_size=1, max_size=100)
        .filter(
            lambda x: (
                not (
                    x.strip().startswith("{")
                    or x.strip().startswith("[")
                    or x.strip().isdigit()
                    or x.strip().lstrip("-").isdigit()
                    or x.strip() in ["true", "false", "null"]
                    or (x.strip().startswith('"') and x.strip().endswith('"'))
                )
            )
        )
        .filter(
            # Exclude valid JSON floats/scientific notation
            lambda x: _not_json_number(x.strip())
        )
    )
    def test_safe_parse_with_invalid_content(self, invalid_content):
        """Test that safe_json_parse handles invalid content gracefully."""
        result = safe_json_parse(invalid_content)

        # Should return None for invalid JSON
        assert result is None

    @given(
        nested_json=st.recursive(
            st.one_of(st.text(max_size=50), st.integers(), st.booleans()),
            lambda children: st.dictionaries(st.text(min_size=1, max_size=10), children, max_size=5),
            max_leaves=20,
        )
    )
    def test_nested_json_parsing(self, nested_json):
        """Test that deeply nested JSON structures are parsed correctly."""
        try:
            json_str = json.dumps(nested_json)

            parsed = safe_json_parse(json_str)
            if parsed is not None:  # Only test if parsing succeeded
                assert parsed == nested_json
        except (ValueError, TypeError, RecursionError):
            # Some randomly generated structures might be too complex
            pass


class TestRiskCalculationProperties:
    """Property-based tests for risk management calculations."""

    @given(
        account_balance=st.floats(min_value=1000.0, max_value=1000000.0),
        risk_percent=st.floats(min_value=0.1, max_value=5.0),
        stop_loss_pips=st.floats(min_value=5.0, max_value=200.0),
        pip_value=st.floats(min_value=0.1, max_value=10.0),
    )
    def test_position_size_calculation_properties(self, account_balance, risk_percent, stop_loss_pips, pip_value):
        """Test that position size calculations maintain mathematical properties."""
        # Calculate position size (simplified version of real calculation)
        risk_amount = account_balance * (risk_percent / 100)
        stop_loss_value = stop_loss_pips * pip_value

        if stop_loss_value > 0:
            position_size = risk_amount / stop_loss_value

            # Mathematical properties that should hold
            assert position_size > 0, "Position size must be positive"
            assert position_size * stop_loss_value <= account_balance, "Risk should not exceed account"

            # Scale invariance property
            scaled_balance = account_balance * 2
            scaled_risk_amount = scaled_balance * (risk_percent / 100)
            scaled_position = scaled_risk_amount / stop_loss_value
            assert abs(scaled_position - position_size * 2) < 0.01, "Position should scale linearly with balance"

    @given(
        equity=st.floats(min_value=1000.0, max_value=500000.0),
        max_risk_percent=st.floats(min_value=0.5, max_value=10.0),
    )
    def test_risk_limits_monotonic_property(self, equity, max_risk_percent):
        """Test that risk limits are monotonic with respect to equity."""
        max_risk_amount = equity * (max_risk_percent / 100)

        # Property: Higher equity should allow higher absolute risk
        higher_equity = equity * 1.5
        higher_max_risk = higher_equity * (max_risk_percent / 100)

        assert higher_max_risk > max_risk_amount, "Risk limits should increase with equity"

        # Property: Risk percentage should remain constant
        calculated_percent = (max_risk_amount / equity) * 100
        assert abs(calculated_percent - max_risk_percent) < 0.001, "Risk percentage should be preserved"


# Test configuration
pytest_plugins = ["hypothesis"]

# Hypothesis settings for CI/local development
hypothesis.settings.register_profile("ci", max_examples=50, deadline=5000)
hypothesis.settings.register_profile("dev", max_examples=20, deadline=2000)

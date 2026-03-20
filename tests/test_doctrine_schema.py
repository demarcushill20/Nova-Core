"""Tests for novatrade.doctrine.schema — StrategyDoctrine model."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from novatrade.doctrine.schema import (
    DoctrineCampaign,
    DoctrineData,
    DoctrineExecution,
    DoctrineValidation,
    ParamBound,
    StrategyDoctrine,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_doctrine(**kw) -> StrategyDoctrine:
    """Build a minimal valid doctrine for testing."""
    defaults = dict(
        name="Test Doctrine",
        concept="IRB reversal on H1",
        data=DoctrineData(pair="EURUSD"),
    )
    defaults.update(kw)
    return StrategyDoctrine(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ParamBound tests
# ---------------------------------------------------------------------------


class TestParamBound:
    def test_valid_bound(self):
        pb = ParamBound(default=0.45, min=0.30, max=0.60, step=0.01)
        assert pb.default == 0.45
        assert pb.min == 0.30
        assert pb.max == 0.60

    def test_default_at_min(self):
        pb = ParamBound(default=0.30, min=0.30, max=0.60)
        assert pb.default == pb.min

    def test_default_at_max(self):
        pb = ParamBound(default=0.60, min=0.30, max=0.60)
        assert pb.default == pb.max

    def test_min_greater_than_max_fails(self):
        with pytest.raises(ValueError, match=r"min.*>.*max"):
            ParamBound(default=0.45, min=0.60, max=0.30)

    def test_default_below_min_fails(self):
        with pytest.raises(ValueError, match=r"default.*not in"):
            ParamBound(default=0.20, min=0.30, max=0.60)

    def test_default_above_max_fails(self):
        with pytest.raises(ValueError, match=r"default.*not in"):
            ParamBound(default=0.70, min=0.30, max=0.60)

    def test_negative_step_fails(self):
        with pytest.raises(ValueError, match="step must be positive"):
            ParamBound(default=0.45, min=0.30, max=0.60, step=-0.01)

    def test_zero_step_fails(self):
        with pytest.raises(ValueError, match="step must be positive"):
            ParamBound(default=0.45, min=0.30, max=0.60, step=0.0)

    def test_none_step_allowed(self):
        pb = ParamBound(default=0.45, min=0.30, max=0.60, step=None)
        assert pb.step is None

    def test_frozen(self):
        pb = ParamBound(default=0.45, min=0.30, max=0.60)
        with pytest.raises(Exception):  # noqa: B017
            pb.default = 0.50  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DoctrineData tests
# ---------------------------------------------------------------------------


class TestDoctrineData:
    def test_defaults(self):
        d = DoctrineData(pair="EURUSD")
        assert d.pair == "EURUSD"
        assert d.timeframes == ["H1"]
        assert d.min_bars == 5000

    def test_min_bars_too_low(self):
        with pytest.raises(Exception):  # noqa: B017
            DoctrineData(pair="EURUSD", min_bars=50)

    def test_custom_timeframes(self):
        d = DoctrineData(pair="GBPUSD", timeframes=["M15", "H1"])
        assert d.timeframes == ["M15", "H1"]


# ---------------------------------------------------------------------------
# StrategyDoctrine tests
# ---------------------------------------------------------------------------


class TestStrategyDoctrine:
    def test_minimal_creation(self):
        d = _minimal_doctrine()
        assert d.name == "Test Doctrine"
        assert d.concept == "IRB reversal on H1"
        assert d.data.pair == "EURUSD"

    def test_defaults(self):
        d = _minimal_doctrine()
        assert d.version == "1.0.0"
        assert d.author == "nova"
        assert d.setup_type == "reversal"
        assert d.entry_signal == "irb_rejection"
        assert isinstance(d.execution, DoctrineExecution)
        assert isinstance(d.validation, DoctrineValidation)
        assert isinstance(d.campaign, DoctrineCampaign)

    def test_mutable_params(self):
        params = {
            "irb_threshold": ParamBound(default=0.45, min=0.30, max=0.60, step=0.01),
        }
        d = _minimal_doctrine(mutable_params=params)
        assert "irb_threshold" in d.mutable_params
        assert d.mutable_params["irb_threshold"].default == 0.45

    def test_immutable_params(self):
        d = _minimal_doctrine(immutable_params={"risk_per_trade_pct": 1.0})
        assert d.immutable_params["risk_per_trade_pct"] == 1.0

    def test_filter_consistency_mandatory_optional_conflict(self):
        with pytest.raises(ValueError, match="mandatory and optional"):
            _minimal_doctrine(
                filters_mandatory=["trend_alignment"],
                filters_optional=["trend_alignment"],
            )

    def test_filter_consistency_mandatory_forbidden_conflict(self):
        with pytest.raises(ValueError, match="mandatory and forbidden"):
            _minimal_doctrine(
                filters_mandatory=["trend_alignment"],
                filters_forbidden=["trend_alignment"],
            )

    def test_filter_consistency_optional_forbidden_conflict(self):
        with pytest.raises(ValueError, match="optional and forbidden"):
            _minimal_doctrine(
                filters_optional=["adx_strength"],
                filters_forbidden=["adx_strength"],
            )

    def test_filter_consistency_no_overlap(self):
        d = _minimal_doctrine(
            filters_mandatory=["trend_alignment"],
            filters_optional=["adx_strength"],
            filters_forbidden=["news_blackout"],
        )
        assert d.filters_mandatory == ["trend_alignment"]
        assert d.filters_optional == ["adx_strength"]
        assert d.filters_forbidden == ["news_blackout"]

    def test_content_hash_deterministic(self):
        d1 = _minimal_doctrine()
        d2 = _minimal_doctrine()
        assert d1.content_hash() == d2.content_hash()

    def test_content_hash_changes_with_params(self):
        d1 = _minimal_doctrine()
        d2 = _minimal_doctrine(
            mutable_params={
                "irb_threshold": ParamBound(default=0.50, min=0.30, max=0.60),
            }
        )
        assert d1.content_hash() != d2.content_hash()

    def test_frozen(self):
        d = _minimal_doctrine()
        with pytest.raises(Exception):  # noqa: B017
            d.name = "modified"  # type: ignore[misc]

    def test_extra_params_escape_hatch(self):
        d = _minimal_doctrine(extra_params={"custom_field": 42})
        assert d.extra_params["custom_field"] == 42

    def test_to_dict_from_dict_roundtrip(self):
        d = _minimal_doctrine(
            mutable_params={
                "irb_threshold": ParamBound(default=0.45, min=0.30, max=0.60, step=0.01),
            },
            immutable_params={"risk_per_trade_pct": 1.0},
        )
        data = d.to_dict()
        d2 = StrategyDoctrine.from_dict(data)
        assert d == d2
        assert d.content_hash() == d2.content_hash()


# ---------------------------------------------------------------------------
# YAML round-trip tests
# ---------------------------------------------------------------------------


class TestDoctrineYAML:
    def test_yaml_roundtrip(self, tmp_path: Path):
        d = _minimal_doctrine(
            mutable_params={
                "irb_threshold": ParamBound(default=0.45, min=0.30, max=0.60, step=0.01),
                "ema_period": ParamBound(default=20, min=10, max=50, step=1),
            },
            immutable_params={"risk_per_trade_pct": 1.0},
            filters_mandatory=["trend_alignment"],
        )
        yaml_path = tmp_path / "doctrine.yaml"
        d.to_yaml(yaml_path)

        d2 = StrategyDoctrine.from_yaml(yaml_path)
        assert d == d2

    def test_yaml_creates_parent_dirs(self, tmp_path: Path):
        d = _minimal_doctrine()
        yaml_path = tmp_path / "deep" / "nested" / "doctrine.yaml"
        d.to_yaml(yaml_path)
        assert yaml_path.exists()

    def test_yaml_load_dump_load_stable(self, tmp_path: Path):
        """Round-trip stability: load → dump → load = identical."""
        d = _minimal_doctrine(
            mutable_params={
                "irb_threshold": ParamBound(default=0.45, min=0.30, max=0.60),
            },
        )
        path1 = tmp_path / "d1.yaml"
        path2 = tmp_path / "d2.yaml"

        d.to_yaml(path1)
        d2 = StrategyDoctrine.from_yaml(path1)
        d2.to_yaml(path2)

        # Compare raw YAML content
        text1 = path1.read_text()
        text2 = path2.read_text()
        assert text1 == text2

    def test_yaml_empty_file_fails(self, tmp_path: Path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        with pytest.raises(Exception):  # noqa: B017
            StrategyDoctrine.from_yaml(path)

    def test_yaml_readable_format(self, tmp_path: Path):
        d = _minimal_doctrine()
        path = tmp_path / "doctrine.yaml"
        d.to_yaml(path)
        raw = yaml.safe_load(path.read_text())
        assert raw["name"] == "Test Doctrine"
        assert raw["data"]["pair"] == "EURUSD"

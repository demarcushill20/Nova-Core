"""Tests for novatrade.validation.migration_audit — provider swap readiness."""

from __future__ import annotations

from pathlib import Path

import pytest

from novatrade.models import (
    AuditFinding,
    AuditSeverity,
    MigrationAuditResult,
)
from novatrade.validation.migration_audit import (
    _find_provider_patterns,
    format_audit,
    run_migration_audit,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_package(tmp_path: Path) -> Path:
    """Create a minimal clean novatrade package structure."""
    pkg = tmp_path / "novatrade"
    for subdir in ["execution", "risk", "monitor", "validation", "adapter"]:
        (pkg / subdir).mkdir(parents=True)
        (pkg / subdir / "__init__.py").write_text("")

    # Clean models
    (pkg / "models.py").write_text("# Provider-neutral models\nclass Foo: pass\n")
    (pkg / "config.py").write_text("provider = 'metaapi'\n")

    # Clean adapter base
    (pkg / "adapter" / "base.py").write_text("# Provider-neutral adapter ABC\nclass MT5Adapter: pass\n")

    # Clean core logic
    (pkg / "execution" / "executor.py").write_text("# No provider references\ndef execute(): pass\n")
    (pkg / "risk" / "gate.py").write_text("# No provider references\ndef check(): pass\n")
    (pkg / "monitor" / "health.py").write_text("# No provider references\ndef snapshot(): pass\n")
    (pkg / "validation" / "evidence.py").write_text("# No provider references\ndef record(): pass\n")

    return pkg


@pytest.fixture
def coupled_package(clean_package: Path) -> Path:
    """Create a package with provider coupling in core logic."""
    # Add provider-specific reference in execution layer
    (clean_package / "execution" / "executor.py").write_text("from metaapi import MetaApi\ndef execute(): pass\n")
    return clean_package


@pytest.fixture
def coupled_adapter_base(clean_package: Path) -> Path:
    """Create a package with provider coupling in adapter base."""
    (clean_package / "adapter" / "base.py").write_text(
        '"""Every adapter (MetaApi, etc.) implements this."""\nclass MT5Adapter: pass\n'
    )
    return clean_package


# ===========================================================================
# Clean / ready case
# ===========================================================================


class TestCleanPackage:
    def test_clean_package_is_swap_ready(self, clean_package: Path):
        scripts = clean_package.parent / "scripts"
        scripts.mkdir(exist_ok=True)
        result = run_migration_audit(clean_package, scripts)
        assert result.swap_ready is True
        assert result.blocking_count == 0

    def test_clean_has_ready_findings(self, clean_package: Path):
        scripts = clean_package.parent / "scripts"
        scripts.mkdir(exist_ok=True)
        result = run_migration_audit(clean_package, scripts)
        ready = [f for f in result.findings if f.severity == AuditSeverity.READY]
        assert len(ready) > 0

    def test_models_are_ready(self, clean_package: Path):
        scripts = clean_package.parent / "scripts"
        scripts.mkdir(exist_ok=True)
        result = run_migration_audit(clean_package, scripts)
        model_findings = [f for f in result.findings if "models" in f.location]
        assert all(f.severity == AuditSeverity.READY for f in model_findings)


# ===========================================================================
# Blocking coupling case
# ===========================================================================


class TestBlockingCoupling:
    def test_core_logic_coupling_blocks(self, coupled_package: Path):
        scripts = coupled_package.parent / "scripts"
        scripts.mkdir(exist_ok=True)
        result = run_migration_audit(coupled_package, scripts)
        assert result.swap_ready is False
        assert result.blocking_count >= 1

    def test_blocking_finding_has_location(self, coupled_package: Path):
        scripts = coupled_package.parent / "scripts"
        scripts.mkdir(exist_ok=True)
        result = run_migration_audit(coupled_package, scripts)
        blockers = [f for f in result.findings if f.severity == AuditSeverity.BLOCKING_COUPLING]
        assert all(f.location for f in blockers)
        assert all(f.recommendation for f in blockers)

    def test_adapter_base_coupling_blocks(self, coupled_adapter_base: Path):
        scripts = coupled_adapter_base.parent / "scripts"
        scripts.mkdir(exist_ok=True)
        result = run_migration_audit(coupled_adapter_base, scripts)
        assert result.swap_ready is False
        base_findings = [
            f for f in result.findings if "base.py" in f.location and f.severity == AuditSeverity.BLOCKING_COUPLING
        ]
        assert len(base_findings) == 1


# ===========================================================================
# Minor refactor case
# ===========================================================================


class TestMinorRefactor:
    def test_config_finding(self, clean_package: Path):
        scripts = clean_package.parent / "scripts"
        scripts.mkdir(exist_ok=True)
        result = run_migration_audit(clean_package, scripts)
        config_findings = [f for f in result.findings if f.location == "config.py"]
        assert len(config_findings) == 1
        assert config_findings[0].severity == AuditSeverity.MINOR_REFACTOR
        assert config_findings[0].defer_ok is True

    def test_script_coupling_is_minor(self, clean_package: Path):
        scripts = clean_package.parent / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "novatrade_demo.py").write_text("from novatrade.adapter.metaapi_provider import MetaApiAdapter\n")
        result = run_migration_audit(clean_package, scripts)
        script_findings = [
            f for f in result.findings if "scripts/" in f.location and f.severity == AuditSeverity.MINOR_REFACTOR
        ]
        assert len(script_findings) >= 1
        assert all(f.defer_ok for f in script_findings)


# ===========================================================================
# Real package audit
# ===========================================================================


class TestRealPackage:
    def test_real_novatrade_is_swap_ready(self):
        """The actual NovaTrade package should be swap-ready after fixes."""
        result = run_migration_audit()
        assert result.swap_ready is True
        assert result.blocking_count == 0

    def test_real_has_ready_core_areas(self):
        result = run_migration_audit()
        ready_locations = {f.location for f in result.findings if f.severity == AuditSeverity.READY}
        assert "models.py" in ready_locations
        assert "execution/" in ready_locations
        assert "risk/" in ready_locations
        assert "monitor/" in ready_locations

    def test_real_scripts_are_minor(self):
        result = run_migration_audit()
        script_findings = [f for f in result.findings if "scripts/" in f.location]
        assert all(f.severity == AuditSeverity.MINOR_REFACTOR for f in script_findings)
        assert all(f.defer_ok for f in script_findings)

    def test_real_config_is_minor(self):
        result = run_migration_audit()
        config_findings = [f for f in result.findings if f.location == "config.py"]
        assert len(config_findings) == 1
        assert config_findings[0].severity == AuditSeverity.MINOR_REFACTOR


# ===========================================================================
# Pattern detection
# ===========================================================================


class TestPatternDetection:
    def test_finds_metaapi(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("from metaapi_cloud_sdk import MetaApi\n")
        assert "MetaApi" in _find_provider_patterns(f)
        assert "metaapi" in _find_provider_patterns(f)

    def test_ignores_comments(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("# This module does not use MetaApi\nclass Foo: pass\n")
        assert len(_find_provider_patterns(f)) == 0

    def test_ignores_provider_neutral_docs(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("# Provider-neutral: no MetaApi imports\nclass Foo: pass\n")
        assert len(_find_provider_patterns(f)) == 0

    def test_finds_env_vars(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text('token = os.environ["METAAPI_TOKEN"]\n')
        assert "METAAPI" in _find_provider_patterns(f)

    def test_clean_file(self, tmp_path: Path):
        f = tmp_path / "test.py"
        f.write_text("class OrderRequest:\n    pass\n")
        assert len(_find_provider_patterns(f)) == 0

    def test_nonexistent_file(self, tmp_path: Path):
        f = tmp_path / "nonexistent.py"
        assert len(_find_provider_patterns(f)) == 0


# ===========================================================================
# Model tests
# ===========================================================================


class TestAuditModels:
    def test_finding_defaults(self):
        f = AuditFinding(
            location="test.py",
            description="test",
            severity=AuditSeverity.READY,
            recommendation="",
        )
        assert f.defer_ok is True

    def test_severity_values(self):
        assert AuditSeverity.READY.value == "READY"
        assert AuditSeverity.MINOR_REFACTOR.value == "MINOR_REFACTOR"
        assert AuditSeverity.BLOCKING_COUPLING.value == "BLOCKING_COUPLING"

    def test_result_properties(self):
        result = MigrationAuditResult(
            swap_ready=True,
            findings=[
                AuditFinding("a", "x", AuditSeverity.READY, ""),
                AuditFinding("b", "x", AuditSeverity.MINOR_REFACTOR, "y"),
                AuditFinding("c", "x", AuditSeverity.BLOCKING_COUPLING, "z"),
                AuditFinding("d", "x", AuditSeverity.READY, ""),
            ],
        )
        assert result.ready_count == 2
        assert result.minor_count == 1
        assert result.blocking_count == 1


# ===========================================================================
# Formatting
# ===========================================================================


class TestFormatAudit:
    def test_ready_format(self):
        result = run_migration_audit()
        text = format_audit(result)
        assert "Swap-ready:" in text
        assert "YES" in text

    def test_blocking_format(self, coupled_package: Path):
        scripts = coupled_package.parent / "scripts"
        scripts.mkdir(exist_ok=True)
        result = run_migration_audit(coupled_package, scripts)
        text = format_audit(result)
        assert "NO" in text
        assert "Blocking" in text
        assert "[!]" in text

    def test_minor_format(self):
        result = run_migration_audit()
        text = format_audit(result)
        assert "Minor" in text

    def test_ready_section(self):
        result = run_migration_audit()
        text = format_audit(result)
        assert "[OK]" in text

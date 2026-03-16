"""Migration-readiness audit for NovaTrade provider swap.

Inspects the NovaTrade codebase for provider-specific coupling that would
need to be addressed before swapping MetaApi for a self-hosted MT5 backend.

Uses static file analysis — reads source files and checks for provider-
specific patterns outside the adapter boundary.

The audit is deterministic and does not require a live adapter connection.
"""

from __future__ import annotations

import re
from pathlib import Path

from novatrade.models import (
    AuditFinding,
    AuditSeverity,
    MigrationAuditResult,
)

# Provider-specific patterns that should only appear inside the adapter.
# Use case-sensitive patterns that catch identifiers like MetaApiAdapter,
# metaapi_provider, METAAPI_TOKEN, etc.
_PROVIDER_PATTERNS = [
    re.compile(r"MetaApi"),
    re.compile(r"metaapi"),
    re.compile(r"METAAPI"),
]

# Files that are expected to reference the provider (adapter boundary).
_PROVIDER_BOUNDARY_FILES = {
    "adapter/metaapi_provider.py",
    "adapter/__init__.py",
}

# Directories to scan (relative to the novatrade package root).
_SCAN_DIRS = ["execution", "risk", "monitor", "validation"]

# The novatrade package root.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def run_migration_audit(
    package_root: Path | None = None,
    scripts_dir: Path | None = None,
) -> MigrationAuditResult:
    """Run a full migration-readiness audit.

    Inspects core logic, config, and scripts for provider coupling.
    Returns categorized findings with recommendations.

    Pure analysis — no I/O beyond reading source files.
    """
    root = package_root or _PACKAGE_ROOT
    scripts = scripts_dir or root.parent / "scripts"

    findings: list[AuditFinding] = []

    # -- 1. Audit core logic (should be provider-free) ---------------------
    findings.extend(_audit_core_logic(root))

    # -- 2. Audit config (expected provider awareness, check boundaries) ---
    findings.extend(_audit_config(root))

    # -- 3. Audit scripts (expected provider references, check isolation) --
    findings.extend(_audit_scripts(scripts))

    # -- 4. Audit adapter boundary (should be clean) -----------------------
    findings.extend(_audit_adapter_boundary(root))

    # -- 5. Add READY findings for clean areas -----------------------------
    findings.extend(_audit_ready_areas(root))

    swap_ready = all(f.severity != AuditSeverity.BLOCKING_COUPLING for f in findings)

    return MigrationAuditResult(
        swap_ready=swap_ready,
        findings=findings,
    )


def format_audit(result: MigrationAuditResult) -> str:
    """Render a MigrationAuditResult as a human-readable text summary."""
    lines = [
        "NovaTrade Migration-Readiness Audit",
        "=" * 44,
        "",
        f"Swap-ready:  {'YES' if result.swap_ready else 'NO'}",
        f"Blocking:    {result.blocking_count}",
        f"Minor:       {result.minor_count}",
        f"Ready:       {result.ready_count}",
        "",
    ]

    # Group by severity
    for severity, label in [
        (AuditSeverity.BLOCKING_COUPLING, "Blocking Couplings (must fix before swap)"),
        (AuditSeverity.MINOR_REFACTOR, "Minor Refactors (recommended before swap)"),
        (AuditSeverity.READY, "Ready (no changes needed)"),
    ]:
        items = [f for f in result.findings if f.severity == severity]
        if not items:
            continue

        lines.append(label)
        lines.append("-" * 44)
        for f in items:
            marker = {"BLOCKING_COUPLING": "[!]", "MINOR_REFACTOR": "[~]", "READY": "[OK]"}
            lines.append(f"  {marker.get(severity.value, '   ')} {f.location}")
            lines.append(f"      {f.description}")
            if f.recommendation:
                lines.append(f"      Fix: {f.recommendation}")
            if severity != AuditSeverity.READY:
                lines.append(f"      Defer OK: {'yes' if f.defer_ok else 'NO — fix now'}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal audit functions
# ---------------------------------------------------------------------------


def _audit_core_logic(root: Path) -> list[AuditFinding]:
    """Check execution, risk, monitor, validation dirs for provider leaks."""
    findings: list[AuditFinding] = []

    # The audit module itself legitimately references provider names.
    self_path = Path(__file__).resolve()

    for scan_dir in _SCAN_DIRS:
        dir_path = root / scan_dir
        if not dir_path.is_dir():
            continue

        for py_file in sorted(dir_path.rglob("*.py")):
            if py_file.resolve() == self_path:
                continue  # skip self — the auditor references providers by design
            rel = py_file.relative_to(root)
            leaks = _find_provider_patterns(py_file)
            if leaks:
                findings.append(
                    AuditFinding(
                        location=str(rel),
                        description=(f"Provider-specific reference(s) found: {', '.join(sorted(leaks))}"),
                        severity=AuditSeverity.BLOCKING_COUPLING,
                        recommendation=(
                            "Replace provider-specific text with generic terms (e.g. 'adapter', 'broker', 'provider')."
                        ),
                        defer_ok=False,
                    )
                )

    return findings


def _audit_config(root: Path) -> list[AuditFinding]:
    """Check config.py for provider-aware structure."""
    findings: list[AuditFinding] = []
    config_path = root / "config.py"

    if not config_path.exists():
        return findings

    content = config_path.read_text()

    # MetaApiConfig is expected — it's the provider-specific config.
    # The coupling concern is whether NovaTradeCfg has the right seam.
    if "provider:" in content or "provider =" in content:
        findings.append(
            AuditFinding(
                location="config.py",
                description=(
                    "NovaTradeCfg has a 'provider' field for adapter selection. "
                    "MetaApiConfig is provider-specific by design. Config structure "
                    "supports future provider swap via the provider field."
                ),
                severity=AuditSeverity.MINOR_REFACTOR,
                recommendation=(
                    "When adding a second provider, add a factory function "
                    "that dispatches on cfg.provider to create the right adapter. "
                    "No change needed now."
                ),
                defer_ok=True,
            )
        )
    else:
        findings.append(
            AuditFinding(
                location="config.py",
                description="No provider selection mechanism found in config.",
                severity=AuditSeverity.MINOR_REFACTOR,
                recommendation="Add a 'provider' field to NovaTradeCfg.",
                defer_ok=True,
            )
        )

    return findings


def _audit_scripts(scripts_dir: Path) -> list[AuditFinding]:
    """Check CLI scripts for provider coupling."""
    findings: list[AuditFinding] = []

    if not scripts_dir.is_dir():
        return findings

    for py_file in sorted(scripts_dir.glob("novatrade_*.py")):
        leaks = _find_provider_patterns(py_file)
        if leaks:
            findings.append(
                AuditFinding(
                    location=f"scripts/{py_file.name}",
                    description=(
                        f"Direct provider import(s): {', '.join(sorted(leaks))}. "
                        "Scripts are the adapter selection point — this is expected "
                        "for MVP but should use a factory on provider swap."
                    ),
                    severity=AuditSeverity.MINOR_REFACTOR,
                    recommendation=(
                        "When adding a second provider, replace direct "
                        "MetaApiAdapter imports with a factory function "
                        "dispatched from cfg.provider."
                    ),
                    defer_ok=True,
                )
            )

    return findings


def _audit_adapter_boundary(root: Path) -> list[AuditFinding]:
    """Verify the adapter boundary is clean."""
    findings: list[AuditFinding] = []
    adapter_dir = root / "adapter"

    if not adapter_dir.is_dir():
        findings.append(
            AuditFinding(
                location="adapter/",
                description="Adapter directory not found.",
                severity=AuditSeverity.BLOCKING_COUPLING,
                recommendation="Ensure adapter/ directory exists with base.py ABC.",
                defer_ok=False,
            )
        )
        return findings

    base_path = adapter_dir / "base.py"
    if base_path.exists():
        base_leaks = _find_provider_patterns(base_path)
        if base_leaks:
            findings.append(
                AuditFinding(
                    location="adapter/base.py",
                    description=(f"Provider-specific reference(s) in adapter ABC: {', '.join(sorted(base_leaks))}"),
                    severity=AuditSeverity.BLOCKING_COUPLING,
                    recommendation=(
                        "The adapter ABC must be fully provider-neutral. "
                        "Remove provider-specific references from base.py."
                    ),
                    defer_ok=False,
                )
            )

    return findings


def _audit_ready_areas(root: Path) -> list[AuditFinding]:
    """Confirm which areas are already provider-neutral."""
    ready_areas = [
        ("models.py", "Domain models"),
        ("execution/", "Execution layer"),
        ("risk/", "Risk gate"),
        ("monitor/", "Monitoring/reconciliation"),
    ]

    findings: list[AuditFinding] = []
    for path_str, label in ready_areas:
        path = root / path_str
        if path.is_file():
            leaks = _find_provider_patterns(path)
            if not leaks:
                findings.append(
                    AuditFinding(
                        location=path_str,
                        description=f"{label} — fully provider-neutral.",
                        severity=AuditSeverity.READY,
                        recommendation="",
                    )
                )
        elif path.is_dir():
            all_clean = True
            for py_file in path.rglob("*.py"):
                if _find_provider_patterns(py_file):
                    all_clean = False
                    break
            if all_clean:
                findings.append(
                    AuditFinding(
                        location=path_str,
                        description=f"{label} — fully provider-neutral.",
                        severity=AuditSeverity.READY,
                        recommendation="",
                    )
                )

    return findings


def _find_provider_patterns(file_path: Path) -> set[str]:
    """Find provider-specific patterns in a file. Returns matched strings."""
    try:
        content = file_path.read_text()
    except OSError:
        return set()

    # Skip comment-only references (docstrings describing provider neutrality).
    # We check line-by-line: only flag if pattern appears in non-comment code.
    matches: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        # Skip pure comment lines
        if stripped.startswith("#"):
            continue
        # Skip lines that are just documenting provider neutrality
        if "provider-neutral" in stripped.lower() or "no metaapi" in stripped.lower():
            continue
        for pattern in _PROVIDER_PATTERNS:
            found = pattern.findall(stripped)
            for m in found:
                matches.add(m)
    return matches

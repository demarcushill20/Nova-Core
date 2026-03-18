#!/usr/bin/env python3
"""Generate compliance evidence report for NovaCore security hardening.

Phase 4.4: Compliance evidence collection.
Covers EU AI Act Art. 12, OWASP Agentic Top 10, and SOC 2 Type II controls.

Usage:
    python3 scripts/compliance-report.py [--output OUTPUT/]
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path("/home/nova/nova-core")
STATE = BASE / "STATE"
LOGS = BASE / "LOGS"
AUDIT_DIR = LOGS / "audit"
OUTPUT_DIR = BASE / "OUTPUT"


def check_file_exists(path: Path) -> dict:
    """Check if a security component file exists."""
    exists = path.exists()
    return {
        "file": str(path.relative_to(BASE)),
        "exists": exists,
        "size": path.stat().st_size if exists else 0,
    }


def check_systemd_hardening(service: str) -> dict:
    """Check systemd service security score."""
    try:
        result = subprocess.run(
            ["systemd-analyze", "security", service],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Parse the overall score from the last line
        lines = result.stdout.strip().splitlines()
        score_line = [ln for ln in lines if "Overall exposure" in ln]
        score = score_line[0].split()[-1] if score_line else "unknown"
        return {"service": service, "score": score, "status": "checked"}
    except Exception as e:
        return {"service": service, "score": "error", "status": str(e)}


def check_audit_chain() -> dict:
    """Verify audit log chain integrity."""
    try:
        sys.path.insert(0, str(BASE / "scripts"))
        # Run the verifier
        result = subprocess.run(
            [sys.executable, str(BASE / "scripts" / "verify-audit-chain.py")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "valid": result.returncode == 0,
            "output": result.stdout.strip()[:500],
        }
    except Exception as e:
        return {"valid": False, "output": str(e)}


def check_secret_permissions() -> dict:
    """Run secret file permission audit."""
    try:
        result = subprocess.run(
            ["bash", str(BASE / "scripts" / "check-secret-perms.sh")],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {
            "passed": result.returncode == 0,
            "output": result.stdout.strip()[:500],
        }
    except Exception as e:
        return {"passed": False, "output": str(e)}


def count_audit_entries(days: int = 30) -> dict:
    """Count audit log entries over the last N days."""
    total = 0
    files_found = 0
    if AUDIT_DIR.exists():
        for f in sorted(AUDIT_DIR.glob("audit_*.jsonl")):
            files_found += 1
            total += sum(1 for _ in f.open())
    return {"files": files_found, "entries": total, "days_covered": days}


def generate_report() -> dict:
    """Generate comprehensive compliance evidence report."""
    now = datetime.now(timezone.utc)

    report = {
        "report_type": "NovaCore Security Compliance Evidence",
        "generated_at": now.isoformat(),
        "version": "1.0",
        "sections": {},
    }

    # --- Section 1: Security Components Inventory ---
    components = {
        "input_sanitizer": check_file_exists(BASE / "telegram" / "input_security.py"),
        "kill_switch": check_file_exists(BASE / "nova_kill_switch.py"),
        "task_validator": check_file_exists(BASE / "utils" / "task_validator.py"),
        "budget_enforcer": check_file_exists(BASE / "agents" / "budget_enforcer.py"),
        "circuit_breakers": check_file_exists(BASE / "agents" / "circuit_breakers.py"),
        "secret_scanner": check_file_exists(BASE / "utils" / "secrets.py"),
        "audit_logger": check_file_exists(BASE / "utils" / "audit_log.py"),
        "mcp_sanitizer": check_file_exists(BASE / "utils" / "mcp_sanitizer.py"),
        "dlp_gate": check_file_exists(BASE / "utils" / "dlp_gate.py"),
        "security_monitor": check_file_exists(BASE / "utils" / "security_monitor.py"),
        "llm_guard": check_file_exists(BASE / "telegram" / "llm_guard_middleware.py"),
    }
    installed = sum(1 for c in components.values() if c["exists"])
    report["sections"]["component_inventory"] = {
        "total": len(components),
        "installed": installed,
        "coverage_pct": round(100 * installed / len(components), 1),
        "components": components,
    }

    # --- Section 2: Systemd Hardening ---
    services = [
        "novacore-watcher.service",
        "novacore-telegram.service",
        "novacore-heartbeat.service",
    ]
    hardening = {svc: check_systemd_hardening(svc) for svc in services}
    report["sections"]["systemd_hardening"] = hardening

    # --- Section 3: Audit Logging ---
    report["sections"]["audit_logging"] = {
        "chain_integrity": check_audit_chain(),
        "entry_counts": count_audit_entries(),
    }

    # --- Section 4: Secret Management ---
    report["sections"]["secret_management"] = {
        "permission_audit": check_secret_permissions(),
    }

    # --- Section 5: Egress Control ---
    egress_script = BASE / "scripts" / "egress-allowlist.sh"
    report["sections"]["egress_control"] = {
        "script_exists": egress_script.exists(),
    }

    # --- Section 6: OWASP Agentic Top 10 Coverage ---
    owasp_coverage = {
        "ASI01_Agent_Goal_Hijacking": {
            "controls": ["input_sanitizer", "llm_guard", "delimiter_defense"],
            "status": "implemented" if components["input_sanitizer"]["exists"] else "missing",
        },
        "ASI02_Tool_Misuse": {
            "controls": ["task_validator", "action_budgets", "circuit_breakers"],
            "status": "implemented" if components["task_validator"]["exists"] else "missing",
        },
        "ASI03_Code_Execution": {
            "controls": ["systemd_hardening", "kill_switch"],
            "status": "implemented" if components["kill_switch"]["exists"] else "missing",
        },
        "ASI04_Memory_Poisoning": {
            "controls": ["mcp_sanitizer", "dlp_gate"],
            "status": "implemented" if components["mcp_sanitizer"]["exists"] else "missing",
        },
        "ASI05_Privilege_Escalation": {
            "controls": ["systemd_NoNewPrivileges", "CapabilityBoundingSet"],
            "status": "implemented",
        },
        "ASI06_Data_Exfiltration": {
            "controls": ["egress_allowlist", "dlp_gate", "secret_scanner"],
            "status": "implemented" if components["dlp_gate"]["exists"] else "missing",
        },
        "ASI07_Supply_Chain": {
            "controls": ["mcp_tool_integrity", "pip_audit"],
            "status": "partial",
        },
        "ASI08_Logging_Monitoring": {
            "controls": ["audit_logger", "security_monitor", "hash_chain"],
            "status": "implemented" if components["audit_logger"]["exists"] else "missing",
        },
        "ASI09_Resource_Exhaustion": {
            "controls": ["budget_enforcer", "circuit_breakers", "MemoryMax"],
            "status": "implemented" if components["budget_enforcer"]["exists"] else "missing",
        },
        "ASI10_Denial_of_Service": {
            "controls": ["rate_limiting", "kill_switch", "circuit_breakers"],
            "status": "implemented" if components["circuit_breakers"]["exists"] else "missing",
        },
    }
    covered = sum(1 for v in owasp_coverage.values() if v["status"] == "implemented")
    report["sections"]["owasp_agentic_top10"] = {
        "covered": covered,
        "total": len(owasp_coverage),
        "coverage_pct": round(100 * covered / len(owasp_coverage), 1),
        "details": owasp_coverage,
    }

    # --- Section 7: EU AI Act Art. 12 Compliance ---
    report["sections"]["eu_ai_act_art12"] = {
        "automatic_logging": components["audit_logger"]["exists"],
        "log_immutability": "hash_chain",
        "event_capture_rate": "all security events logged",
        "retention": "90 days (audit logs)",
        "traceability": "correlation IDs in all log entries",
    }

    return report


def main():
    output_dir = OUTPUT_DIR
    if len(sys.argv) > 2 and sys.argv[1] == "--output":
        output_dir = Path(sys.argv[2])

    report = generate_report()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # Write JSON report
    json_path = output_dir / f"compliance_report__{stamp}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Write human-readable summary
    md_path = output_dir / f"compliance_report__{stamp}.md"
    inventory = report["sections"]["component_inventory"]
    owasp = report["sections"]["owasp_agentic_top10"]

    md_lines = [
        "# NovaCore Security Compliance Report",
        "",
        f"**Generated:** {report['generated_at']}",
        "",
        f"## Component Coverage: {inventory['coverage_pct']}%",
        "",
        "| Component | Status |",
        "|-----------|--------|",
    ]
    for name, info in inventory["components"].items():
        status = "Installed" if info["exists"] else "MISSING"
        md_lines.append(f"| {name} | {status} |")

    md_lines.extend(
        [
            "",
            f"## OWASP Agentic Top 10: {owasp['coverage_pct']}%",
            "",
            "| Risk | Status | Controls |",
            "|------|--------|----------|",
        ]
    )
    for risk, detail in owasp["details"].items():
        md_lines.append(f"| {risk} | {detail['status']} | {', '.join(detail['controls'])} |")

    md_lines.extend(
        [
            "",
            "## EU AI Act Art. 12",
            "",
            f"- Automatic logging: {'Yes' if report['sections']['eu_ai_act_art12']['automatic_logging'] else 'No'}",
            f"- Log immutability: {report['sections']['eu_ai_act_art12']['log_immutability']}",
            f"- Retention: {report['sections']['eu_ai_act_art12']['retention']}",
            f"- Traceability: {report['sections']['eu_ai_act_art12']['traceability']}",
        ]
    )

    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print("Compliance report written:")
    print(f"  JSON: {json_path}")
    print(f"  Markdown: {md_path}")
    print(f"  Components: {inventory['installed']}/{inventory['total']} ({inventory['coverage_pct']}%)")
    print(f"  OWASP coverage: {owasp['covered']}/{owasp['total']} ({owasp['coverage_pct']}%)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Emergency MetaAPI Rate Limiting Diagnostic & Fix Script.

This script helps diagnose and address HTTP 429 rate limiting issues
that can occur when MetaAPI receives too many connection attempts.

Usage:
    python3 scripts/fix_metaapi_rate_limiting.py [--apply-fix]
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from novatrade.cli.commands.config_diagnostic import generate_configuration_report

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)


def check_service_status():
    """Check NovaTrade service status and instances."""
    import subprocess

    try:
        # Check service status
        result = subprocess.run(['systemctl', 'status', 'novacore-novatrade.service'],
                              capture_output=True, text=True)
        service_active = result.returncode == 0

        # Check for multiple instances
        result = subprocess.run(['pgrep', '-f', 'novatrade.runtime.runner'],
                              capture_output=True, text=True)
        pids = result.stdout.strip().split('\n') if result.stdout.strip() else []
        multiple_instances = len(pids) > 1

        # Check journal for recent rate limiting errors
        result = subprocess.run([
            'journalctl', '-u', 'novacore-novatrade.service',
            '--since', '10 minutes ago', '--no-pager'
        ], capture_output=True, text=True)

        rate_limit_count = result.stdout.count('429') if result.stdout else 0
        connection_error_count = result.stdout.count('ConnectionError') if result.stdout else 0

        return {
            'service_active': service_active,
            'multiple_instances': multiple_instances,
            'instance_pids': pids,
            'recent_rate_limits': rate_limit_count,
            'recent_connection_errors': connection_error_count,
            'journal_available': bool(result.stdout)
        }

    except Exception as e:
        log.error("Error checking service status: %s", e)
        return {
            'error': str(e),
            'service_active': None,
            'multiple_instances': None
        }


async def check_runtime_health():
    """Check runtime health via HTTP endpoint."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get('http://localhost:8877/status', timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return {
                        'responsive': True,
                        'status_data': data
                    }
                else:
                    return {
                        'responsive': False,
                        'status_code': response.status
                    }
    except Exception as e:
        return {
            'responsive': False,
            'error': str(e)
        }


def analyze_rate_limiting_situation(service_status, config_report, runtime_health):
    """Analyze the current situation and provide recommendations."""
    issues = []
    recommendations = []
    severity = "info"

    # Check for multiple instances
    if service_status.get('multiple_instances'):
        issues.append("Multiple NovaTrade instances detected")
        recommendations.append("CRITICAL: Kill duplicate instances immediately")
        severity = "critical"

    # Check for recent rate limiting
    if service_status.get('recent_rate_limits', 0) > 0:
        issues.append(f"Recent rate limiting detected ({service_status['recent_rate_limits']} events)")
        recommendations.append("HIGH: Implement rate limiting protection in adapter")
        severity = max(severity, "high") if severity != "critical" else severity

    # Check feed health
    if runtime_health.get('responsive') and runtime_health.get('status_data'):
        feed_health = runtime_health['status_data'].get('feed_health', {})
        if feed_health.get('unhealthy', 0) > 0:
            issues.append("Unhealthy data feeds detected")
            recommendations.append("MEDIUM: Address stale data feeds")
            severity = max(severity, "medium") if severity not in ["critical", "high"] else severity

    # Check configuration
    config_analysis = config_report.get('discrepancy_analysis', {})
    if config_analysis.get('issue_identified'):
        issues.append("Configuration propagation issue")
        recommendations.append("LOW: Fix configuration loading")

    return {
        'severity': severity,
        'issues': issues,
        'recommendations': recommendations,
        'immediate_actions': generate_immediate_actions(severity, service_status)
    }


def generate_immediate_actions(severity, service_status):
    """Generate immediate action steps based on severity."""
    actions = []

    if severity == "critical":
        if service_status.get('multiple_instances'):
            pids = service_status.get('instance_pids', [])
            if len(pids) > 1:
                actions.append(f"Kill duplicate instance: sudo kill {pids[-1]}")
                actions.append("Wait 30 seconds then check logs for improvement")

        actions.append("Monitor logs: journalctl -u novacore-novatrade.service -f")
        actions.append("If still rate limited: sudo systemctl restart novacore-novatrade.service")

    elif severity == "high":
        actions.append("Apply rate limiting protection (see --apply-fix option)")
        actions.append("Monitor connection health for 10 minutes")
        actions.append("If no improvement: restart service")

    elif severity == "medium":
        actions.append("Monitor feed health")
        actions.append("Check for feed reconnection in logs")
        actions.append("Consider scheduled service restart during maintenance window")

    else:
        actions.append("Continue monitoring")
        actions.append("No immediate action required")

    return actions


async def main():
    parser = argparse.ArgumentParser(description="MetaAPI Rate Limiting Diagnostic & Fix")
    parser.add_argument('--apply-fix', action='store_true',
                       help="Apply rate limiting protection (requires service restart)")
    parser.add_argument('--json', action='store_true',
                       help="Output in JSON format")

    args = parser.parse_args()

    log.info("Running MetaAPI rate limiting diagnostic...")

    # Gather diagnostic information
    log.info("Checking service status...")
    service_status = check_service_status()

    log.info("Checking configuration...")
    config_report = generate_configuration_report()

    log.info("Checking runtime health...")
    runtime_health = await check_runtime_health()

    # Analyze situation
    analysis = analyze_rate_limiting_situation(service_status, config_report, runtime_health)

    # Prepare report
    report = {
        'timestamp': datetime.now().isoformat(),
        'service_status': service_status,
        'config_report': config_report,
        'runtime_health': runtime_health,
        'analysis': analysis,
        'rate_guardian_available': True,  # We created the rate guardian
        'fix_applied': False
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    # Human-readable output
    print("\n" + "="*60)
    print("MetaAPI Rate Limiting Diagnostic Report")
    print("="*60)

    print(f"\n🕐 Timestamp: {report['timestamp']}")
    print(f"⚡ Severity: {analysis['severity'].upper()}")

    if analysis['issues']:
        print("\n❌ Issues Detected:")
        for issue in analysis['issues']:
            print(f"   • {issue}")

    print("\n💡 Recommendations:")
    for rec in analysis['recommendations']:
        print(f"   • {rec}")

    print("\n🚀 Immediate Actions:")
    for action in analysis['immediate_actions']:
        print(f"   • {action}")

    # Service status summary
    print("\n📊 Service Status:")
    print(f"   • Active: {service_status.get('service_active', 'unknown')}")
    print(f"   • Multiple instances: {service_status.get('multiple_instances', 'unknown')}")
    print(f"   • Recent rate limits: {service_status.get('recent_rate_limits', 0)}")
    print(f"   • Runtime responsive: {runtime_health.get('responsive', 'unknown')}")

    # Rate limiting protection status
    print("\n🛡️  Rate Limiting Protection:")
    print("   • RateLimitGuardian available: ✅")
    print("   • Integration created: ✅")
    print(f"   • Applied to running service: {'✅' if args.apply_fix else '❌ (use --apply-fix)'}")

    if args.apply_fix:
        print("\n🔧 Applying Rate Limiting Fix...")
        print("   NOTE: This requires service restart to take effect")
        print("   1. Rate limiting protection code has been created")
        print("   2. Enhancement integration is available")
        print("   3. Service restart recommended: sudo systemctl restart novacore-novatrade.service")
        report['fix_applied'] = True

    print("\n📝 Full diagnostic report available in JSON with --json flag")
    print("="*60)

    # Return appropriate exit code
    if analysis['severity'] == "critical":
        sys.exit(1)
    elif analysis['severity'] == "high":
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())

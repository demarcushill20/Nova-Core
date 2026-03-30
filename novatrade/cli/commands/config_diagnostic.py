"""Configuration diagnostic tool for NovaTrade.

Validates environment -> config -> runtime propagation to identify
configuration discrepancies that prevent proper mode transitions.

Usage:
    python3 -m novatrade.cli.commands.config_diagnostic
"""

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from novatrade.config import NovaTradeCfg


def validate_environment_loading() -> dict[str, Any]:
    """Validate environment variable loading and parsing."""
    env_vars = {}
    config_keys = [
        'NOVATRADE_DRY_RUN',
        'NOVATRADE_LAUNCH_MODE',
        'METAAPI_TOKEN',
        'METAAPI_ACCOUNT_ID',
        'FTMO_SYMBOL_SUFFIX',
        'NOVATRADE_SYMBOLS',
        'NOVATRADE_MAX_POSITIONS'
    ]

    for key in config_keys:
        env_vars[key] = {
            'value': os.getenv(key),
            'set': key in os.environ,
            'source': 'environment'
        }

    # Check config file sources
    default_env_file = Path('/etc/novacore/novatrade.env')
    override_env_file = Path('configs/novatrade.override.env')

    env_files = {
        'default_config': {
            'path': str(default_env_file),
            'exists': default_env_file.exists(),
            'readable': default_env_file.is_file() if default_env_file.exists() else False
        },
        'override_config': {
            'path': str(override_env_file),
            'exists': override_env_file.exists(),
            'readable': override_env_file.is_file() if override_env_file.exists() else False
        }
    }

    return {
        'environment_variables': env_vars,
        'config_files': env_files,
        'timestamp': time.time()
    }


def validate_config_loading() -> dict[str, Any]:
    """Validate NovaTradeCfg loading from environment."""
    try:
        config = NovaTradeCfg.load()
        asdict(config)

        # Focus on critical settings
        critical_settings = {
            'dry_run': config.dry_run,
            'provider': config.provider,
            'symbols': config.symbols,
            'metaapi_token_set': bool(config.metaapi.token),
            'metaapi_account_id_set': bool(config.metaapi.account_id),
            'max_positions': config.risk.max_positions,
            'max_daily_drawdown_pct': config.risk.max_daily_drawdown_pct,
            'ftmo_enabled': config.ftmo.enabled,
            'ftmo_account_size': config.ftmo.account_size
        }

        return {
            'loading_success': True,
            'config_instance': critical_settings,
            'full_config_available': True,
            'timestamp': time.time()
        }

    except Exception as e:
        return {
            'loading_success': False,
            'error': str(e),
            'config_instance': None,
            'timestamp': time.time()
        }


def validate_runtime_propagation() -> dict[str, Any]:
    """Check if configuration properly propagates to runtime behavior."""
    # This would need to query the running service
    # For now, return placeholder that can be expanded

    try:
        import requests

        # Try to get runtime status
        response = requests.get('http://localhost:8877/status', timeout=5)
        if response.status_code == 200:
            status = response.json()
            runtime_info = {
                'service_responsive': True,
                'feed_health': status.get('feed_health', {}),
                'pipeline_state': status.get('pipeline', {}),
                'last_activity': status.get('uptime', 0)
            }
        else:
            runtime_info = {
                'service_responsive': False,
                'status_code': response.status_code
            }

    except Exception as e:
        runtime_info = {
            'service_responsive': False,
            'error': str(e)
        }

    return {
        'runtime_check': runtime_info,
        'timestamp': time.time()
    }


def generate_configuration_report() -> dict[str, Any]:
    """Generate comprehensive configuration diagnostic report."""
    report = {
        'diagnostic_version': '1.0.0',
        'generated_at': time.time(),
        'environment_validation': validate_environment_loading(),
        'config_loading_validation': validate_config_loading(),
        'runtime_propagation_validation': validate_runtime_propagation()
    }

    # Add analysis section
    env_dry_run = os.getenv('NOVATRADE_DRY_RUN', 'true').lower()
    config_result = report['config_loading_validation']

    if config_result['loading_success']:
        config_dry_run = config_result['config_instance']['dry_run']

        report['discrepancy_analysis'] = {
            'env_dry_run_setting': env_dry_run,
            'config_dry_run_loaded': config_dry_run,
            'values_match': (env_dry_run == 'false') == (not config_dry_run),
            'propagation_expected': env_dry_run == 'false' and not config_dry_run,
            'issue_identified': (env_dry_run == 'false') and config_dry_run
        }
    else:
        report['discrepancy_analysis'] = {
            'config_loading_failed': True,
            'cannot_analyze': True
        }

    return report


def main():
    """Main diagnostic function - can be called from CLI or imported."""
    report = generate_configuration_report()

    print("=== NovaTrade Configuration Diagnostic ===")
    print(json.dumps(report, indent=2, default=str))

    # Summary analysis
    print("\n=== SUMMARY ===")

    report['environment_validation']
    config_check = report['config_loading_validation']
    runtime_check = report['runtime_propagation_validation']
    analysis = report.get('discrepancy_analysis', {})

    if config_check['loading_success']:
        print("✅ Config Loading: SUCCESS")
        dry_run_status = config_check['config_instance']['dry_run']
        print(f"   - dry_run setting: {dry_run_status}")
    else:
        print(f"❌ Config Loading: FAILED - {config_check.get('error', 'Unknown')}")

    if runtime_check['runtime_check']['service_responsive']:
        print("✅ Runtime Service: RESPONSIVE")
    else:
        print("❌ Runtime Service: NOT RESPONSIVE")

    if analysis.get('issue_identified'):
        print("⚠️  ISSUE: Environment sets dry_run=false but config loads dry_run=true")
        print("   - This explains the execution discrepancy in trade journal")
        print("   - Recommendation: Verify environment file loading in NovaTradeCfg.load()")
    elif analysis.get('values_match'):
        print("✅ Configuration Propagation: VALUES MATCH")

    return report


if __name__ == '__main__':
    main()

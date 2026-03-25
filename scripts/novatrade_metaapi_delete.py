#!/usr/bin/env python3
"""Delete a MetaApi cloud account for NovaTrade.

Removes the MetaApi cloud account (the proxy layer), NOT the actual MT5 broker account.
After deletion, use novatrade_metaapi_setup.py to create a new one.

Usage:
    # Delete using env file (reads METAAPI_TOKEN and METAAPI_ACCOUNT_ID):
    python3 scripts/novatrade_metaapi_delete.py

    # Delete with explicit args:
    python3 scripts/novatrade_metaapi_delete.py \
        --token YOUR_METAAPI_TOKEN \
        --account-id 4c121f03-836f-4fb1-8799-736e53699a66

    # Skip confirmation prompt:
    python3 scripts/novatrade_metaapi_delete.py --yes
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def load_env_file(env_path: str = "/etc/novacore/novatrade.env") -> dict[str, str]:
    """Load key=value pairs from an env file."""
    result: dict[str, str] = {}
    p = Path(env_path)
    if not p.exists():
        return result
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


async def delete_account(token: str, account_id: str) -> int:
    try:
        from metaapi_cloud_sdk import MetaApi
    except ImportError:
        print("ERROR: metaapi_cloud_sdk not installed")
        print("  pip install metaapi-cloud-sdk")
        return 1

    print("=" * 50)
    print("  MetaApi Account Deletion")
    print("=" * 50)
    print()
    print(f"  Account ID: {account_id}")
    print()

    # --- 1. Initialize SDK ---
    print("[1/4] Initializing MetaApi SDK...")
    api = MetaApi(token)
    print("  OK")

    # --- 2. Fetch account info ---
    print("[2/4] Fetching account details...")
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        print(f"  Name:   {account.name}")
        print(f"  State:  {account.state}")
        print(f"  Server: {getattr(account, 'server', 'unknown')}")
        print(f"  Login:  {getattr(account, 'login', 'unknown')}")
    except Exception as exc:
        print(f"  Could not fetch account: {exc}")
        print("  The account may already be deleted or the ID is wrong.")
        return 2

    # --- 3. Undeploy if deployed ---
    print("[3/4] Undeploying account...")
    try:
        if account.state in ("DEPLOYED", "DEPLOYING"):
            await account.undeploy()
            await account.wait_undeployed(timeout_in_seconds=120)
            print("  Undeployed")
        else:
            print(f"  Already undeployed (state: {account.state})")
    except Exception as exc:
        print(f"  Undeploy warning: {exc}")
        print("  Continuing with removal...")

    # --- 4. Remove account ---
    print("[4/4] Removing account from MetaApi cloud...")
    try:
        await account.remove()
        print("  DELETED successfully")
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 3

    print()
    print("=" * 50)
    print("  Account Deleted")
    print("=" * 50)
    print()
    print("  Next steps:")
    print("    1. Get your new FTMO MT5 credentials (login, investor password, server)")
    print("    2. Run the setup script to create a new MetaApi account:")
    print()
    print("       python3 scripts/novatrade_metaapi_setup.py \\")
    print("           --token YOUR_METAAPI_TOKEN \\")
    print("           --login NEW_MT5_LOGIN \\")
    print("           --password 'NEW_INVESTOR_PASSWORD' \\")
    print("           --server 'FTMO-Demo' \\")
    print("           --campaign 'ftmo-free-trial-march-2026' \\")
    print("           --env-out /etc/novacore/novatrade.env")
    print()

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete a MetaApi cloud account",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="MetaApi API token (default: from METAAPI_TOKEN env var or env file)",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help="MetaApi account ID to delete (default: from METAAPI_ACCOUNT_ID env var or env file)",
    )
    parser.add_argument(
        "--env-file",
        default="/etc/novacore/novatrade.env",
        help="Path to env file for defaults (default: /etc/novacore/novatrade.env)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )

    args = parser.parse_args()

    # Resolve token and account_id from args > env vars > env file
    env_vars = load_env_file(args.env_file)

    token = args.token or os.environ.get("METAAPI_TOKEN") or env_vars.get("METAAPI_TOKEN")
    account_id = args.account_id or os.environ.get("METAAPI_ACCOUNT_ID") or env_vars.get("METAAPI_ACCOUNT_ID")

    if not token:
        print("ERROR: No MetaApi token. Use --token or set METAAPI_TOKEN")
        sys.exit(1)
    if not account_id:
        print("ERROR: No account ID. Use --account-id or set METAAPI_ACCOUNT_ID")
        sys.exit(1)

    if not args.yes:
        print(f"About to DELETE MetaApi account: {account_id}")
        print("This removes the cloud proxy only — your MT5 broker account is unaffected.")
        confirm = input("Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(0)

    exit_code = asyncio.get_event_loop().run_until_complete(delete_account(token, account_id))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

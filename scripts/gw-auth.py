#!/usr/bin/env python3
"""One-time Google Workspace OAuth setup.

Usage:
    1. Create a Google Cloud project at https://console.cloud.google.com
    2. Enable APIs: Gmail, Calendar, Drive, Docs, Sheets
    3. Create OAuth 2.0 Desktop credentials
    4. Download client_secret.json to ~/.config/nova-core/google/client_secret.json
    5. Run: python3 scripts/gw-auth.py

    For headless VPS, use SSH tunnel:
        ssh -L 8080:localhost:8080 nova@your-vps
        python3 scripts/gw-auth.py
        # Open the URL in your local browser, complete consent
"""

import os
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

CONFIG_DIR = Path.home() / ".config" / "nova-core" / "google"
CLIENT_SECRET = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "token.json"


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not CLIENT_SECRET.exists():
        print(f"ERROR: Place your Google OAuth client_secret.json at:\n  {CLIENT_SECRET}")
        print("\nTo get this file:")
        print("  1. Go to https://console.cloud.google.com")
        print("  2. Create/select a project")
        print("  3. Enable APIs: Gmail, Calendar, Drive, Docs, Sheets")
        print("  4. Go to Credentials > Create OAuth Client ID > Desktop app")
        print("  5. Download the JSON and save it to the path above")
        sys.exit(1)

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: Missing dependencies. Run:")
        print("  pip install google-api-python-client google-auth-oauthlib")
        sys.exit(1)

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if creds and creds.valid:
        print(f"Already authenticated. Token at: {TOKEN_FILE}")
        print(f"Scopes: {creds.scopes}")
        return

    if creds and creds.expired and creds.refresh_token:
        print("Refreshing expired token...")
        creds.refresh(Request())
    else:
        import socketserver

        socketserver.TCPServer.allow_reuse_address = True

        port = int(os.environ.get("GW_AUTH_PORT", "9090"))
        print(f"Starting OAuth flow on port {port}...")
        print(f"SSH tunnel: ssh -L {port}:localhost:{port} nova@your-vps")
        print()
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=port, open_browser=False)

    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    print(f"\nAuthentication successful! Token saved to: {TOKEN_FILE}")
    print("This token will auto-refresh. You shouldn't need to re-authenticate unless you revoke access.")


if __name__ == "__main__":
    main()

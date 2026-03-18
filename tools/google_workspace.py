#!/usr/bin/env python3
"""NovaCore Google Workspace CLI.

A unified CLI for Gmail, Calendar, Drive, and Docs.
Designed to be called by NovaCore skills and the watcher pipeline.

Usage:
    python3 tools/google_workspace.py gmail search "from:boss subject:urgent"
    python3 tools/google_workspace.py gmail read <message_id>
    python3 tools/google_workspace.py gmail send --to user@example.com --subject "Hi" --body "Hello"
    python3 tools/google_workspace.py calendar list [--days 7]
    python3 tools/google_workspace.py calendar create --title "Meeting" --start "..." --end "..."
    python3 tools/google_workspace.py drive search "quarterly report"
    python3 tools/google_workspace.py drive download <file_id> <output_path>
    python3 tools/google_workspace.py docs read <document_id>
    python3 tools/google_workspace.py docs create --title "New Doc" --body "Content here"
    python3 tools/google_workspace.py sheets read <spreadsheet_id> [--range "A1:D10"]
    python3 tools/google_workspace.py auth status
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "nova-core" / "google"
TOKEN_FILE = CONFIG_DIR / "token.json"

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


def _get_creds():
    """Load and refresh Google OAuth credentials."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        print("ERROR: Not authenticated. Run: python3 scripts/gw-auth.py", file=sys.stderr)
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        TOKEN_FILE.chmod(0o600)
    elif not creds.valid:
        print("ERROR: Token invalid. Re-run: python3 scripts/gw-auth.py", file=sys.stderr)
        sys.exit(1)

    return creds


def _build_service(api, version):
    """Build a Google API service client."""
    from googleapiclient.discovery import build

    return build(api, version, credentials=_get_creds(), cache_discovery=False)


def _json_out(data):
    """Print JSON output for skill consumption."""
    print(json.dumps(data, indent=2, default=str))


# ── Gmail ────────────────────────────────────────────────────────────────


def gmail_search(args):
    svc = _build_service("gmail", "v1")
    results = svc.users().messages().list(userId="me", q=args.query, maxResults=args.max_results).execute()

    messages = results.get("messages", [])
    if not messages:
        _json_out({"count": 0, "messages": []})
        return

    out = []
    for msg_stub in messages:
        msg = (
            svc.users()
            .messages()
            .get(userId="me", id=msg_stub["id"], format="metadata", metadataHeaders=["From", "To", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        out.append(
            {
                "id": msg["id"],
                "thread_id": msg["threadId"],
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
                "labels": msg.get("labelIds", []),
            }
        )

    _json_out({"count": len(out), "messages": out})


def gmail_read(args):
    svc = _build_service("gmail", "v1")
    msg = svc.users().messages().get(userId="me", id=args.message_id, format="full").execute()

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    # Extract body text
    body = ""
    payload = msg.get("payload", {})
    if payload.get("body", {}).get("data"):
        import base64

        body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    elif payload.get("parts"):
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                import base64

                body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                break

    _json_out(
        {
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "body": body[:10000],  # cap at 10k chars
            "labels": msg.get("labelIds", []),
        }
    )


def gmail_send(args):
    import base64
    from email.mime.text import MIMEText

    svc = _build_service("gmail", "v1")

    message = MIMEText(args.body)
    message["to"] = args.to
    message["subject"] = args.subject
    if args.cc:
        message["cc"] = args.cc

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    result = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    _json_out({"status": "sent", "id": result["id"], "thread_id": result["threadId"]})


def gmail_labels(args):
    svc = _build_service("gmail", "v1")
    results = svc.users().labels().list(userId="me").execute()
    labels = [{"id": lbl["id"], "name": lbl["name"], "type": lbl["type"]} for lbl in results.get("labels", [])]
    _json_out({"count": len(labels), "labels": labels})


def gmail_threads(args):
    svc = _build_service("gmail", "v1")
    results = svc.users().threads().list(userId="me", q=args.query, maxResults=args.max_results).execute()

    threads = results.get("threads", [])
    if not threads:
        _json_out({"count": 0, "threads": []})
        return

    out = []
    for t in threads:
        thread = (
            svc.users()
            .threads()
            .get(userId="me", id=t["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        msgs = thread.get("messages", [])
        first_headers = {h["name"]: h["value"] for h in msgs[0].get("payload", {}).get("headers", [])} if msgs else {}
        out.append(
            {
                "id": t["id"],
                "message_count": len(msgs),
                "subject": first_headers.get("Subject", ""),
                "from": first_headers.get("From", ""),
                "snippet": t.get("snippet", ""),
            }
        )

    _json_out({"count": len(out), "threads": out})


# ── Calendar ─────────────────────────────────────────────────────────────


def calendar_list(args):
    svc = _build_service("calendar", "v3")
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=args.days)

    events_result = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            maxResults=args.max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    events = events_result.get("items", [])
    out = []
    for e in events:
        start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", ""))
        end_time = e.get("end", {}).get("dateTime", e.get("end", {}).get("date", ""))
        out.append(
            {
                "id": e["id"],
                "summary": e.get("summary", "(no title)"),
                "start": start,
                "end": end_time,
                "location": e.get("location", ""),
                "description": (e.get("description") or "")[:500],
                "status": e.get("status", ""),
                "attendees": [a.get("email") for a in e.get("attendees", [])],
            }
        )

    _json_out({"count": len(out), "days": args.days, "events": out})


def calendar_create(args):
    svc = _build_service("calendar", "v3")

    event = {
        "summary": args.title,
        "start": {"dateTime": args.start, "timeZone": args.timezone},
        "end": {"dateTime": args.end, "timeZone": args.timezone},
    }
    if args.description:
        event["description"] = args.description
    if args.location:
        event["location"] = args.location
    if args.attendees:
        event["attendees"] = [{"email": e.strip()} for e in args.attendees.split(",")]

    result = svc.events().insert(calendarId="primary", body=event).execute()
    _json_out(
        {
            "status": "created",
            "id": result["id"],
            "link": result.get("htmlLink", ""),
            "summary": result.get("summary", ""),
            "start": result.get("start", {}),
        }
    )


def calendar_delete(args):
    svc = _build_service("calendar", "v3")
    svc.events().delete(calendarId="primary", eventId=args.event_id).execute()
    _json_out({"status": "deleted", "event_id": args.event_id})


def calendar_update(args):
    svc = _build_service("calendar", "v3")
    event = svc.events().get(calendarId="primary", eventId=args.event_id).execute()

    if args.title:
        event["summary"] = args.title
    if args.start:
        event["start"] = {"dateTime": args.start, "timeZone": args.timezone}
    if args.end:
        event["end"] = {"dateTime": args.end, "timeZone": args.timezone}
    if args.description:
        event["description"] = args.description
    if args.location:
        event["location"] = args.location

    result = svc.events().update(calendarId="primary", eventId=args.event_id, body=event).execute()
    _json_out({"status": "updated", "id": result["id"], "summary": result.get("summary", "")})


# ── Drive ────────────────────────────────────────────────────────────────


def drive_search(args):
    svc = _build_service("drive", "v3")

    query = f"name contains '{args.query}' and trashed = false"
    if args.mime_type:
        query += f" and mimeType = '{args.mime_type}'"

    results = (
        svc.files()
        .list(
            q=query,
            pageSize=args.max_results,
            fields="files(id, name, mimeType, modifiedTime, size, parents, webViewLink)",
            orderBy="modifiedTime desc",
        )
        .execute()
    )

    files = results.get("files", [])
    _json_out({"count": len(files), "files": files})


def drive_list(args):
    svc = _build_service("drive", "v3")

    query = "trashed = false"
    if args.folder_id:
        query += f" and '{args.folder_id}' in parents"

    results = (
        svc.files()
        .list(
            q=query,
            pageSize=args.max_results,
            fields="files(id, name, mimeType, modifiedTime, size, webViewLink)",
            orderBy="modifiedTime desc",
        )
        .execute()
    )

    files = results.get("files", [])
    _json_out({"count": len(files), "files": files})


def drive_download(args):
    from googleapiclient.http import MediaIoBaseDownload

    svc = _build_service("drive", "v3")

    # Check if it's a Google Doc type (needs export)
    file_meta = svc.files().get(fileId=args.file_id, fields="mimeType,name").execute()
    mime = file_meta.get("mimeType", "")

    export_map = {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
        "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if mime in export_map:
        export_mime, _ = export_map[mime]
        request = svc.files().export_media(fileId=args.file_id, mimeType=export_mime)
    else:
        request = svc.files().get_media(fileId=args.file_id)

    with open(output_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    _json_out(
        {
            "status": "downloaded",
            "file_id": args.file_id,
            "name": file_meta.get("name", ""),
            "output": str(output_path),
            "size_bytes": output_path.stat().st_size,
        }
    )


def drive_upload(args):
    from googleapiclient.http import MediaFileUpload

    svc = _build_service("drive", "v3")
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"ERROR: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    file_metadata = {"name": args.name or file_path.name}
    if args.folder_id:
        file_metadata["parents"] = [args.folder_id]

    media = MediaFileUpload(str(file_path), resumable=True)
    result = svc.files().create(body=file_metadata, media_body=media, fields="id,name,webViewLink").execute()
    _json_out(
        {
            "status": "uploaded",
            "id": result["id"],
            "name": result["name"],
            "link": result.get("webViewLink", ""),
        }
    )


# ── Docs ─────────────────────────────────────────────────────────────────


def docs_read(args):
    svc = _build_service("docs", "v1")
    doc = svc.documents().get(documentId=args.document_id).execute()

    # Extract plain text from document body
    text = ""
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if para:
            for el in para.get("elements", []):
                text_run = el.get("textRun")
                if text_run:
                    text += text_run.get("content", "")

    _json_out(
        {
            "id": doc["documentId"],
            "title": doc.get("title", ""),
            "text": text[:20000],  # cap at 20k chars
            "revision_id": doc.get("revisionId", ""),
        }
    )


def docs_create(args):
    svc = _build_service("docs", "v1")
    doc = svc.documents().create(body={"title": args.title}).execute()
    doc_id = doc["documentId"]

    if args.body:
        requests = [{"insertText": {"location": {"index": 1}, "text": args.body}}]
        svc.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()

    _json_out(
        {
            "status": "created",
            "id": doc_id,
            "title": doc.get("title", ""),
            "link": f"https://docs.google.com/document/d/{doc_id}/edit",
        }
    )


def docs_append(args):
    svc = _build_service("docs", "v1")
    doc = svc.documents().get(documentId=args.document_id).execute()

    # Find the end index
    content = doc.get("body", {}).get("content", [])
    end_index = 1
    if content:
        end_index = content[-1].get("endIndex", 1) - 1

    requests = [{"insertText": {"location": {"index": max(end_index, 1)}, "text": args.text}}]
    svc.documents().batchUpdate(documentId=args.document_id, body={"requests": requests}).execute()
    _json_out({"status": "appended", "document_id": args.document_id, "chars_added": len(args.text)})


# ── Sheets ───────────────────────────────────────────────────────────────


def sheets_read(args):
    svc = _build_service("sheets", "v4")
    range_name = args.range or "Sheet1"

    result = svc.spreadsheets().values().get(spreadsheetId=args.spreadsheet_id, range=range_name).execute()

    values = result.get("values", [])
    _json_out(
        {
            "spreadsheet_id": args.spreadsheet_id,
            "range": result.get("range", range_name),
            "rows": len(values),
            "values": values[:500],  # cap at 500 rows
        }
    )


def sheets_write(args):
    svc = _build_service("sheets", "v4")
    range_name = args.range or "Sheet1!A1"

    values = json.loads(args.values)
    body = {"values": values}

    result = (
        svc.spreadsheets()
        .values()
        .update(
            spreadsheetId=args.spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body=body,
        )
        .execute()
    )

    _json_out(
        {
            "status": "written",
            "spreadsheet_id": args.spreadsheet_id,
            "updated_range": result.get("updatedRange", ""),
            "updated_rows": result.get("updatedRows", 0),
            "updated_cells": result.get("updatedCells", 0),
        }
    )


def sheets_create(args):
    svc = _build_service("sheets", "v4")
    spreadsheet = {"properties": {"title": args.title}}
    result = svc.spreadsheets().create(body=spreadsheet).execute()
    _json_out(
        {
            "status": "created",
            "id": result["spreadsheetId"],
            "title": result["properties"]["title"],
            "link": result.get("spreadsheetUrl", ""),
        }
    )


# ── Auth ─────────────────────────────────────────────────────────────────


def auth_status(args):
    if not TOKEN_FILE.exists():
        _json_out({"authenticated": False, "error": "No token found. Run: python3 scripts/gw-auth.py"})
        return

    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    _json_out(
        {
            "authenticated": True,
            "valid": creds.valid,
            "expired": creds.expired,
            "token_file": str(TOKEN_FILE),
            "scopes": list(creds.scopes) if creds.scopes else SCOPES,
            "has_refresh_token": bool(creds.refresh_token),
        }
    )


# ── Argument Parser ──────────────────────────────────────────────────────


def build_parser():
    parser = argparse.ArgumentParser(
        prog="gw",
        description="NovaCore Google Workspace CLI",
    )
    sub = parser.add_subparsers(dest="service", required=True)

    # ── Gmail ──
    gmail = sub.add_parser("gmail", help="Gmail operations")
    gmail_sub = gmail.add_subparsers(dest="action", required=True)

    p = gmail_sub.add_parser("search", help="Search emails")
    p.add_argument("query", help="Gmail search query (e.g. 'from:boss subject:urgent')")
    p.add_argument("--max-results", type=int, default=10)
    p.set_defaults(func=gmail_search)

    p = gmail_sub.add_parser("read", help="Read a specific email")
    p.add_argument("message_id", help="Gmail message ID")
    p.set_defaults(func=gmail_read)

    p = gmail_sub.add_parser("send", help="Send an email")
    p.add_argument("--to", required=True, help="Recipient email")
    p.add_argument("--subject", required=True, help="Email subject")
    p.add_argument("--body", required=True, help="Email body text")
    p.add_argument("--cc", help="CC recipients (comma-separated)")
    p.set_defaults(func=gmail_send)

    p = gmail_sub.add_parser("labels", help="List Gmail labels")
    p.set_defaults(func=gmail_labels)

    p = gmail_sub.add_parser("threads", help="List email threads")
    p.add_argument("query", help="Gmail search query")
    p.add_argument("--max-results", type=int, default=10)
    p.set_defaults(func=gmail_threads)

    # ── Calendar ──
    cal = sub.add_parser("calendar", help="Google Calendar operations")
    cal_sub = cal.add_subparsers(dest="action", required=True)

    p = cal_sub.add_parser("list", help="List upcoming events")
    p.add_argument("--days", type=int, default=7, help="Days ahead to show")
    p.add_argument("--max-results", type=int, default=20)
    p.set_defaults(func=calendar_list)

    p = cal_sub.add_parser("create", help="Create a calendar event")
    p.add_argument("--title", required=True, help="Event title")
    p.add_argument("--start", required=True, help="Start time (ISO format)")
    p.add_argument("--end", required=True, help="End time (ISO format)")
    p.add_argument("--description", help="Event description")
    p.add_argument("--location", help="Event location")
    p.add_argument("--attendees", help="Comma-separated attendee emails")
    p.add_argument("--timezone", default="UTC", help="Timezone (default: UTC)")
    p.set_defaults(func=calendar_create)

    p = cal_sub.add_parser("delete", help="Delete an event")
    p.add_argument("event_id", help="Event ID to delete")
    p.set_defaults(func=calendar_delete)

    p = cal_sub.add_parser("update", help="Update an event")
    p.add_argument("event_id", help="Event ID to update")
    p.add_argument("--title", help="New title")
    p.add_argument("--start", help="New start time")
    p.add_argument("--end", help="New end time")
    p.add_argument("--description", help="New description")
    p.add_argument("--location", help="New location")
    p.add_argument("--timezone", default="UTC")
    p.set_defaults(func=calendar_update)

    # ── Drive ──
    drv = sub.add_parser("drive", help="Google Drive operations")
    drv_sub = drv.add_subparsers(dest="action", required=True)

    p = drv_sub.add_parser("search", help="Search files")
    p.add_argument("query", help="Search query")
    p.add_argument("--mime-type", help="Filter by MIME type")
    p.add_argument("--max-results", type=int, default=10)
    p.set_defaults(func=drive_search)

    p = drv_sub.add_parser("list", help="List files")
    p.add_argument("--folder-id", help="Folder ID to list")
    p.add_argument("--max-results", type=int, default=20)
    p.set_defaults(func=drive_list)

    p = drv_sub.add_parser("download", help="Download a file")
    p.add_argument("file_id", help="File ID to download")
    p.add_argument("output", help="Output file path")
    p.set_defaults(func=drive_download)

    p = drv_sub.add_parser("upload", help="Upload a file")
    p.add_argument("file", help="Local file path to upload")
    p.add_argument("--name", help="Name in Drive (default: filename)")
    p.add_argument("--folder-id", help="Target folder ID")
    p.set_defaults(func=drive_upload)

    # ── Docs ──
    docs = sub.add_parser("docs", help="Google Docs operations")
    docs_sub = docs.add_subparsers(dest="action", required=True)

    p = docs_sub.add_parser("read", help="Read a document")
    p.add_argument("document_id", help="Document ID")
    p.set_defaults(func=docs_read)

    p = docs_sub.add_parser("create", help="Create a document")
    p.add_argument("--title", required=True, help="Document title")
    p.add_argument("--body", help="Initial document text")
    p.set_defaults(func=docs_create)

    p = docs_sub.add_parser("append", help="Append text to a document")
    p.add_argument("document_id", help="Document ID")
    p.add_argument("--text", required=True, help="Text to append")
    p.set_defaults(func=docs_append)

    # ── Sheets ──
    sht = sub.add_parser("sheets", help="Google Sheets operations")
    sht_sub = sht.add_subparsers(dest="action", required=True)

    p = sht_sub.add_parser("read", help="Read spreadsheet data")
    p.add_argument("spreadsheet_id", help="Spreadsheet ID")
    p.add_argument("--range", help="Cell range (e.g. 'Sheet1!A1:D10')")
    p.set_defaults(func=sheets_read)

    p = sht_sub.add_parser("write", help="Write data to spreadsheet")
    p.add_argument("spreadsheet_id", help="Spreadsheet ID")
    p.add_argument("--range", help="Target range")
    p.add_argument("--values", required=True, help='JSON array of rows, e.g. \'[["a","b"],["c","d"]]\'')
    p.set_defaults(func=sheets_write)

    p = sht_sub.add_parser("create", help="Create a new spreadsheet")
    p.add_argument("--title", required=True, help="Spreadsheet title")
    p.set_defaults(func=sheets_create)

    # ── Auth ──
    auth = sub.add_parser("auth", help="Authentication status")
    auth_sub = auth.add_subparsers(dest="action", required=True)

    p = auth_sub.add_parser("status", help="Check auth status")
    p.set_defaults(func=auth_status)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        _json_out({"error": str(e), "type": type(e).__name__})
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

ROOT = Path("/home/nova/nova-core")
OUTPUT = ROOT / "OUTPUT"
LOGS = ROOT / "LOGS"
STATE = ROOT / "STATE"
INTENTS_DIR = STATE / "intents"
STATE.mkdir(parents=True, exist_ok=True)

VAULT_DIR = Path("/home/nova/nova-vault")
VAULT_DIARY = VAULT_DIR / "90-diary"

SENT_LOG = STATE / "tg_sent_outputs.txt"  # legacy; kept for backward compat reads
NOTIFIED_DIR = STATE / "notified"  # durable marker dir (one file per output)
CEO_DELEGATED_DIR = STATE / "ceo_delegated"  # Phase 9: CEO Nova delegation markers
MODE_FILE = STATE / "notifier_mode.txt"
MARKER_MAX_AGE_DAYS = 7
CEO_DEFER_SECONDS = 20  # how long to wait for CEO Nova before fallback

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "").strip()

TELEGRAM_MAX = 3500  # safe chunk size


def log(msg: str) -> None:
    print(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} [notifier] {msg}", flush=True)


def get_mode() -> str:
    try:
        m = MODE_FILE.read_text(encoding="utf-8").strip().lower()
        return m if m in {"compact", "normal", "verbose"} else "normal"
    except Exception:
        return "normal"


def send_text(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_ID env var.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    with httpx.Client(timeout=25) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()


def send_message_chunked(text: str) -> None:
    # Telegram messages have length limits; chunk safely.
    chunks: list[str] = []
    s = text.strip()
    while len(s) > TELEGRAM_MAX:
        cut = s.rfind("\n", 0, TELEGRAM_MAX)
        if cut < 500:
            cut = TELEGRAM_MAX
        chunks.append(s[:cut].rstrip())
        s = s[cut:].lstrip()
    if s:
        chunks.append(s)

    for _i, c in enumerate(chunks, start=1):
        send_text(c)
        time.sleep(0.25)


def _migrate_legacy_sent_log() -> None:
    """One-time: convert legacy tg_sent_outputs.txt entries into marker files."""
    if not SENT_LOG.exists():
        return
    for line in SENT_LOG.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name:
            marker = NOTIFIED_DIR / f"{name}.notified"
            if not marker.exists():
                try:
                    fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                    os.write(fd, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC (migrated)\n").encode())
                    os.close(fd)
                except FileExistsError:
                    pass
    # Rename legacy file so migration only runs once
    SENT_LOG.rename(SENT_LOG.with_suffix(".txt.migrated"))
    log("Migrated legacy tg_sent_outputs.txt → marker files")


def _cleanup_old_markers() -> None:
    """Delete marker files older than MARKER_MAX_AGE_DAYS."""
    cutoff = time.time() - (MARKER_MAX_AGE_DAYS * 86400)
    removed = 0
    for marker in NOTIFIED_DIR.glob("*.notified"):
        try:
            if marker.stat().st_mtime < cutoff:
                marker.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log(f"Cleaned up {removed} marker(s) older than {MARKER_MAX_AGE_DAYS}d")


def already_sent(name: str) -> bool:
    """Check if durable marker file exists."""
    return (NOTIFIED_DIR / f"{name}.notified").exists()


def claim_send(name: str) -> bool:
    """Atomically claim the right to send for this output file.

    Uses O_CREAT|O_EXCL which is atomic at the kernel level — if two
    processes race, exactly one gets the fd and the other gets EEXIST.
    Returns True if we won the claim, False if already claimed.
    """
    marker = NOTIFIED_DIR / f"{name}.notified"
    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        content = f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\npid={os.getpid()} host={platform.node()}\n"
        os.write(fd, content.encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def unclaim_send(name: str) -> None:
    """Remove marker on send failure so it can be retried."""
    marker = NOTIFIED_DIR / f"{name}.notified"
    try:
        marker.unlink()
    except OSError:
        pass


SUMMARY_MAX_CHARS = 300

# --- Heartbeat output detection & smart summarization ----------------------

_HB_PREFIX_RE = re.compile(r"^hb_(plan|research)_\d{8}-\d{6}\.md$")


def _is_heartbeat_output(name: str) -> bool:
    """Check if the output file is a heartbeat plan or research report."""
    return bool(_HB_PREFIX_RE.match(name))


def _extract_plan_version(md_text: str) -> str:
    """Extract version label like 'v50' from the title line."""
    m = re.search(r"Plan\s+(v\d+)", md_text)
    return m.group(1) if m else ""


def _extract_what_changed(md_text: str) -> list[str]:
    """Pull the numbered 'What changed since' items as short summaries."""
    # Find the section
    m = re.search(
        r"\*\*What changed since[^*]*\*\*[:\s]*\n(.*?)(?:\n\*\*Known Issues|\n---|\Z)",
        md_text,
        flags=re.DOTALL,
    )
    if not m:
        return []

    items = []
    for line in m.group(1).strip().splitlines():
        # Match numbered items like "1. **Title** — description..."
        im = re.match(r"\d+\.\s+\*\*([^*]+)\*\*\s*(?:—|--|-)\s*(.+)", line.strip())
        if im:
            title = im.group(1).strip()
            detail = im.group(2).strip()
            # Truncate detail to keep it readable
            if len(detail) > 120:
                detail = detail[:117].rstrip() + "..."
            items.append(f"  {title} — {detail}")
        elif line.strip().startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
            # Fallback: plain numbered items without bold
            clean = re.sub(r"^\d+\.\s*", "", line.strip())
            if len(clean) > 140:
                clean = clean[:137].rstrip() + "..."
            items.append(f"  {clean}")
    return items


def _extract_known_issues_count(md_text: str) -> int:
    """Count lines under **Known Issues:**."""
    m = re.search(
        r"\*\*Known Issues:\*\*\s*\n(.*?)(?:\n---|\n##|\Z)",
        md_text,
        flags=re.DOTALL,
    )
    if not m:
        return 0
    return sum(1 for line in m.group(1).strip().splitlines() if line.strip().startswith("-"))


def _extract_codebase_stats(md_text: str) -> str:
    """Extract codebase stats line."""
    m = re.search(r"\*\*Codebase:\*\*\s*([^\n]+)", md_text)
    if m:
        raw = m.group(1).strip()
        # Shorten to key numbers
        tests_m = re.search(r"([\d,]+\+?)\s*tests", raw)
        loc_m = re.search(r"~?([\d,]+K)\s*Python\s*LOC", raw)
        parts = []
        if loc_m:
            parts.append(f"{loc_m.group(1)} LOC")
        if tests_m:
            parts.append(f"{tests_m.group(1)} tests")
        return " | ".join(parts) if parts else ""
    return ""


def _extract_progress(md_text: str) -> str:
    """Extract progress line."""
    m = re.search(r"\*\*Progress:\s*([^\n*]+)", md_text)
    return m.group(1).strip() if m else ""


def _build_heartbeat_digest(output_path: Path, md_text: str) -> str:
    """Build a concise, readable digest for heartbeat plan/research outputs."""
    kind = "Plan" if "hb_plan_" in output_path.name else "Research"
    version = _extract_plan_version(md_text)
    changes = _extract_what_changed(md_text)
    issues_count = _extract_known_issues_count(md_text)
    stats = _extract_codebase_stats(md_text)
    progress = _extract_progress(md_text)

    # Extract timestamp from filename
    ts_m = re.search(r"(\d{8})-(\d{6})", output_path.name)
    time_str = ""
    if ts_m:
        try:
            dt = datetime.strptime(f"{ts_m.group(1)}{ts_m.group(2)}", "%Y%m%d%H%M%S")
            time_str = dt.strftime("%H:%M UTC")
        except ValueError:
            pass

    # Build the message
    header = f"\U0001f4cb {kind} Update"
    if version:
        header += f" ({version})"
    if time_str:
        header += f" — {time_str}"

    lines = [header, ""]

    if progress:
        lines.append(f"\U0001f3af {progress}")

    if stats:
        lines.append(f"\U0001f4e6 {stats}")

    if lines[-1]:  # add separator if we had stats
        lines.append("")

    if changes:
        lines.append(f"What's new ({len(changes)}):")
        lines.extend(changes[:6])
        if len(changes) > 6:
            lines.append(f"  ...and {len(changes) - 6} more")
    else:
        lines.append("No changes since last revision.")

    if issues_count > 0:
        lines.append("")
        lines.append(f"\u26a0\ufe0f {issues_count} known issue{'s' if issues_count != 1 else ''}")

    lines.append("")
    lines.append(f"\U0001f4c4 Full report: OUTPUT/{output_path.name}")

    return "\n".join(lines)


def _extract_section(md_text: str, heading: str) -> str | None:
    """Extract body text under a ## heading, stopping at next ## or #."""
    pat = rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?:\n^##?\s+|\Z)"
    m = re.search(pat, md_text, flags=re.MULTILINE | re.DOTALL)
    if m:
        body = m.group(1).strip()
        return body if body else None
    return None


def _first_bullet_or_sentence(text: str) -> str:
    """Return the first bullet line or first sentence from a block of text."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("-", "*", "•")):
            # Return bullet content without the marker
            return re.sub(r"^[-*•]\s*", "", stripped)
    # No bullet found — return first sentence (up to period, or whole first line)
    first_line = text.strip().splitlines()[0].strip()
    dot = first_line.find(". ")
    if dot > 0:
        return first_line[: dot + 1]
    return first_line


def _clean_summary(text: str) -> str:
    """Trim, collapse whitespace, and cap at SUMMARY_MAX_CHARS."""
    # Collapse multiple newlines into single newline
    text = re.sub(r"\n{2,}", "\n", text.strip())
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS].rstrip() + "..."
    return text


def _extract_summary(md_text: str) -> str:
    """Intelligent summary extraction with 4-tier fallback.

    Priority 1: ## Summary         — full section body
    Priority 2: ## Actions Taken   — first bullet or sentence only
    Priority 3: ## Instruction     — full section body
    Priority 4: First paragraph after top-level # header
    Final:      "(no summary available)"
    """
    # Priority 1: "## Summary"
    body = _extract_section(md_text, "Summary")
    if body:
        return _clean_summary(body)

    # Priority 2: "## Actions Taken" — just the first bullet/sentence
    body = _extract_section(md_text, "Actions Taken")
    if body:
        return _clean_summary(_first_bullet_or_sentence(body))

    # Priority 3: "## Instruction"
    body = _extract_section(md_text, "Instruction")
    if body:
        return _clean_summary(body)

    # Priority 4: First paragraph after top-level report header
    # Matches "# Task Report:", "# Output Report:", "# Output for:", "# Output:", "# Task 0004"
    m = re.search(
        r"^#\s+(?:Task|Output)[^\n]*\n(.*?)(?:\n^##?\s+|\Z)",
        md_text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if m:
        # Skip metadata lines like **Completed:**, **Task:**, - Processed:, blank lines
        lines = []
        for line in m.group(1).splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r"^(\*\*\w+[:\*]|[-•]\s*(Processed|Host):)", stripped):
                continue
            lines.append(stripped)
        if lines:
            return _clean_summary("\n".join(lines))

    # Priority 5 (emergency): first non-empty ## section body in the document
    # Catches legacy reports whose sections don't match any known heading
    m = re.search(
        r"^##\s+\w[^\n]*\n(.*?)(?:\n^##?\s+|\Z)",
        md_text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if m:
        body = m.group(1).strip()
        if body:
            return _clean_summary(_first_bullet_or_sentence(body))

    # Priority 6 (last resort): first non-header, non-metadata line in the file
    for line in md_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if re.match(r"^(\*\*\w+[:\*]|[-•]\s*(Processed|Host):)", stripped):
            continue
        return _clean_summary(stripped)

    return "(no summary available)"


def parse_task_report(md_text: str, output_name: str = "") -> dict:
    """Pull structured fields from the output report markdown.

    Task ID priority:
      1) **Task ID:** ... line
      2) # Task Report: <id> header
    Completed timestamp priority:
      1) **Completed:** ...
      2) **Timestamp:** ...
      3) Inferred from output filename suffix __YYYYMMDD-HHMMSS
    """

    def find_one(pat: str) -> str | None:
        m = re.search(pat, md_text, flags=re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else None

    # --- Task ID ---
    # Priority: **Task ID:** → **Task:** → header (Task Report / Output Report)
    task_id = find_one(r"\*\*Task ID:\*\*\s*([^\n]+)")
    if not task_id:
        task_id = find_one(r"\*\*Task:\*\*\s*([^\n]+)")
    if not task_id:
        task_id = find_one(r"^#\s+(?:Task|Output)\s+Report:\s*(.+)$")

    # --- Completed timestamp ---
    # Priority: **Completed:** → **Timestamp:** → filename suffix
    completed = find_one(r"\*\*Completed:\*\*\s*([^\n]+)")
    if not completed:
        completed = find_one(r"\*\*Timestamp:\*\*\s*([^\n]+)")
    if not completed and output_name:
        ts_match = re.search(r"__(\d{8}-\d{6})\.md$", output_name)
        if ts_match:
            raw = ts_match.group(1)
            try:
                dt = datetime.strptime(raw, "%Y%m%d-%H%M%S")
                completed = dt.strftime("%Y-%m-%d %H:%M:%S") + " UTC (inferred)"
            except ValueError:
                pass

    # Normalize ISO timestamps (e.g. 2026-03-01T20:15:20Z → 2026-03-01 20:15:20 UTC)
    if completed:
        iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})Z$", completed)
        if iso_match:
            completed = f"{iso_match.group(1)} {iso_match.group(2)} UTC"

    files: list[str] = []

    # --- Summary extraction with fallback chain ---
    # Priority 1: "## Summary" section
    # Priority 2: First bullet/sentence under "## Actions Taken"
    # Priority 3: Text under "## Instruction"
    # Priority 4: First paragraph after top-level report header
    summary = _extract_summary(md_text)

    # Files Created/Modified section: capture bullet list items like - `/path`
    m2 = re.search(r"^##\s+Files Created[^\n]*\n(.*?)(?:\n^##\s+|\Z)", md_text, flags=re.MULTILINE | re.DOTALL)
    if m2:
        block = m2.group(1)
        for line in block.splitlines():
            line = line.strip()
            mpath = re.match(r"^-+\s*`([^`]+)`", line)
            if mpath:
                files.append(mpath.group(1))

    return {
        "task_id": task_id,
        "completed": completed,
        "summary": summary,
        "files": files,
    }


def compute_metrics(output_path: Path) -> dict:
    # Latency metrics from filename timestamps: tg_YYYYMMDD-HHMMSS...__YYYYMMDD-HHMMSS.md
    name = output_path.name
    m = re.match(r"^tg_(\d{8}-\d{6})_.+?__(\d{8}-\d{6})\.md$", name)
    queued_ts = None
    done_ts = None
    if m:
        queued_ts = m.group(1)
        done_ts = m.group(2)
    size_bytes = output_path.stat().st_size if output_path.exists() else 0

    latency_sec = None
    try:
        if queued_ts and done_ts:
            q = datetime.strptime(queued_ts, "%Y%m%d-%H%M%S")
            d = datetime.strptime(done_ts, "%Y%m%d-%H%M%S")
            latency_sec = int((d - q).total_seconds())
    except Exception:
        latency_sec = None

    return {
        "queued_ts": queued_ts,
        "done_ts": done_ts,
        "latency_sec": latency_sec,
        "size_bytes": size_bytes,
    }


def worker_log_for_output(output_path: Path, task_id: str | None = None) -> Path | None:
    """Find the worker log for a given output file.

    Search order:
      a) LOGS/worker_<task_base>.log          (base = filename before "__")
      b) LOGS/worker_<task_id>.log            (if task_id parsed from report)
      c) newest glob match: LOGS/worker_*<task_base>*.log
      d) None if nothing found
    """
    name = output_path.name
    base = name.split("__", 1)[0]

    # (a) exact match on task base
    if base:
        candidate = LOGS / f"worker_{base}.log"
        if candidate.exists():
            return candidate

    # (b) exact match on parsed task_id
    if task_id:
        candidate = LOGS / f"worker_{task_id}.log"
        if candidate.exists():
            return candidate

    # (c) glob fallback — newest partial match
    if base:
        matches = sorted(
            LOGS.glob(f"worker_*{base}*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0]

    return None


def _load_intent(output_path: Path) -> str:
    """Load the intent (chat/task) for an output file. Default: task."""
    # Output filenames: {stem}__YYYYMMDD-HHMMSS.md
    # Intent files: STATE/intents/{stem}.intent
    name = output_path.stem  # e.g. "0010_foo__20260305-160249"
    # Strip the timestamp suffix to get the task stem
    m = re.match(r"^(.+?)__\d{8}-\d{6}$", name)
    stem = m.group(1) if m else name
    try:
        return (INTENTS_DIR / f"{stem}.intent").read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return "task"


def _build_chat_message(output_path: Path) -> str:
    """Build a clean, stripped chat-mode message — answer content only."""
    # Import here to avoid circular imports at module level
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location("telegram.format", str(ROOT / "telegram" / "format.py"))
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    txt = output_path.read_text(encoding="utf-8", errors="replace")
    return _mod.strip_report_sections(txt)


def build_message(output_path: Path) -> str:
    txt = output_path.read_text(encoding="utf-8", errors="replace")

    # Smart digest for heartbeat outputs — always concise regardless of mode
    if _is_heartbeat_output(output_path.name):
        return _build_heartbeat_digest(output_path, txt)

    info = parse_task_report(txt, output_name=output_path.name)
    metrics = compute_metrics(output_path)
    mode = get_mode()

    # Build a natural summary line
    summary = info.get("summary") or "(no summary available)"
    task_id = info.get("task_id") or ""
    task_label = f"Task {task_id}" if task_id else output_path.name

    # Latency in human-readable form
    lat = metrics["latency_sec"]
    if isinstance(lat, int):
        if lat < 60:
            lat_str = f"{lat}s"
        elif lat < 3600:
            lat_str = f"{lat // 60}m {lat % 60}s"
        else:
            lat_str = f"{lat // 3600}h {(lat % 3600) // 60}m"
    else:
        lat_str = None

    if mode == "compact":
        line = f"{task_label} finished."
        if lat_str:
            line += f" Took {lat_str}."
        return line

    if mode == "normal":
        lines = [f"{task_label} finished."]
        if lat_str:
            lines[0] += f" Took {lat_str}."
        lines.append("")
        lines.append(summary)
        preview = "\n".join(txt.splitlines()[:30])
        if preview.strip():
            lines.append("")
            lines.append(preview)
        return "\n".join(lines)

    # verbose
    completed = info.get("completed") or ""
    lines = [f"{task_label} finished."]
    if lat_str:
        lines[0] += f" Took {lat_str}."
    if completed:
        lines.append(f"Completed: {completed}")
    lines.append("")
    lines.append(summary)
    lines.append("")
    lines.append(txt.strip())
    return "\n".join(lines)


def _is_ceo_delegated(output_path: Path) -> bool:
    """Check if this task was delegated through CEO Nova."""
    name = output_path.stem  # e.g. "0010_foo__20260305-160249"
    m = re.match(r"^(.+?)__\d{8}-\d{6}$", name)
    stem = m.group(1) if m else name
    return (CEO_DELEGATED_DIR / f"{stem}.delegated").exists()


VAULT_MAX_BYTES = 34_000  # vault_write enforces 34KB cap
NOTEBOOKLM_BIN = "/home/nova/.local/bin/notebooklm"


# ---------------------------------------------------------------------------
# NotebookLM summarization
# ---------------------------------------------------------------------------


def _nlm(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a notebooklm CLI command."""
    return subprocess.run(
        [NOTEBOOKLM_BIN, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _nlm_json(args: list[str], timeout: int = 120) -> dict | None:
    """Run a notebooklm CLI command that returns JSON, parse it."""
    r = _nlm(args, timeout=timeout)
    if r.returncode != 0:
        log(f"NLM {args[0]} failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        log(f"NLM {args[0]}: bad JSON: {r.stdout[:200]}")
        return None


def summarize_with_notebooklm(file_path: Path, task_id: str) -> str | None:
    """Create a temp NotebookLM notebook, add output as source, get summary, cleanup.

    Returns the NLM-generated summary string, or None on failure.
    """
    notebook_id: str | None = None
    try:
        # 1. Create notebook
        data = _nlm_json(["create", f"Summary: {task_id}", "--json"])
        if not data:
            return None
        # id is nested: {"notebook": {"id": "..."}}
        notebook_id = (data.get("notebook") or data).get("id")
        if not notebook_id:
            log(f"NLM create: no id in response: {json.dumps(data)[:200]}")
            return None
        log(f"NLM notebook created: {notebook_id[:8]}…")

        # 2. Add output file as source
        src = _nlm_json(
            ["source", "add", str(file_path), "--notebook", notebook_id, "--json"],
            timeout=60,
        )
        # id is nested: {"source": {"id": "..."}}
        src_inner = (src or {}).get("source") or src or {}
        source_id = src_inner.get("id") or src_inner.get("source_id")

        # 3. Wait for source indexing
        if source_id:
            wr = _nlm(
                ["source", "wait", source_id, "-n", notebook_id, "--timeout", "300"],
                timeout=330,
            )
            if wr.returncode != 0:
                log(f"NLM source wait rc={wr.returncode}, proceeding anyway")
        else:
            # Fallback: blind wait
            log("NLM: no source_id, blind-waiting 45s")
            time.sleep(45)

        # 4. Ask for executive summary
        ask = _nlm_json(
            [
                "ask",
                (
                    "Provide a concise executive summary of this completed task. "
                    "Cover: what was accomplished, key results or changes, "
                    "any issues or risks noted, and next steps if mentioned. "
                    "Keep it under 500 words. Use markdown formatting."
                ),
                "--notebook",
                notebook_id,
                "--json",
            ],
            timeout=120,
        )
        if not ask:
            return None
        answer = ask.get("answer", "").strip()
        log(f"NLM summary received ({len(answer)} chars)")
        return answer or None

    except subprocess.TimeoutExpired:
        log("NLM summarization timed out")
        return None
    except OSError as e:
        log(f"NLM OS error: {e}")
        return None
    finally:
        # 5. Cleanup — delete temp notebook
        if notebook_id:
            try:
                _nlm(["delete", "-n", notebook_id, "-y"], timeout=30)
                log(f"NLM notebook {notebook_id[:8]}… deleted")
            except Exception:
                log(f"NLM cleanup failed for {notebook_id[:8]}…")


# ---------------------------------------------------------------------------
# Vault diary writer
# ---------------------------------------------------------------------------


def write_to_vault_diary(output_path: Path) -> None:
    """Summarize completed output via NotebookLM, then write diary to 90-diary/."""
    VAULT_DIARY.mkdir(parents=True, exist_ok=True)

    raw = output_path.read_text(encoding="utf-8", errors="replace")
    info = parse_task_report(raw, output_name=output_path.name)
    task_id = info.get("task_id") or output_path.stem.split("__")[0]
    fallback_summary = info.get("summary") or ""
    completed = info.get("completed") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    now = datetime.utcnow()
    date_str = now.strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", output_path.stem)[:60].rstrip("-")
    vault_filename = f"completed-{date_str}-{slug}.md"
    vault_path = VAULT_DIARY / vault_filename

    if vault_path.exists():
        log(f"Vault diary already exists: {vault_filename} — skipping")
        return

    # Try NotebookLM summarization
    nlm_summary = summarize_with_notebooklm(output_path, task_id)

    if nlm_summary:
        note = f"""---
type: diary
title: "Completed: {task_id}"
date: "{date_str}"
source: nova-core-notifier
summarized_by: notebooklm
tags:
  - "#type/diary"
  - "#project/novacore"
---

## Task Completed

**Task:** {task_id}
**Completed:** {completed}
**Source:** `OUTPUT/{output_path.name}`

## Summary (NotebookLM)

{nlm_summary}
"""
        log(f"Diary uses NLM summary for {task_id}")
    else:
        # Fallback: extracted summary + truncated raw output
        log(f"NLM unavailable — falling back to raw summary for {task_id}")
        full_output = raw.strip()
        overhead = 600 + len(fallback_summary)
        budget = VAULT_MAX_BYTES - overhead
        if len(full_output.encode("utf-8")) > budget:
            full_output = full_output[: budget - 40] + "\n\n…(truncated — see OUTPUT/ for full)"

        note = f"""---
type: diary
title: "Completed: {task_id}"
date: "{date_str}"
source: nova-core-notifier
tags:
  - "#type/diary"
  - "#project/novacore"
---

## Task Completed

**Task:** {task_id}
**Completed:** {completed}
**Source:** `OUTPUT/{output_path.name}`

## Summary

{fallback_summary}

## Full Output

{full_output}
"""

    vault_path.write_text(note, encoding="utf-8")
    log(f"Wrote vault diary: {vault_filename} ({len(note)} bytes)")


# Only skip heartbeat reports (system chatter) — NOT autonomous task outputs
_SKIP_PREFIXES = ("hb_plan_", "heartbeat_report_")

# Intermediate outputs: retries, research sub-steps, synthesis sub-steps
_SKIP_SUBSTRINGS = ("__retry", "_research_", "_synthesize_")


def _is_autonomous_task_output(output_path: Path) -> bool:
    """Check if this output came from an autonomy-generated task."""
    name = output_path.stem
    m = re.match(r"^(.+?)__\d{8}-\d{6}$", name)
    stem = m.group(1) if m else name
    # Check the original task file for autonomy source marker
    tasks_dir = ROOT / "TASKS"
    for suffix in (".md.done", ".md.inprogress", ".md"):
        task_path = tasks_dir / f"{stem}{suffix}"
        if task_path.exists():
            try:
                head = task_path.read_text(encoding="utf-8")[:500]
                return "source: autonomy-decision-engine" in head
            except OSError:
                pass
    return False


def _build_autonomous_alert(output_path: Path) -> str:
    """Build a Telegram alert for an autonomously-created and completed task."""
    txt = output_path.read_text(encoding="utf-8", errors="replace")
    info = parse_task_report(txt, output_name=output_path.name)
    summary = info.get("summary") or "(no summary)"
    task_id = info.get("task_id") or output_path.name

    # Extract category and dimension from task frontmatter
    name = output_path.stem
    m = re.match(r"^(.+?)__\d{8}-\d{6}$", name)
    stem = m.group(1) if m else name
    category = "unknown"
    dimension = ""
    tasks_dir = ROOT / "TASKS"
    for suffix in (".md.done", ".md.inprogress", ".md"):
        task_path = tasks_dir / f"{stem}{suffix}"
        if task_path.exists():
            try:
                head = task_path.read_text(encoding="utf-8")[:500]
                cat_m = re.search(r"category:\s*(\S+)", head)
                dim_m = re.search(r"target_dimension:\s*(\S+)", head)
                if cat_m:
                    category = cat_m.group(1)
                if dim_m and dim_m.group(1) != "none":
                    dimension = dim_m.group(1)
            except OSError:
                pass
            break

    dim_label = f" ({dimension})" if dimension else ""
    lines = [
        f"[AUTONOMOUS] Nova acted on its own — {category}{dim_label}",
        f"Task: {task_id}",
        "",
        summary,
    ]
    # Include first 20 lines of output for context
    preview = "\n".join(txt.splitlines()[:20])
    if preview.strip():
        lines.append("")
        lines.append(preview)
    return "\n".join(lines)


def maybe_notify(path: Path) -> None:
    # Notify for all .md output files (numbered tasks + legacy tg_ tasks)
    if path.suffix.lower() != ".md":
        return

    # Skip internal system reports — already saved to vault by the report script
    if any(path.name.startswith(pfx) for pfx in _SKIP_PREFIXES):
        log(f"Skipped diary noise (prefix): {path.name}")
        return

    # Skip intermediate outputs (retries, research/synthesis sub-steps)
    if any(sub in path.name for sub in _SKIP_SUBSTRINGS):
        log(f"Skipped diary noise (substring): {path.name}")
        return

    # Fast pre-check before expensive work
    if already_sent(path.name):
        return

    # Phase 9: defer for CEO-delegated tasks — give CEO Nova priority
    if _is_ceo_delegated(path):
        log(f"CEO-delegated task {path.name} — deferring {CEO_DEFER_SECONDS}s for CEO Nova")
        time.sleep(CEO_DEFER_SECONDS)
        if already_sent(path.name):
            log(f"CEO Nova claimed {path.name} — skipping")
            return
        log(f"CEO Nova didn't claim {path.name} after {CEO_DEFER_SECONDS}s — fallback notify")

    # Atomic claim — O_CREAT|O_EXCL guarantees exactly one winner,
    # even across multiple processes or threads.
    if not claim_send(path.name):
        log(f"Skipped duplicate for {path.name} (marker already claimed)")
        return

    # debounce for writer/rename
    time.sleep(0.6)

    try:
        intent = _load_intent(path)
        mode = get_mode()
        is_autonomous = _is_autonomous_task_output(path)

        # Always send to Telegram — verbose mode additionally writes to vault
        if mode == "verbose" and intent != "chat":
            try:
                write_to_vault_diary(path)
                log(f"Wrote vault diary for {path.name} (mode=verbose)")
            except Exception as vault_err:
                log(f"Vault diary write failed for {path.name}: {vault_err}")

        # Build the Telegram message
        if is_autonomous:
            msg = _build_autonomous_alert(path)
        elif intent == "chat":
            msg = _build_chat_message(path)
        else:
            msg = build_message(path)

        send_message_chunked(msg)
        log(
            f"Sent Telegram for {path.name} "
            f"(intent={intent}, mode={mode}, autonomous={is_autonomous}, pid={os.getpid()})"
        )
    except Exception as e:
        # Send/write failed — remove marker so next attempt can retry
        unclaim_send(path.name)
        log(f"Send FAILED for {path.name}, marker removed for retry: {e}")
        raise


def catch_up_latest() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    candidates = sorted(OUTPUT.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates[:8]:
        if not already_sent(p.name):
            log(f"Catch-up: sending latest unsent output {p.name}")
            maybe_notify(p)
            break
    else:
        log("Catch-up: nothing new to send")


class Handler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        log(f"FS created: {p.name}")
        try:
            maybe_notify(p)
        except Exception as e:
            log(f"ERROR on_created {p.name}: {e}")

    def on_moved(self, event):
        dest_path = getattr(event, "dest_path", None)
        if not dest_path:
            return
        p = Path(dest_path)
        log(f"FS moved: {p.name}")
        try:
            maybe_notify(p)
        except Exception as e:
            log(f"ERROR on_moved {p.name}: {e}")


def main() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or ALLOWED_CHAT_ID env var.")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    NOTIFIED_DIR.mkdir(parents=True, exist_ok=True)

    # Migrate legacy flat-file dedup → marker files (one-time)
    _migrate_legacy_sent_log()
    # Purge stale markers older than 7 days
    _cleanup_old_markers()

    # Startup ping includes mode + routing info
    mode = get_mode()
    route = "→ nova-vault diary" if mode == "verbose" else "→ Telegram"
    try:
        send_text(f"Nova notifier is up, running in {mode} mode ({route}).")
        log(f"Startup ping sent — mode={mode} route={route}")
    except Exception as e:
        log(f"Startup ping failed: {e}")

    try:
        catch_up_latest()
    except Exception as e:
        log(f"Catch-up failed: {e}")

    obs = Observer()
    obs.schedule(Handler(), str(OUTPUT), recursive=False)
    obs.start()
    log("Watching OUTPUT/ for *.md files")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        obs.stop()
        obs.join()


if __name__ == "__main__":
    main()

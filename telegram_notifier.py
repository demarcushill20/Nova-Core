#!/usr/bin/env python3
from __future__ import annotations

import os
import platform
import re
import threading
import time
from pathlib import Path
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import httpx

ROOT = Path("/home/nova/nova-core")
OUTPUT = ROOT / "OUTPUT"
LOGS = ROOT / "LOGS"
STATE = ROOT / "STATE"
INTENTS_DIR = STATE / "intents"
STATE.mkdir(parents=True, exist_ok=True)

SENT_LOG = STATE / "tg_sent_outputs.txt"  # legacy; kept for backward compat reads
NOTIFIED_DIR = STATE / "notified"         # durable marker dir (one file per output)
MODE_FILE = STATE / "notifier_mode.txt"
MARKER_MAX_AGE_DAYS = 7

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

    for i, c in enumerate(chunks, start=1):
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
                    os.write(fd, datetime.utcnow().strftime(
                        "%Y-%m-%d %H:%M:%S UTC (migrated)\n").encode())
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
        content = (
            f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"pid={os.getpid()} host={platform.node()}\n"
        )
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
        md_text, flags=re.MULTILINE | re.DOTALL,
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
        md_text, flags=re.MULTILINE | re.DOTALL,
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
    _spec = _ilu.spec_from_file_location(
        "telegram.format", str(ROOT / "telegram" / "format.py"))
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    txt = output_path.read_text(encoding="utf-8", errors="replace")
    return _mod.strip_report_sections(txt)


def build_message(output_path: Path) -> str:
    txt = output_path.read_text(encoding="utf-8", errors="replace")
    info = parse_task_report(txt, output_name=output_path.name)
    metrics = compute_metrics(output_path)
    mode = get_mode()

    header = (
        f"✅ NovaCore completed ({mode.upper()})\n"
        f"Output: {output_path.name}\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
    )

    # Metrics line
    lat = metrics["latency_sec"]
    lat_str = f"{lat}s" if isinstance(lat, int) else "n/a"
    queued = metrics["queued_ts"] or "n/a"
    done = metrics["done_ts"] or "n/a"
    size = metrics["size_bytes"]
    metrics_block = (
        "\n📊 Metrics\n"
        f"- Queue timestamp (from filename): {queued} UTC\n"
        f"- Output timestamp (from filename): {done} UTC\n"
        f"- Queue→Output latency: {lat_str}\n"
        f"- Output size: {size} bytes\n"
    )

    # Key files
    files = info.get("files") or []
    key_files = []
    key_files.append(f"- OUTPUT: {output_path}")
    wlog = worker_log_for_output(output_path, task_id=info.get("task_id"))
    if wlog:
        key_files.append(f"- Worker log: {wlog} ({'exists' if wlog.exists() else 'missing'})")
    else:
        key_files.append("- Worker log: (not found)")
    for f in files:
        key_files.append(f"- {f}")

    key_files_block = "\n📁 Key Files\n" + "\n".join(key_files) + "\n"

    # Content behavior by mode
    if mode == "compact":
        return header + metrics_block + key_files_block

    if mode == "normal":
        preview = "\n".join(txt.splitlines()[:30])
        return header + metrics_block + key_files_block + "\n🧾 Preview\n" + preview

    # VERBOSE: include full Summary + Files Created + any other sections (still chunked)
    summary = info.get("summary") or "(no Summary section found)"
    task_id = info.get("task_id") or "(unknown)"
    completed = info.get("completed") or "(unknown)"

    verbose_top = (
        header
        + f"\n🧠 Report Fields\n- Task ID: {task_id}\n- Completed: {completed}\n"
        + metrics_block
        + key_files_block
        + "\n🧾 Full Summary\n"
        + summary.strip()
        + "\n"
    )

    # Append rest of the report (minus header lines) for “full summary + key files + metrics”
    # We’ll keep the entire report as well, chunked.
    verbose_full = verbose_top + "\n📄 Full Report\n" + txt.strip()
    return verbose_full

def maybe_notify(path: Path) -> None:
    # Notify for all .md output files (numbered tasks + legacy tg_ tasks)
    if path.suffix.lower() != ".md":
        return

    # Fast pre-check before expensive work
    if already_sent(path.name):
        return

    # Atomic claim — O_CREAT|O_EXCL guarantees exactly one winner,
    # even across multiple processes or threads.
    if not claim_send(path.name):
        log(f"Skipped duplicate for {path.name} (marker already claimed)")
        return

    # debounce for writer/rename
    time.sleep(0.6)

    try:
        intent = _load_intent(path)
        if intent == "chat":
            msg = _build_chat_message(path)
        else:
            msg = build_message(path)
            # Append source identity footer only in task/report mode
            footer = (
                f"\n---\nnotifier_pid={os.getpid()}"
                f" host={platform.node()}"
            )
            msg += footer
        send_message_chunked(msg)
        log(f"Sent notification for {path.name} (intent={intent}, mode={get_mode()}, pid={os.getpid()})")
    except Exception as e:
        # Send failed — remove marker so next attempt can retry
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

    # Startup ping includes mode
    try:
        send_text(f"🟢 NovaCore notifier is online (mode={get_mode()}).")
        log("Startup ping sent")
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

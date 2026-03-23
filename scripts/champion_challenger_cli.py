#!/usr/bin/env python3
"""Champion-Challenger CLI — compare backtest results or watch for new ones.

Usage:
    # Compare a challenger CSV against current champion
    python scripts/champion_challenger_cli.py compare data/irb_v4_tv_results.csv --label "v4.0.0"

    # Compare with explicit champion
    python scripts/champion_challenger_cli.py compare challenger.csv --champion champion.csv

    # Compare + save to vault + notify via Telegram
    python scripts/champion_challenger_cli.py compare data/challenger.csv --label "v5.0.0" --save-vault --notify

    # Show current champion
    python scripts/champion_challenger_cli.py status

    # Watch vault inbox for new backtest results
    python scripts/champion_challenger_cli.py watch
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import yaml

from novatrade.backtest.champion_challenger import (
    ComparisonResult,
    compare,
    format_markdown,
    format_report,
    format_telegram,
)

REGISTRY_PATH = PROJECT_ROOT / "configs" / "champion_registry.yaml"
VAULT_INBOX = Path("/home/nova/nova-vault/00-inbox")
DATA_DIR = PROJECT_ROOT / "data"
TV_CSV_HEADER = "Trade #,Type,Date and time,Signal,Price"


def load_registry() -> dict:
    with open(REGISTRY_PATH) as f:
        return yaml.safe_load(f)


def save_registry(reg: dict):
    with open(REGISTRY_PATH, "w") as f:
        yaml.dump(reg, f, default_flow_style=False, sort_keys=False)


def get_champion_csv() -> Path:
    reg = load_registry()
    csv_path = reg["current_champion"]["csv_path"]
    p = PROJECT_ROOT / csv_path
    if not p.exists():
        print(f"ERROR: Champion CSV not found: {p}")
        sys.exit(1)
    return p


def detect_version_label(filename: str) -> str:
    """Try to extract version label from filename."""
    # Match patterns like "v4", "v4.0.0", "v5", "IRB v4"
    match = re.search(r"v(\d+(?:\.\d+)*)", filename, re.IGNORECASE)
    if match:
        return f"v{match.group(1)}"
    return Path(filename).stem


def is_tv_csv(filepath: Path) -> bool:
    """Check if a file contains TradingView CSV export data."""
    try:
        with open(filepath) as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                if line.startswith(TV_CSV_HEADER):
                    return True
                if i > 5:
                    break
    except (OSError, UnicodeDecodeError):
        pass
    return False


def extract_csv_from_md(md_path: Path) -> Path:
    """Extract CSV content from a vault .md file into data/ dir."""
    lines = []
    with open(md_path) as f:
        in_csv = False
        for line in f:
            stripped = line.strip()
            if not in_csv:
                if stripped.startswith(TV_CSV_HEADER):
                    in_csv = True
                    lines.append(stripped)
                continue
            # Stop at YAML frontmatter or empty after data
            if stripped.startswith("---") and len(lines) > 1:
                break
            if stripped:
                lines.append(stripped)

    if not lines:
        return md_path  # Already a CSV or couldn't extract

    # Generate a stable filename from the source
    stem = md_path.stem.lower().replace(" ", "_")
    csv_name = f"irb_{stem}_extracted.csv"
    csv_path = DATA_DIR / csv_name
    csv_path.write_text("\n".join(lines) + "\n")
    return csv_path


def run_compare(
    challenger_path: str,
    champion_path: str | None = None,
    label: str | None = None,
    save_vault: bool = False,
    notify: bool = False,
    promote: bool = False,
) -> ComparisonResult:
    """Run comparison and handle outputs."""
    # Resolve champion
    if champion_path:
        champ_csv = Path(champion_path)
    else:
        champ_csv = get_champion_csv()

    chall_csv = Path(challenger_path)

    # Extract CSV from .md if needed
    if chall_csv.suffix == ".md" and is_tv_csv(chall_csv):
        chall_csv = extract_csv_from_md(chall_csv)
    if champ_csv.suffix == ".md" and is_tv_csv(champ_csv):
        champ_csv = extract_csv_from_md(champ_csv)

    # Labels
    reg = load_registry()
    champ_label = reg["current_champion"]["label"] if not champion_path else detect_version_label(str(champion_path))
    chall_label = label or detect_version_label(str(challenger_path))

    # Run comparison
    result = compare(champ_csv, chall_csv, champ_label, chall_label)

    # Print report
    report = format_report(result)
    print(report)

    # Save to vault
    if save_vault:
        vault_save(result)

    # Telegram notification
    if notify:
        telegram_notify(result)

    # Auto-promote if challenger wins and --promote flag set
    if promote and result.verdict and result.verdict.winner == "challenger":
        promote_challenger(result, chall_csv)

    return result


def vault_save(result: ComparisonResult):
    """Save markdown report to vault inbox."""
    md = format_markdown(result)
    cl = result.champion_label
    ll = result.challenger_label
    date = datetime.now().strftime("%Y-%m-%d")

    frontmatter = f"""---
type: inbox
created: {date}
tags:
  - "#novatrade"
  - "#irb"
  - "#backtest"
  - "#research"
  - "#type/inbox"
status: complete
---

"""
    filename = f"NovaTrade {ll} vs {cl} analysis.md"
    filepath = VAULT_INBOX / filename
    filepath.write_text(frontmatter + md)
    print(f"\n  Vault saved: {filepath}")


def telegram_notify(result: ComparisonResult):
    """Send Telegram notification with verdict."""
    try:
        from telegram_notifier import send_text
        msg = format_telegram(result)
        send_text(msg)
        print("  Telegram notification sent")
    except Exception as e:
        print(f"  Telegram notification failed: {e}")


def promote_challenger(result: ComparisonResult, challenger_csv: Path):
    """Promote challenger to champion in registry."""
    reg = load_registry()
    ls = result.challenger_stats
    ll = result.challenger_label

    # Archive current champion to history
    current = reg["current_champion"].copy()
    if "history" not in reg:
        reg["history"] = []
    reg["history"].append(current)

    # Copy challenger CSV to data/ with stable name
    stable_name = f"irb_{ll.lower().replace(' ', '_').replace('.', '')}_champion.csv"
    stable_path = DATA_DIR / stable_name
    if challenger_csv != stable_path:
        shutil.copy2(challenger_csv, stable_path)

    # Update champion
    reg["current_champion"] = {
        "version": ll,
        "label": ll,
        "csv_path": f"data/{stable_name}",
        "trades": ls["trades"],
        "consistency": round(ls["consistency"], 2),
        "net_pnl": round(ls["net_pnl"]),
        "promoted_date": datetime.now().strftime("%Y-%m-%d"),
        "notes": f"Promoted from {result.champion_label} via champion_challenger_cli",
    }

    save_registry(reg)
    print(f"\n  ✓ {ll} PROMOTED to champion!")
    print(f"  Registry updated: {REGISTRY_PATH}")


def cmd_compare(args):
    run_compare(
        challenger_path=args.challenger,
        champion_path=args.champion,
        label=args.label,
        save_vault=args.save_vault,
        notify=args.notify,
        promote=args.promote,
    )


def cmd_status(args):
    reg = load_registry()
    c = reg["current_champion"]
    print("  Current Champion:")
    print(f"    Version:     {c['label']}")
    print(f"    CSV:         {c['csv_path']}")
    print(f"    Trades:      {c['trades']}")
    print(f"    Consistency: {c['consistency']}")
    print(f"    Net P&L:     ${c['net_pnl']:,}")
    print(f"    Promoted:    {c['promoted_date']}")
    if c.get("notes"):
        print(f"    Notes:       {c['notes']}")

    history = reg.get("history", [])
    if history:
        print(f"\n  Champion History ({len(history)} entries):")
        for h in history:
            print(f"    {h['promoted_date']}: {h.get('label', h['version'])} (${h['net_pnl']:,}, C={h['consistency']})")


def cmd_watch(args):
    """Watch vault inbox for new backtest results and auto-compare."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        print("ERROR: watchdog not installed. Run: pip install watchdog")
        sys.exit(1)

    print(f"  Watching {VAULT_INBOX} for new backtest results...")
    print(f"  Champion: {load_registry()['current_champion']['label']}")
    print("  Press Ctrl+C to stop\n")

    seen: set[str] = set()

    class NewResultHandler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            path = Path(event.src_path)
            if path.suffix != ".md":
                return

            # Debounce — wait for file to stabilize
            time.sleep(2)

            # Check if it's a TV CSV export
            if not is_tv_csv(path):
                return

            # Deduplicate
            file_hash = hashlib.md5(path.read_bytes()).hexdigest()
            if file_hash in seen:
                return
            seen.add(file_hash)

            label = detect_version_label(path.name)
            print(f"\n  New backtest detected: {path.name} (label: {label})")

            try:
                run_compare(
                    challenger_path=str(path),
                    label=label,
                    save_vault=args.save_vault,
                    notify=args.notify,
                )
            except Exception as e:
                print(f"  ERROR comparing: {e}")

    observer = Observer()
    observer.schedule(NewResultHandler(), str(VAULT_INBOX), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\n  Watcher stopped.")
    observer.join()


def main():
    parser = argparse.ArgumentParser(
        description="NovaTrade Champion-Challenger Comparison",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # compare
    p_compare = sub.add_parser("compare", help="Compare challenger against champion")
    p_compare.add_argument("challenger", help="Path to challenger CSV or vault .md file")
    p_compare.add_argument("--champion", help="Override champion CSV path (default: from registry)")
    p_compare.add_argument("--label", help="Challenger version label (auto-detected if omitted)")
    p_compare.add_argument("--save-vault", action="store_true", help="Save markdown report to vault")
    p_compare.add_argument("--notify", action="store_true", help="Send Telegram notification")
    p_compare.add_argument("--promote", action="store_true", help="Auto-promote challenger if it wins")
    p_compare.set_defaults(func=cmd_compare)

    # status
    p_status = sub.add_parser("status", help="Show current champion")
    p_status.set_defaults(func=cmd_status)

    # watch
    p_watch = sub.add_parser("watch", help="Watch vault inbox for new results")
    p_watch.add_argument("--save-vault", action="store_true", help="Auto-save reports to vault")
    p_watch.add_argument("--notify", action="store_true", help="Auto-notify via Telegram")
    p_watch.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

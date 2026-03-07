"""Adapter: pdf.generate

Generate a PDF file from markdown or plain text using reportlab.
Writes to OUTPUT/ within the sandbox. Deterministic — no shell execution.
"""

import re
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer


def _resolve_repo_root() -> Path:
    """Resolve the repo root dynamically (same approach as runner.py)."""
    return Path(__file__).resolve().parent.parent.parent


def _markdown_to_paragraphs(content: str, styles) -> list:
    """Convert markdown/plain text to reportlab flowables.

    Handles:
    - # headings (H1, H2, H3)
    - **bold** and *italic* via reportlab XML markup
    - Bullet lists (- or * prefix)
    - Plain paragraphs
    """
    flowables = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip empty lines (add small spacer)
        if not stripped:
            flowables.append(Spacer(1, 4 * mm))
            i += 1
            continue

        # Headings
        if stripped.startswith("### "):
            text = _inline_markup(stripped[4:])
            flowables.append(Paragraph(text, styles["Heading3"]))
            flowables.append(Spacer(1, 2 * mm))
            i += 1
            continue
        if stripped.startswith("## "):
            text = _inline_markup(stripped[3:])
            flowables.append(Paragraph(text, styles["Heading2"]))
            flowables.append(Spacer(1, 3 * mm))
            i += 1
            continue
        if stripped.startswith("# "):
            text = _inline_markup(stripped[2:])
            flowables.append(Paragraph(text, styles["Heading1"]))
            flowables.append(Spacer(1, 4 * mm))
            i += 1
            continue

        # Horizontal rule
        if stripped in ("---", "***", "___"):
            flowables.append(Spacer(1, 4 * mm))
            i += 1
            continue

        # Bullet list items
        if stripped.startswith(("- ", "* ", "• ")):
            text = _inline_markup(stripped[2:])
            flowables.append(Paragraph(f"• {text}", styles["bullet"]))
            i += 1
            continue

        # Numbered list items
        num_match = re.match(r"^(\d+)\.\s+(.+)", stripped)
        if num_match:
            num = num_match.group(1)
            text = _inline_markup(num_match.group(2))
            flowables.append(Paragraph(f"{num}. {text}", styles["bullet"]))
            i += 1
            continue

        # Regular paragraph — collect consecutive non-empty, non-special lines
        para_lines = [stripped]
        i += 1
        while i < len(lines):
            next_stripped = lines[i].strip()
            if not next_stripped or next_stripped.startswith(("#", "- ", "* ", "• ")):
                break
            if re.match(r"^\d+\.\s+", next_stripped):
                break
            if next_stripped in ("---", "***", "___"):
                break
            para_lines.append(next_stripped)
            i += 1

        text = _inline_markup(" ".join(para_lines))
        flowables.append(Paragraph(text, styles["BodyText"]))
        flowables.append(Spacer(1, 2 * mm))

    return flowables


def _inline_markup(text: str) -> str:
    """Convert markdown inline markup to reportlab XML tags.

    Handles **bold**, *italic*, `code`.
    Escapes XML special characters first.
    """
    # Escape XML specials (must be done before adding tags)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")

    # Bold: **text** → <b>text</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # Italic: *text* → <i>text</i>
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    # Code: `text` → <font face="Courier">text</font>
    text = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', text)

    return text


def _build_styles():
    """Build the stylesheet for PDF rendering."""
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        "bullet",
        parent=styles["BodyText"],
        leftIndent=12 * mm,
        firstLineIndent=0,
        spaceBefore=1 * mm,
        spaceAfter=1 * mm,
    ))
    return styles


def pdf_generate(
    content: str,
    filename: str,
    _sandbox: Path | None = None,
) -> dict:
    """Generate a PDF from markdown/plain text content.

    Args:
        content: Markdown or plain text to render.
        filename: Output filename (e.g. 'report.pdf'). Written to OUTPUT/.
        _sandbox: Internal override for repo root (testing only).

    Returns:
        dict with keys: ok, path, size_bytes, verified
    """
    if not content or not isinstance(content, str):
        return {
            "ok": False,
            "path": "",
            "size_bytes": 0,
            "verified": False,
            "error": "content is required (non-empty str)",
        }

    if not filename or not isinstance(filename, str):
        return {
            "ok": False,
            "path": "",
            "size_bytes": 0,
            "verified": False,
            "error": "filename is required (non-empty str)",
        }

    # Ensure .pdf extension
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    # Sanitize filename — strip path separators to prevent traversal
    safe_name = Path(filename).name
    if not safe_name:
        return {
            "ok": False,
            "path": "",
            "size_bytes": 0,
            "verified": False,
            "error": "filename resolved to empty after sanitization",
        }

    root = (_sandbox if _sandbox is not None else _resolve_repo_root()).resolve()
    output_dir = root / "OUTPUT"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / safe_name

    try:
        styles = _build_styles()
        flowables = _markdown_to_paragraphs(content, styles)

        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=A4,
            topMargin=20 * mm,
            bottomMargin=20 * mm,
            leftMargin=20 * mm,
            rightMargin=20 * mm,
        )
        doc.build(flowables)

    except Exception as exc:
        return {
            "ok": False,
            "path": f"OUTPUT/{safe_name}",
            "size_bytes": 0,
            "verified": False,
            "error": f"PDF generation failed: {exc}",
        }

    # Verify file exists and has content
    if not output_path.is_file():
        return {
            "ok": False,
            "path": f"OUTPUT/{safe_name}",
            "size_bytes": 0,
            "verified": False,
            "error": "PDF file not found after generation",
        }

    size_bytes = output_path.stat().st_size
    if size_bytes == 0:
        return {
            "ok": False,
            "path": f"OUTPUT/{safe_name}",
            "size_bytes": 0,
            "verified": False,
            "error": "PDF file is empty (0 bytes)",
        }

    return {
        "ok": True,
        "path": f"OUTPUT/{safe_name}",
        "size_bytes": size_bytes,
        "verified": True,
    }

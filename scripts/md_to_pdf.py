#!/usr/bin/env python3
"""Convert markdown to PDF using weasyprint."""

import sys

import markdown  # type: ignore[import-untyped]
from weasyprint import HTML


def md_to_pdf(md_path: str, pdf_path: str):
    with open(md_path) as f:
        md_text = f.read()

    html_body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "codehilite", "toc"],
    )

    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    @page {{
        size: A4;
        margin: 2cm;
    }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
        font-size: 11pt;
        line-height: 1.5;
        color: #1a1a1a;
        max-width: 100%;
    }}
    h1 {{
        font-size: 22pt;
        border-bottom: 3px solid #2563eb;
        padding-bottom: 8px;
        margin-top: 0;
        color: #1e293b;
    }}
    h2 {{
        font-size: 16pt;
        border-bottom: 1px solid #cbd5e1;
        padding-bottom: 4px;
        margin-top: 28px;
        color: #334155;
    }}
    h3 {{
        font-size: 13pt;
        margin-top: 20px;
        color: #475569;
    }}
    h4 {{
        font-size: 11pt;
        margin-top: 16px;
        color: #64748b;
    }}
    table {{
        border-collapse: collapse;
        width: 100%;
        margin: 12px 0;
        font-size: 9.5pt;
    }}
    th, td {{
        border: 1px solid #e2e8f0;
        padding: 6px 10px;
        text-align: left;
    }}
    th {{
        background-color: #f1f5f9;
        font-weight: 600;
        color: #334155;
    }}
    tr:nth-child(even) {{
        background-color: #f8fafc;
    }}
    code {{
        background-color: #f1f5f9;
        padding: 1px 4px;
        border-radius: 3px;
        font-size: 9.5pt;
        font-family: "JetBrains Mono", "Fira Code", Consolas, monospace;
    }}
    pre {{
        background-color: #1e293b;
        color: #e2e8f0;
        padding: 12px 16px;
        border-radius: 6px;
        overflow-x: auto;
        font-size: 8.5pt;
        line-height: 1.4;
    }}
    pre code {{
        background-color: transparent;
        color: inherit;
        padding: 0;
    }}
    hr {{
        border: none;
        border-top: 1px solid #e2e8f0;
        margin: 24px 0;
    }}
    blockquote {{
        border-left: 4px solid #2563eb;
        margin: 12px 0;
        padding: 8px 16px;
        background-color: #eff6ff;
        color: #1e40af;
    }}
    strong {{
        color: #1e293b;
    }}
    .codehilite {{
        background-color: #1e293b;
        padding: 12px 16px;
        border-radius: 6px;
        overflow-x: auto;
    }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""

    HTML(string=full_html).write_pdf(pdf_path)
    print(f"PDF written to {pdf_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.md> <output.pdf>")
        sys.exit(1)
    md_to_pdf(sys.argv[1], sys.argv[2])

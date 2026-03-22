#!/usr/bin/env python3
"""Convert markdown to PDF using fpdf2 with markdown parsing."""

import re
import sys

from fpdf import FPDF


class MarkdownPDF(FPDF):
    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(20, 20, 20)
        # Add Unicode fonts
        font_dir = "/usr/share/fonts/truetype/dejavu/"
        self.add_font("DejaVu", "", font_dir + "DejaVuSans.ttf", uni=True)
        self.add_font("DejaVu", "B", font_dir + "DejaVuSans-Bold.ttf", uni=True)
        self.add_font("DejaVu", "I", font_dir + "DejaVuSans.ttf", uni=True)
        self.add_font("DejaVuMono", "", font_dir + "DejaVuSansMono.ttf", uni=True)
        self.add_font("DejaVuMono", "B", font_dir + "DejaVuSansMono-Bold.ttf", uni=True)
        self.add_page()
        self.set_font("DejaVu", size=11)
        self.in_code_block = False
        self.table_rows = []
        self.table_col_widths = []

    def header(self):
        if self.page_no() > 1:
            self.set_font("DejaVu", "I", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 8, "Phase 7: Eval Framework & Code Quality — Implementation Plan", align="C")
            self.ln(4)
            self.set_draw_color(200, 200, 200)
            self.line(20, self.get_y(), self.w - 20, self.get_y())
            self.ln(6)

    def footer(self):
        self.set_y(-15)
        self.set_font("DejaVu", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    def write_rich_line(self, text, font_family="DejaVu", font_style="", font_size=11, color=(26, 26, 26)):
        """Write a line with inline bold/code formatting."""
        self.set_text_color(*color)
        # Split on bold and inline code markers
        parts = re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text)
        for part in parts:
            if part.startswith("**") and part.endswith("**"):
                self.set_font(font_family, "B", font_size)
                self.write(6, part[2:-2])
                self.set_font(font_family, font_style, font_size)
            elif part.startswith("`") and part.endswith("`"):
                self.set_font("DejaVuMono", "", max(font_size - 1, 8))
                self.set_fill_color(241, 245, 249)
                self.write(6, part[1:-1])
                self.set_font(font_family, font_style, font_size)
            else:
                self.set_font(font_family, font_style, font_size)
                self.write(6, part)

    def render_table(self, rows):
        """Render a markdown table."""
        if not rows or len(rows) < 2:
            return

        # Calculate column widths based on content
        num_cols = len(rows[0])
        usable_w = self.w - 40
        col_widths = [usable_w / num_cols] * num_cols

        # Header row
        self.set_font("DejaVu", "B", 9)
        self.set_fill_color(241, 245, 249)
        self.set_draw_color(200, 200, 200)
        for i, cell in enumerate(rows[0]):
            self.cell(col_widths[i], 7, cell.strip(), border=1, fill=True, align="L")
        self.ln()

        # Data rows (skip separator row at index 1)
        self.set_font("DejaVu", "", 9)
        for row_idx, row in enumerate(rows[2:] if len(rows) > 2 else []):
            if row_idx % 2 == 1:
                self.set_fill_color(248, 250, 252)
                fill = True
            else:
                self.set_fill_color(255, 255, 255)
                fill = True
            for i, cell in enumerate(row[:num_cols]):
                w = col_widths[i] if i < len(col_widths) else col_widths[-1]
                text = cell.strip()
                # Truncate if too long for cell
                self.set_font("DejaVu", "", 9)
                if self.get_string_width(text) > w - 2:
                    while self.get_string_width(text + "...") > w - 2 and len(text) > 3:
                        text = text[:-1]
                    text += "..."
                self.cell(w, 7, text, border=1, fill=fill, align="L")
            self.ln()
        self.ln(2)


def parse_and_render(pdf: MarkdownPDF, md_text: str):
    """Parse markdown and render to PDF."""
    lines = md_text.split("\n")
    i = 0
    in_code = False
    code_lines: list[str] = []
    table_rows: list[list[str]] = []

    while i < len(lines):
        line = lines[i]

        # Code blocks
        if line.strip().startswith("```"):
            if in_code:
                # End code block
                in_code = False
                if code_lines:
                    pdf.set_font("DejaVuMono", "", 8)
                    pdf.set_fill_color(30, 41, 59)
                    pdf.set_text_color(226, 232, 240)
                    # Render code block
                    y = pdf.get_y()
                    block_h = len(code_lines) * 4.5 + 6
                    if y + block_h > pdf.h - 25:
                        pdf.add_page()
                        y = pdf.get_y()
                    pdf.set_xy(22, y)
                    pdf.rect(20, y - 1, pdf.w - 40, block_h, "F")
                    for cl in code_lines:
                        pdf.set_x(24)
                        pdf.cell(0, 4.5, cl[:120], ln=True)
                    pdf.ln(3)
                    pdf.set_text_color(26, 26, 26)
                code_lines = []
            else:
                # Start code block
                in_code = True
                # Flush any pending table
                if table_rows:
                    pdf.render_table(table_rows)
                    table_rows = []
            i += 1
            continue

        if in_code:
            code_lines.append(line)
            i += 1
            continue

        # Table rows
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().split("|")]
            cells = [c for c in cells if c != ""]  # remove empty first/last
            # Skip separator rows
            if cells and all(re.match(r"^[-:]+$", c) for c in cells):
                table_rows.append(cells)  # Keep separator for rendering logic
            else:
                table_rows.append(cells)
            i += 1
            continue
        elif table_rows:
            pdf.render_table(table_rows)
            table_rows = []

        # Horizontal rule
        if re.match(r"^---+\s*$", line.strip()):
            pdf.set_draw_color(200, 200, 200)
            pdf.line(20, pdf.get_y() + 4, pdf.w - 20, pdf.get_y() + 4)
            pdf.ln(10)
            i += 1
            continue

        # H1
        if line.startswith("# "):
            text = line[2:].strip()
            pdf.set_font("DejaVu", "B", 20)
            pdf.set_text_color(30, 41, 59)
            pdf.multi_cell(0, 10, text)
            pdf.set_draw_color(37, 99, 235)
            pdf.set_line_width(0.8)
            pdf.line(20, pdf.get_y(), pdf.w - 20, pdf.get_y())
            pdf.set_line_width(0.2)
            pdf.ln(6)
            i += 1
            continue

        # H2
        if line.startswith("## "):
            text = line[3:].strip()
            # Check for page break need
            if pdf.get_y() > pdf.h - 50:
                pdf.add_page()
            pdf.ln(4)
            pdf.set_font("DejaVu", "B", 15)
            pdf.set_text_color(51, 65, 85)
            pdf.multi_cell(0, 8, text)
            pdf.set_draw_color(203, 213, 225)
            pdf.line(20, pdf.get_y(), pdf.w - 20, pdf.get_y())
            pdf.ln(4)
            i += 1
            continue

        # H3
        if line.startswith("### "):
            text = line[4:].strip()
            if pdf.get_y() > pdf.h - 40:
                pdf.add_page()
            pdf.ln(3)
            pdf.set_font("DejaVu", "B", 13)
            pdf.set_text_color(71, 85, 105)
            pdf.multi_cell(0, 7, text)
            pdf.ln(2)
            i += 1
            continue

        # H4
        if line.startswith("#### "):
            text = line[5:].strip()
            pdf.set_font("DejaVu", "B", 11)
            pdf.set_text_color(100, 116, 139)
            pdf.multi_cell(0, 6, text)
            pdf.ln(2)
            i += 1
            continue

        # Bullet points
        if re.match(r"^[\-\*] ", line.strip()):
            text = re.sub(r"^[\-\*] ", "", line.strip())
            pdf.set_x(25)
            pdf.set_font("DejaVu", "", 10)
            pdf.set_text_color(26, 26, 26)
            pdf.cell(5, 6, chr(8226))
            pdf.write_rich_line(text, font_size=10)
            pdf.ln(6)
            i += 1
            continue

        # Numbered list
        m = re.match(r"^(\d+)\.\s+(.*)", line.strip())
        if m:
            num, text = m.group(1), m.group(2)
            pdf.set_x(25)
            pdf.set_font("DejaVu", "B", 10)
            pdf.set_text_color(37, 99, 235)
            pdf.cell(8, 6, f"{num}.")
            pdf.set_text_color(26, 26, 26)
            pdf.write_rich_line(text, font_size=10)
            pdf.ln(6)
            i += 1
            continue

        # Empty line
        if line.strip() == "":
            pdf.ln(3)
            i += 1
            continue

        # Regular paragraph
        text = line.strip()
        if text:
            pdf.set_font("DejaVu", "", 10)
            pdf.set_text_color(26, 26, 26)
            pdf.write_rich_line(text, font_size=10)
            pdf.ln(6)

        i += 1

    # Flush remaining table
    if table_rows:
        pdf.render_table(table_rows)


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.md> <output.pdf>")
        sys.exit(1)

    md_path, pdf_path = sys.argv[1], sys.argv[2]

    with open(md_path) as f:
        md_text = f.read()

    pdf = MarkdownPDF()
    pdf.alias_nb_pages()
    parse_and_render(pdf, md_text)
    pdf.output(pdf_path)
    print(f"PDF written to {pdf_path}")


if __name__ == "__main__":
    main()

# Skill: generate-pdf-report

Generate a PDF report from text or markdown and deliver it via Telegram.

---

## Frontmatter

```yaml
name: generate-pdf-report
version: 1.0.0
description: >
  Generate a PDF from text or markdown content and send it to Telegram.
  Uses reportlab for deterministic PDF rendering and the Telegram Bot API
  for delivery.

tools:
  - pdf.generate
  - telegram.send_file
  - contracts.validate

tool_rules:
  - "always verify PDF exists after generation"
  - "never send files outside sandbox"
  - "always validate output contract"

output_contract:
  - summary
  - files_changed
  - verification
  - confidence
```

---

## When To Use

- A task requests generating a PDF report.
- A task asks to export results as a PDF.
- A task asks to send a document to Telegram.
- A task combines report generation with Telegram delivery.

Do **not** use for:
- Sending plain text messages to Telegram (use telegram_notifier instead).
- Generating images or non-PDF documents.
- Reading or parsing existing PDFs.

---

## Workflow

### 1. Prepare report content

Assemble the report content as markdown or plain text. The content should
be complete before calling the PDF generator.

### 2. Generate PDF

Use `pdf.generate` to create the PDF file.

```
Tool: pdf.generate
Args: {
  "content": "<markdown or plain text>",
  "filename": "<descriptive_name>.pdf"
}
```

Verify the return includes `ok: true` and `verified: true`.

### 3. Verify file exists

Confirm the generated PDF exists at the returned path and has non-zero size.
The adapter performs post-write verification, but the skill should confirm
the `verified` field in the response.

### 4. Send to Telegram

Use `telegram.send_file` to deliver the PDF.

```
Tool: telegram.send_file
Args: {
  "path": "OUTPUT/<filename>.pdf",
  "caption": "<brief description of the report>"
}
```

Verify the return includes `ok: true` and `file_sent: true`.

### 5. Validate contract

Use `contracts.validate` to check the output report.

```
Tool: contracts.validate
Args: { "text": "<full output including ## CONTRACT block>" }
```

### 6. Emit structured contract

Produce the final report with contract block:

```markdown
## CONTRACT
summary: PDF report generated and sent to Telegram
pdf_path: OUTPUT/<filename>.pdf
telegram_status: sent
telegram_message_id: <id>
files_changed: OUTPUT/<filename>.pdf (created)
verification: pdf.generate verified=true, telegram.send_file file_sent=true
confidence: high
```

---

## Confidence Scoring

| Level  | Criteria |
|--------|----------|
| high   | PDF generated and verified, Telegram delivery confirmed |
| medium | PDF generated but Telegram delivery could not be confirmed |
| low    | PDF generation failed or file verification failed |

---

## Failure Handling

- **Empty content**: Return error immediately, do not generate empty PDF.
- **PDF generation fails**: Report the error, set confidence `low`.
- **File not found after generation**: Report verification failure.
- **Telegram send fails**: Report delivery failure but note PDF was created.
  Set confidence `medium` (PDF exists, delivery failed).
- **Missing Telegram credentials**: Report missing env vars. PDF still created.
- **Contract validation fails**: Fix contract and re-validate (up to 2 retries).

---

## Tool Doctrine

1. **Generate before send** — always create the PDF before attempting delivery.
2. **Verify after generation** — confirm file exists and has content.
3. **Sandbox-safe paths** — all file operations within ~/nova-core.
4. **Deterministic rendering** — uses reportlab, no shell execution.
5. **Honest confidence** — score reflects actual evidence from both steps.
6. **Fail loudly** — if anything goes wrong, report it in the contract.

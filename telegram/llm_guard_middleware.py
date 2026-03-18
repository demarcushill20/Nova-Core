"""LLM Guard middleware for prompt injection defense.

Phase 3.3: Uses regex-based scanning (no external ML dependency) as a
defense layer. If llm-guard is installed, uses DeBERTa model for higher
accuracy prompt injection detection.

Scans both user input and LLM output for security issues.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

_log = logging.getLogger("telegram_bot.llm_guard")

# --- Confidence thresholds ---
BLOCK_CONFIDENCE = 0.8
FLAG_CONFIDENCE = 0.5


@dataclass
class ScanResult:
    """Result of scanning text through LLM Guard."""

    safe: bool
    confidence: float  # 0.0 = safe, 1.0 = definitely malicious
    findings: list[str] = field(default_factory=list)
    action: str = "allow"  # allow, flag, block


# --- Input scan patterns (supplement input_security.py) ---

_INPUT_PATTERNS: list[tuple[str, float, str]] = [
    # Direct instruction override
    (
        r"(?i)(?:ignore|disregard|forget)\s+(?:all\s+)?"
        r"(?:previous|above|prior)\s+(?:instructions?|rules?|constraints?)",
        0.9,
        "instruction_override",
    ),
    # Role impersonation
    (r"(?i)you\s+are\s+(?:now|actually)\s+(?:a|an|the)\s+", 0.7, "role_impersonation"),
    # System prompt extraction
    (
        r"(?i)(?:show|reveal|display|print|output)\s+(?:your|the)"
        r"\s+(?:system|initial|original)\s+(?:prompt|instructions?|message)",
        0.85,
        "prompt_extraction",
    ),
    # Delimiter escape
    (r"(?:<<<|>>>|```system|<\|im_start\|>|<\|endoftext\|>)", 0.8, "delimiter_escape"),
    # Payload smuggling
    (r"(?i)(?:base64|eval|exec)\s*\(", 0.6, "payload_smuggling"),
    # Context manipulation
    (
        r"(?i)(?:pretend|imagine|hypothetically|"
        r"in\s+a\s+(?:fictional|hypothetical)\s+(?:scenario|world))",
        0.5,
        "context_manipulation",
    ),
    # Data exfil requests
    (r"(?i)(?:send|post|upload|transmit)\s+(?:to|this\s+to)\s+(?:https?://|webhook|api)", 0.75, "exfil_request"),
    # Instruction injection via formatting
    (r"(?i)\[(?:system|admin|root)\]:", 0.7, "format_injection"),
]

_INPUT_RE = [(re.compile(p), conf, name) for p, conf, name in _INPUT_PATTERNS]

# --- Output scan patterns ---

_OUTPUT_PATTERNS: list[tuple[str, float, str]] = [
    # Leaked system prompt indicators
    (r"(?i)(?:my\s+system\s+prompt|my\s+instructions?\s+(?:say|are|tell))", 0.8, "system_prompt_leak"),
    # Malicious URLs in response
    (r"(?i)(?:https?://(?:bit\.ly|tinyurl|t\.co|goo\.gl)/)", 0.6, "shortened_url"),
    # Refusal bypass indicators
    (r"(?i)(?:I\s+(?:shouldn't|cannot|can't|must\s+not)\s+but\s+(?:here|I'll|let\s+me))", 0.7, "refusal_bypass"),
    # Code execution instructions
    (r"(?i)(?:run\s+this\s+(?:command|script)|execute\s+the\s+following)\s*:?\s*```", 0.5, "code_execution"),
]

_OUTPUT_RE = [(re.compile(p), conf, name) for p, conf, name in _OUTPUT_PATTERNS]


def scan_input(text: str) -> ScanResult:
    """Scan user input for prompt injection patterns.

    Returns ScanResult with confidence and findings.
    """
    findings: list[str] = []
    max_confidence = 0.0

    for pattern, confidence, name in _INPUT_RE:
        if pattern.search(text):
            findings.append(name)
            max_confidence = max(max_confidence, confidence)

    # Try ML-based detection if available
    try:
        from llm_guard.input_scanners import PromptInjection

        scanner = PromptInjection()
        sanitized, is_valid, risk_score = scanner.scan("", text)
        if risk_score > max_confidence:
            max_confidence = risk_score
            if not is_valid:
                findings.append("ml_prompt_injection")
    except ImportError:
        pass

    if max_confidence >= BLOCK_CONFIDENCE:
        action = "block"
    elif max_confidence >= FLAG_CONFIDENCE:
        action = "flag"
    else:
        action = "allow"

    result = ScanResult(
        safe=max_confidence < BLOCK_CONFIDENCE,
        confidence=max_confidence,
        findings=findings,
        action=action,
    )

    if findings:
        _log.info(
            "INPUT_SCAN: confidence=%.2f action=%s findings=%s",
            max_confidence,
            action,
            findings,
        )

    return result


def scan_output(text: str) -> ScanResult:
    """Scan LLM output for security issues.

    Returns ScanResult with confidence and findings.
    """
    findings: list[str] = []
    max_confidence = 0.0

    for pattern, confidence, name in _OUTPUT_RE:
        if pattern.search(text):
            findings.append(name)
            max_confidence = max(max_confidence, confidence)

    # Try ML-based output scanning if available
    try:
        from llm_guard.output_scanners import MaliciousURLs, Sensitive

        for scanner_cls in [Sensitive, MaliciousURLs]:
            scanner = scanner_cls()
            sanitized, is_valid, risk_score = scanner.scan("", text)
            if risk_score > max_confidence:
                max_confidence = risk_score
            if not is_valid:
                findings.append(f"ml_{scanner_cls.__name__.lower()}")
    except ImportError:
        pass

    if max_confidence >= BLOCK_CONFIDENCE:
        action = "block"
    elif max_confidence >= FLAG_CONFIDENCE:
        action = "flag"
    else:
        action = "allow"

    result = ScanResult(
        safe=max_confidence < BLOCK_CONFIDENCE,
        confidence=max_confidence,
        findings=findings,
        action=action,
    )

    if findings:
        _log.info(
            "OUTPUT_SCAN: confidence=%.2f action=%s findings=%s",
            max_confidence,
            action,
            findings,
        )

    return result

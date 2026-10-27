"""PII masking applied BEFORE any text is sent to the LLM.

Principle: data minimisation. The model needs vendor, amounts and dates to do its job.
It does not need bank account numbers, routing numbers, SSNs, card numbers or a
contact person's email/phone, so we never send them.

Note: vendor tax IDs (EIN) are business identifiers we DO need for matching, so they
are deliberately not masked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Order matters: more specific patterns first so a routing number isn't eaten
# by the generic long-digit rule.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b")),
    ("ROUTING_NUMBER", re.compile(r"(?i)(?<=routing)(?:\s*(?:no\.?|number|#))?\s*[:\-]?\s*\d{9}\b")),
    ("BANK_ACCOUNT", re.compile(r"(?i)(?<=account)(?:\s*(?:no\.?|number|#))?\s*[:\-]?\s*\d[\d\- ]{6,18}\d\b")),
    ("BANK_ACCOUNT", re.compile(r"(?i)(?<=a/c)(?:\s*(?:no\.?|#))?\s*[:\-]?\s*\d[\d\- ]{6,18}\d\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD_NUMBER", re.compile(r"\b(?:\d{4}[ -]){3}\d{4}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")),
]


@dataclass
class MaskResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def mask_pii(text: str) -> MaskResult:
    counts: dict[str, int] = {}
    for label, pattern in _PATTERNS:
        def _sub(match: re.Match[str], label: str = label) -> str:
            counts[label] = counts.get(label, 0) + 1
            # Keep the leading separator (": ") so the document stays readable.
            prefix = re.match(r"^[\s:#\-]*(?:no\.?|number|#)?[\s:\-]*", match.group(0), re.I)
            lead = prefix.group(0) if prefix else ""
            return f"{lead}[REDACTED_{label}]"

        text = pattern.sub(_sub, text)
    return MaskResult(text=text, counts=counts)

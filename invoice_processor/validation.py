"""Deterministic checks on extracted data.

The LLM reads; plain code verifies. Anything that can be checked with arithmetic or a
date parser is checked here, not trusted to the model. The result is a list of human-
readable issues plus a confidence score used to decide whether auto-approval is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Invoice

REQUIRED_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "total")

# Fields where a misread could send money to the wrong place or in the wrong amount.
# If the model is unsure about one of these, a human must look. Uncertainty about
# anything else (due date, currency symbol, notes) is recorded as a non-blocking warning.
# Added after the first real eval: every clean "$" invoice was sent to review because
# the model was (honestly) unsure the currency was USD.
CRITICAL_FIELDS = {
    "vendor_name", "vendor_tax_id", "invoice_number", "invoice_date",
    "subtotal", "tax", "total", "line_items",
}


@dataclass
class ValidationReport:
    issues: list[str] = field(default_factory=list)  # block auto-approval
    warnings: list[str] = field(default_factory=list)  # shown to humans, don't block
    confidence: float = 1.0

    @property
    def ok(self) -> bool:
        return not self.issues


def _close(a: float, b: float, tol: float = 0.02) -> bool:
    # Money must match to the cent (allowing 2 cents for rounding). An earlier version
    # used a 0.5% tolerance, and the eval caught a $45.50 error hiding inside it on a
    # $9k invoice. Percentage tolerances are wrong for money.
    return abs(a - b) <= tol


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def validate_invoice(inv: Invoice, today: date | None = None) -> ValidationReport:
    today = today or date.today()
    report = ValidationReport()
    penalty = 0.0

    def issue(msg: str, cost: float) -> None:
        nonlocal penalty
        report.issues.append(msg)
        penalty += cost

    if inv.contains_instructions:
        issue("Document contains embedded instructions (possible prompt injection).", 1.0)

    for name in REQUIRED_FIELDS:
        if getattr(inv, name) in (None, ""):
            issue(f"Missing required field: {name}", 0.3)

    if inv.invoice_date:
        d = _parse_iso(inv.invoice_date)
        if d is None:
            issue(f"invoice_date is not a valid ISO date: {inv.invoice_date!r}", 0.3)
        elif d > today:
            issue(f"invoice_date {d} is in the future", 0.2)
        elif (today - d).days > 365:
            issue(f"invoice_date {d} is more than a year old", 0.1)

    if inv.due_date and inv.invoice_date:
        due, issued = _parse_iso(inv.due_date), _parse_iso(inv.invoice_date)
        if due and issued and due < issued:
            issue("due_date is before invoice_date", 0.1)

    if inv.total is not None and inv.total <= 0:
        issue(f"total must be positive, got {inv.total}", 0.3)

    if inv.line_items:
        items_sum = round(sum(li.amount for li in inv.line_items), 2)
        base = inv.subtotal if inv.subtotal is not None else (
            inv.total - (inv.tax or 0) if inv.total is not None else None
        )
        if base is not None and not _close(items_sum, base, tol=0.01 * len(inv.line_items) + 0.01):
            issue(f"Line items sum to {items_sum:.2f} but subtotal is {base:.2f}", 0.25)
        for li in inv.line_items:
            if li.quantity is not None and li.unit_price is not None and not _close(
                li.quantity * li.unit_price, li.amount
            ):
                issue(f"Line '{li.description}': {li.quantity} x {li.unit_price} != {li.amount}", 0.15)
                break  # one is enough to send it for review

    if inv.subtotal is not None and inv.total is not None:
        expected = inv.subtotal + (inv.tax or 0)
        if not _close(expected, inv.total):
            issue(f"subtotal + tax = {expected:.2f} but total is {inv.total:.2f}", 0.25)

    critical = [f for f in inv.uncertain_fields if f in CRITICAL_FIELDS]
    minor = [f for f in inv.uncertain_fields if f not in CRITICAL_FIELDS]
    if critical:
        issue(f"Model was unsure about critical field(s): {', '.join(critical)}", min(0.1 * len(critical), 0.3))
    if minor:
        report.warnings.append(f"Model was unsure about: {', '.join(minor)}")
        penalty += min(0.02 * len(minor), 0.06)

    report.confidence = round(max(0.0, 1.0 - penalty), 2)
    return report

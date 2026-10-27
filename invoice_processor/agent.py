"""Approval agent: decides what happens to an extracted invoice.

The agent uses tools to look things up (vendor master, duplicates, policy) and must
finish with exactly ONE decision tool. Three safety layers:

1. The agent never sees the raw document - only validated JSON. Text hidden inside an
   invoice can't reach it, which shrinks the prompt-injection surface.
2. Hard guardrails in code: `mark_ready_for_payment` re-checks the policy itself and
   refuses if any condition fails. The prompt asks; the code enforces.
3. There is no "pay" tool. The best outcome the agent can produce is
   "ready for payment", which a payment system / human still acts on.

If the agent loops without deciding, the invoice defaults to human review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import Store, normalize_vendor
from .models import Invoice, Status
from .validation import ValidationReport

MAX_STEPS = 6

TOOLS = [
    {
        "name": "lookup_vendor",
        "description": "Look up a vendor in the approved vendor master list. Returns the vendor if found, otherwise close matches.",
        "input_schema": {"type": "object", "properties": {"vendor_name": {"type": "string"}}, "required": ["vendor_name"]},
    },
    {
        "name": "check_duplicate",
        "description": "Check whether an invoice with the same vendor and invoice number was already processed.",
        "input_schema": {
            "type": "object",
            "properties": {"vendor_name": {"type": "string"}, "invoice_number": {"type": "string"}},
            "required": ["vendor_name", "invoice_number"],
        },
    },
    {
        "name": "get_approval_policy",
        "description": "Get the company's accounts-payable approval policy.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "mark_ready_for_payment",
        "description": "FINAL DECISION. Approve the invoice for the payment queue. Only valid if every policy condition is met; the system will refuse otherwise.",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
    {
        "name": "request_manager_approval",
        "description": "FINAL DECISION. Send the invoice to a manager for sign-off (e.g. amount above threshold).",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
    {
        "name": "send_to_human_review",
        "description": "FINAL DECISION. Send the invoice to the AP team for manual review (data problems, unknown vendor, duplicates, anything suspicious).",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
]

DECISION_TOOLS = {
    "mark_ready_for_payment": Status.READY_FOR_PAYMENT,
    "request_manager_approval": Status.PENDING_MANAGER_APPROVAL,
    "send_to_human_review": Status.NEEDS_HUMAN_REVIEW,
}

SYSTEM_PROMPT = """You are the accounts-payable routing assistant for a mid-sized company.
You receive ONE invoice that has already been extracted and validated. Use the tools to
check the vendor, look for duplicates and read the approval policy, then make exactly
one final decision by calling one FINAL DECISION tool with a short, specific reason
(one or two sentences an auditor would understand).

Be conservative: when in doubt, send to human review. A wrong payment costs far more
than a short delay."""


def policy_text(settings: Settings) -> dict[str, Any]:
    return {
        "approval_threshold": settings.approval_threshold,
        "currency": "USD",
        "rules": [
            "1. If there are ANY validation issues (warnings do not count), the vendor is not on the approved list, "
            "or the invoice is a possible duplicate -> send_to_human_review.",
            f"2. Otherwise, if total > {settings.approval_threshold:.2f} -> request_manager_approval.",
            "3. Otherwise -> mark_ready_for_payment.",
            "A vendor name that differs only in suffix/punctuation (e.g. 'Inc.', ',') counts as the same vendor; "
            "lookup_vendor already normalises this. A close match is NOT a match.",
        ],
    }


@dataclass
class AgentResult:
    status: Status
    reason: str
    trace: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    used_fallback: bool = False


class _Ctx:
    def __init__(self, store: Store, settings: Settings, invoice_id: int,
                 invoice: Invoice, report: ValidationReport):
        self.store, self.settings = store, settings
        self.invoice_id, self.invoice, self.report = invoice_id, invoice, report
        self.decision: tuple[Status, str] | None = None

    # Hard guardrail: re-derive every condition from source data, never from the model.
    def payment_blockers(self) -> list[str]:
        inv, s = self.invoice, self.settings
        blockers = []
        if self.report.issues:
            blockers.append("invoice has validation issues")
        if self.report.confidence < s.min_confidence_for_auto:
            blockers.append(f"confidence {self.report.confidence} below {s.min_confidence_for_auto}")
        if not self.store.lookup_vendor(inv.vendor_name or "")["found"]:
            blockers.append("vendor is not on the approved vendor list")
        if self.store.find_duplicates(inv.vendor_name, inv.invoice_number, exclude_id=self.invoice_id):
            blockers.append("possible duplicate invoice")
        if inv.total is None or inv.total > s.approval_threshold:
            blockers.append(f"total exceeds auto-approval threshold of {s.approval_threshold:.2f}")
        if (inv.currency or "USD") != "USD":
            blockers.append(f"currency {inv.currency} requires manual handling")
        return blockers

    def run_tool(self, name: str, args: dict) -> dict:
        if name == "lookup_vendor":
            return self.store.lookup_vendor(args.get("vendor_name", ""))
        if name == "check_duplicate":
            dups = self.store.find_duplicates(args.get("vendor_name"), args.get("invoice_number"),
                                              exclude_id=self.invoice_id)
            return {"is_duplicate": bool(dups), "matches": dups}
        if name == "get_approval_policy":
            return policy_text(self.settings)
        if name in DECISION_TOOLS:
            if self.decision is not None:
                return {"ok": False, "error": "A decision was already recorded."}
            reason = (args.get("reason") or "").strip() or "(no reason given)"
            if name == "mark_ready_for_payment":
                blockers = self.payment_blockers()
                if blockers:
                    # Refuse and tell the agent why; it must choose another decision.
                    return {"ok": False, "error": "Refused by policy guardrail: " + "; ".join(blockers)}
            self.decision = (DECISION_TOOLS[name], reason)
            return {"ok": True, "status": DECISION_TOOLS[name].value}
        return {"ok": False, "error": f"Unknown tool {name}"}


def _brief(invoice_id: int, invoice: Invoice, report: ValidationReport) -> str:
    payload = {
        "invoice_id": invoice_id,
        "invoice": invoice.model_dump(exclude={"notes"}),
        "validation": {
            "issues": report.issues,
            "warnings_non_blocking": report.warnings,
            "confidence": report.confidence,
        },
    }
    return "Route this invoice.\n\n" + json.dumps(payload, indent=2)


def run_agent(client, store: Store, settings: Settings, invoice_id: int,
              invoice: Invoice, report: ValidationReport) -> AgentResult:
    ctx = _Ctx(store, settings, invoice_id, invoice, report)
    messages: list[dict] = [{"role": "user", "content": _brief(invoice_id, invoice, report)}]
    trace: list[dict] = []
    in_tok = out_tok = 0

    for _ in range(MAX_STEPS):
        response = client.messages.create(
            model=settings.agent_model,
            max_tokens=4000,  # room for default thinking + tool calls
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tok += getattr(usage, "input_tokens", 0) or 0
            out_tok += getattr(usage, "output_tokens", 0) or 0

        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        for b in response.content:
            if getattr(b, "type", None) == "text" and b.text.strip():
                trace.append({"type": "thought", "text": b.text.strip()})
        if not tool_uses:
            break

        results = []
        for tu in tool_uses:
            output = ctx.run_tool(tu.name, dict(tu.input))
            trace.append({"type": "tool", "name": tu.name, "input": dict(tu.input), "output": output})
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(output, default=str)})
        messages.append({"role": "user", "content": results})
        if ctx.decision is not None:
            break

    if ctx.decision is None:
        return AgentResult(Status.NEEDS_HUMAN_REVIEW, "Agent did not reach a decision; defaulting to human review.",
                           trace, in_tok, out_tok, used_fallback=True)
    status, reason = ctx.decision
    return AgentResult(status, reason, trace, in_tok, out_tok)


def vendor_is_known(store: Store, name: str | None) -> bool:  # small helper for the UI
    return bool(name) and store.lookup_vendor(normalize_vendor(name))["found"]

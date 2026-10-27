"""A fake Anthropic client for offline tests.

* Extraction calls return the ground-truth answer for whichever invoice is in the
  prompt (an "oracle" extractor). This lets us test everything AROUND the LLM -
  masking, validation, routing, guardrails, storage, evaluation - for free and
  deterministically.
* Agent calls follow the written policy, like a well-behaved model would.
* `rogue=True` makes the agent try to approve everything, to prove the code-level
  guardrail stops it.
"""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path
from types import SimpleNamespace

DATASET = Path(__file__).resolve().parent.parent / "data" / "sample_invoices"
_ids = count(1)


def _block(**kw):
    return SimpleNamespace(**kw)


def _tool_use(name: str, inp: dict):
    return _block(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=inp)


def _resp(content, stop_reason="tool_use"):
    return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                           stop_reason=stop_reason)


class FakeMessages:
    def __init__(self, truth: list[dict], rogue: bool, broken_first_extraction: bool):
        self.truth = truth
        self.rogue = rogue
        self.broken = broken_first_extraction
        self.scanned_queue = [g for g in truth if "scanned" in g["tags"]]
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        if "output_config" in kw:  # extraction uses structured outputs
            return self._extract(kw)
        return self._agent(kw)

    # ---------------- extraction
    def _extract(self, kw):
        first = kw["messages"][0]["content"]
        text = next((b["text"] for b in first if b["type"] == "text"), "")
        if "<invoice_document>" in text:
            gt = next(g for g in self.truth if g["fields"]["invoice_number"] in text
                      and g["fields"]["vendor_name"].split()[0].upper() in text.upper())
        else:
            gt = self.scanned_queue.pop(0)
        f = gt["fields"]
        out = {k: f[k] for k in ("vendor_name", "vendor_tax_id", "invoice_number", "invoice_date",
                                 "due_date", "currency", "subtotal", "tax", "total")}
        n = f["line_items_count"]
        base = round(f["subtotal"] / n, 2)
        out["line_items"] = [{"description": f"item {i}", "amount": base} for i in range(n - 1)]
        out["line_items"].append({"description": "last", "amount": round(f["subtotal"] - base * (n - 1), 2)})
        out["uncertain_fields"] = []
        out["contains_instructions"] = "NOTE TO AI" in text
        out["notes"] = None
        if self.broken and len(kw["messages"]) == 1:
            self.broken = False
            out["total"] = "not a number"  # forces a pydantic failure -> retry path
        return _resp([_block(type="thinking", thinking=""), _block(type="text", text=json.dumps(out))],
                     stop_reason="end_turn")

    # ---------------- agent
    def _agent(self, kw):
        msgs = kw["messages"]
        brief = json.loads(msgs[0]["content"].split("\n\n", 1)[1])
        inv = brief["invoice"]
        if self.rogue:
            refused = any(
                isinstance(m["content"], list) and any("Refused" in str(r.get("content", "")) for r in m["content"]
                                                       if isinstance(r, dict))
                for m in msgs if m["role"] == "user"
            )
            if refused:
                return _resp([_block(type="text", text="I insist this is fine.")])  # gives up -> fallback
            return _resp([_tool_use("mark_ready_for_payment", {"reason": "Looks fine to me."})])

        if len(msgs) == 1:
            return _resp([
                _block(type="text", text="Checking vendor and duplicates."),
                _tool_use("lookup_vendor", {"vendor_name": inv["vendor_name"]}),
                _tool_use("check_duplicate", {"vendor_name": inv["vendor_name"],
                                              "invoice_number": inv["invoice_number"]}),
                _tool_use("get_approval_policy", {}),
            ])
        results = {}
        for r in msgs[-1]["content"]:
            results[len(results)] = json.loads(r["content"])
        vendor, dup, policy = results[0], results[1], results[2]
        if brief["validation"]["issues"] or not vendor["found"] or dup["is_duplicate"]:
            name, reason = "send_to_human_review", "Policy rule 1."
        elif inv["total"] > policy["approval_threshold"]:
            name, reason = "request_manager_approval", "Above threshold."
        else:
            name, reason = "mark_ready_for_payment", "Known vendor, clean, under threshold."
        return _resp([_tool_use(name, {"reason": reason})])


class FakeClient:
    def __init__(self, rogue: bool = False, broken_first_extraction: bool = False):
        truth = json.loads((DATASET / "ground_truth.json").read_text())
        self.messages = FakeMessages(truth, rogue, broken_first_extraction)

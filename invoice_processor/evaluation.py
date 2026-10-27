"""Evaluation: run the real pipeline over the labelled dataset and score it.

Two questions, scored separately:
  1. Extraction - did we read each field correctly?            (per-field accuracy)
  2. Decision   - did the invoice end up in the right queue?   (routing accuracy)

The decision metric that matters most is the "unsafe" count: invoices that should have
gone to a human or manager but were marked ready for payment. Target: zero.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Settings
from .db import Store, normalize_invoice_number, normalize_vendor
from .pipeline import process_file

FIELDS = ["vendor_name", "vendor_tax_id", "invoice_number", "invoice_date", "due_date",
          "currency", "subtotal", "tax", "total", "line_items_count"]


def field_matches(name: str, expected, actual) -> bool:
    if expected is None:
        return actual in (None, "")
    if actual is None:
        return False
    if name == "vendor_name":
        return normalize_vendor(expected) == normalize_vendor(actual)
    if name in {"invoice_number", "vendor_tax_id"}:
        return normalize_invoice_number(expected) == normalize_invoice_number(actual)
    if name in {"subtotal", "tax", "total"}:
        return abs(float(expected) - float(actual)) <= 0.01
    if name == "currency":
        return str(expected).upper() == str(actual).upper()
    return expected == actual


def run_evaluation(client, dataset_dir: str | Path, settings: Settings, limit: int | None = None,
                   today: date | None = None, store: Store | None = None) -> dict:
    dataset_dir = Path(dataset_dir)
    truth = json.loads((dataset_dir / "ground_truth.json").read_text())
    if limit:
        truth = truth[:limit]

    # Fresh in-memory DB each run so results are reproducible and duplicates are
    # detected only within the dataset, in file order. (build_demo_db passes a real
    # file-backed store so the same run also produces the demo database.)
    if store is None:
        store = Store(":memory:")
        store.seed_vendors(settings.vendors_csv)
        settings = replace(settings, db_path=Path(":memory:"))

    per_field = defaultdict(lambda: [0, 0])  # name -> [correct, total]
    status_pairs, failures, rows = [], [], []
    latencies, in_tok, out_tok = [], 0, 0

    for gt in truth:
        res = process_file(dataset_dir / gt["file"], client, store, settings, today=today)
        inv = res.extraction.invoice if res.extraction else None
        actual = inv.model_dump() if inv else {}
        actual["line_items_count"] = len(actual.get("line_items", []))
        if res.extraction:
            latencies.append(res.extraction.latency_s)
            in_tok += res.extraction.input_tokens
            out_tok += res.extraction.output_tokens
        if res.agent:
            in_tok += res.agent.input_tokens
            out_tok += res.agent.output_tokens

        wrong = {}
        for f in FIELDS:
            ok = field_matches(f, gt["fields"][f], actual.get(f))
            per_field[f][0] += ok
            per_field[f][1] += 1
            if not ok:
                wrong[f] = {"expected": gt["fields"][f], "actual": actual.get(f)}

        status_ok = res.status == gt["expected_status"]
        status_pairs.append((gt["expected_status"], res.status))
        row = {"file": gt["file"], "tags": gt["tags"], "expected_status": gt["expected_status"],
               "actual_status": res.status, "status_ok": status_ok, "reason": res.reason,
               "wrong_fields": wrong}
        rows.append(row)
        if wrong or not status_ok:
            failures.append(row)

    n = len(truth)
    field_acc = {f: round(c / t, 3) for f, (c, t) in per_field.items()}
    all_fields_correct = sum(1 for r in rows if not r["wrong_fields"])
    unsafe = [r for r in rows if r["actual_status"] == "ready_for_payment"
              and r["expected_status"] != "ready_for_payment"]
    by_tag = defaultdict(lambda: [0, 0])
    for r in rows:
        for t in r["tags"]:
            by_tag[t][0] += r["status_ok"]
            by_tag[t][1] += 1

    return {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "extraction_model": settings.extraction_model,
        "agent_model": settings.agent_model,
        "n_invoices": n,
        "field_accuracy": field_acc,
        "documents_fully_correct": round(all_fields_correct / n, 3) if n else 0,
        "routing_accuracy": round(sum(r["status_ok"] for r in rows) / n, 3) if n else 0,
        "routing_accuracy_by_tag": {t: round(c / tot, 3) for t, (c, tot) in by_tag.items()},
        "unsafe_auto_approvals": len(unsafe),
        "confusion": {f"{e} -> {a}": c for (e, a), c in Counter(status_pairs).items()},
        "avg_extraction_latency_s": round(statistics.mean(latencies), 2) if latencies else None,
        "total_input_tokens": in_tok,
        "total_output_tokens": out_tok,
        "failures": failures,
        "rows": rows,
    }


def to_markdown(r: dict) -> str:
    lines = [
        f"# Evaluation report ({r['run_at']})",
        "",
        f"- Invoices: **{r['n_invoices']}**  |  extraction model `{r['extraction_model']}`, agent model `{r['agent_model']}`",
        f"- Documents with every field correct: **{r['documents_fully_correct']:.0%}**",
        f"- Routing accuracy: **{r['routing_accuracy']:.0%}**",
        f"- Unsafe auto-approvals (should be 0): **{r['unsafe_auto_approvals']}**",
        f"- Avg extraction latency: {r['avg_extraction_latency_s']}s  |  tokens in/out: {r['total_input_tokens']:,} / {r['total_output_tokens']:,}",
        "",
        "## Field accuracy",
        "",
        "| Field | Accuracy |",
        "|---|---|",
        *[f"| {f} | {a:.0%} |" for f, a in r["field_accuracy"].items()],
        "",
        "## Routing accuracy by case type",
        "",
        "| Tag | Accuracy |",
        "|---|---|",
        *[f"| {t} | {a:.0%} |" for t, a in sorted(r["routing_accuracy_by_tag"].items())],
        "",
        "## Failures",
        "",
    ]
    if not r["failures"]:
        lines.append("None.")
    for f in r["failures"]:
        lines.append(f"**{f['file']}** ({', '.join(f['tags'])}): expected `{f['expected_status']}`, got `{f['actual_status']}`")
        for name, v in f["wrong_fields"].items():
            lines.append(f"  - `{name}`: expected `{v['expected']}`, got `{v['actual']}`")
        lines.append("")
    return "\n".join(lines)

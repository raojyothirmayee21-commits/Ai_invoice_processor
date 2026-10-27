"""End-to-end processing of one file: the thing a scheduler or the UI calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from .agent import AgentResult, run_agent
from .config import Settings
from .db import Store
from .extractor import ExtractionResult, extract_invoice
from .ingest import load_document
from .models import Status
from .pii import mask_pii
from .validation import ValidationReport, validate_invoice

log = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    invoice_id: int
    file_name: str
    status: str
    reason: str
    skipped_duplicate_file: bool = False
    extraction: ExtractionResult | None = None
    report: ValidationReport | None = None
    agent: AgentResult | None = None


def process_file(path: str | Path, client, store: Store, settings: Settings,
                 today: date | None = None) -> ProcessResult:
    path = Path(path)
    doc = load_document(path)

    # 1. Idempotency: the exact same file never gets processed (or paid) twice.
    existing = store.find_by_hash(doc.file_hash)
    if existing:
        store.log(existing["id"], "system", "duplicate_file_skipped", {"file": path.name})
        return ProcessResult(existing["id"], path.name, existing["status"],
                             "Identical file already processed.", skipped_duplicate_file=True)

    # 2. Mask PII before anything leaves our system.
    pii_counts: dict[str, int] = {}
    if doc.text:
        masked = mask_pii(doc.text)
        doc = replace(doc, text=masked.text)
        pii_counts = masked.counts

    # 3. Extract with the LLM.
    ext = extract_invoice(client, doc, settings.extraction_model)
    meta = {
        "model": ext.model, "attempts": ext.attempts, "latency_s": ext.latency_s,
        "input_tokens": ext.input_tokens, "output_tokens": ext.output_tokens,
        "scanned": doc.is_scanned, "error": ext.error,
    }

    # 4. Validate with plain code.
    if ext.invoice is not None:
        report = validate_invoice(ext.invoice, today=today)
        data = ext.invoice.model_dump()
    else:
        report = ValidationReport(issues=[f"Extraction failed: {ext.error}"], confidence=0.0)
        data = {}

    meta["warnings"] = report.warnings
    invoice_id = store.insert_invoice(
        file_name=path.name, file_hash=doc.file_hash, status="processing", data=data,
        issues=report.issues, confidence=report.confidence, pii_counts=pii_counts, extraction_meta=meta,
    )
    store.log(invoice_id, "system", "extracted", {"pii_masked": pii_counts, **meta})
    if report.issues:
        store.log(invoice_id, "system", "validation_issues", report.issues)
    if report.warnings:
        store.log(invoice_id, "system", "validation_warnings", report.warnings)

    # 5. Decide. Some cases never reach the agent at all - deterministic routing first.
    agent_result: AgentResult | None = None
    if ext.invoice is None:
        status, reason = Status.NEEDS_HUMAN_REVIEW, "Could not extract invoice data."
    elif ext.invoice.contains_instructions:
        status = Status.NEEDS_HUMAN_REVIEW
        reason = "Document contains embedded instructions (possible prompt injection); not sent to agent."
    else:
        agent_result = run_agent(client, store, settings, invoice_id, ext.invoice, report)
        status, reason = agent_result.status, agent_result.reason

    trace = agent_result.trace if agent_result else []
    store.set_decision(invoice_id, status.value, reason, trace)
    if status == Status.PENDING_MANAGER_APPROVAL:
        store.add_approval_request(invoice_id, reason)
    store.log(invoice_id, "agent" if agent_result else "system", f"decision:{status.value}", reason)
    log.info("%s -> %s (%s)", path.name, status.value, reason)

    return ProcessResult(invoice_id, path.name, status.value, reason,
                         extraction=ext, report=report, agent=agent_result)


def process_inbox(inbox_dir: str | Path, client, store: Store, settings: Settings) -> list[ProcessResult]:
    from .ingest import list_inbox

    results = []
    for path in list_inbox(inbox_dir):
        try:
            results.append(process_file(path, client, store, settings))
        except Exception as exc:  # one bad file must not stop the batch
            log.exception("Failed on %s", path)
            store.log(None, "system", "processing_error", {"file": path.name, "error": str(exc)})
    return results

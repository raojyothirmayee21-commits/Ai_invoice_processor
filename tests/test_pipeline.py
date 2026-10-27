from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pytest

from invoice_processor.agent import run_agent
from invoice_processor.config import get_settings
from invoice_processor.db import Store, normalize_vendor
from invoice_processor.email_ingest import is_allowed_sender, save_attachments
from invoice_processor.evaluation import run_evaluation
from invoice_processor.extractor import extract_invoice
from invoice_processor.ingest import load_document
from invoice_processor.models import Invoice, LineItem, Status
from invoice_processor.pii import mask_pii
from invoice_processor.pipeline import process_file
from invoice_processor.validation import validate_invoice

from .fakes import DATASET, FakeClient

TODAY = date(2026, 10, 27)
TRUTH = json.loads((DATASET / "ground_truth.json").read_text())


@pytest.fixture
def settings():
    return replace(get_settings(), db_path=Path(":memory:"))


@pytest.fixture
def store(settings):
    s = Store(":memory:")
    s.seed_vendors(settings.vendors_csv)
    return s


def clean_invoice(**over) -> Invoice:
    base = dict(vendor_name="Acme Office Supply, Inc.", invoice_number="A-1", invoice_date="2026-10-01",
                due_date="2026-10-31", currency="USD", subtotal=100.0, tax=8.25, total=108.25,
                line_items=[LineItem(description="Paper", quantity=2, unit_price=50, amount=100)])
    base.update(over)
    return Invoice(**base)


# ---------------------------------------------------------------- PII
def test_pii_masks_bank_details_but_keeps_business_ids():
    text = ("EIN: 47-2913385\nRouting: 121000358 Account No: 4072247045\n"
            "billing@acme.example | (415) 555-1234\nTOTAL DUE $1,534.75")
    r = mask_pii(text)
    assert "4072247045" not in r.text and "121000358" not in r.text
    assert "billing@acme.example" not in r.text and "555-1234" not in r.text
    assert "47-2913385" in r.text and "$1,534.75" in r.text
    assert r.counts == {"ROUTING_NUMBER": 1, "BANK_ACCOUNT": 1, "EMAIL": 1, "PHONE": 1}


def test_pii_masks_ssn_and_card():
    r = mask_pii("SSN 123-45-6789 card 4111 1111 1111 1111")
    assert "123-45-6789" not in r.text and "4111" not in r.text


# ---------------------------------------------------------------- validation
def test_clean_invoice_has_no_issues():
    rep = validate_invoice(clean_invoice(), today=TODAY)
    assert rep.ok and rep.confidence == 1.0


def test_math_error_is_flagged():
    rep = validate_invoice(clean_invoice(total=208.25), today=TODAY)
    assert any("subtotal + tax" in i for i in rep.issues)
    assert rep.confidence < 0.85


def test_missing_fields_and_future_date():
    rep = validate_invoice(clean_invoice(invoice_number=None, invoice_date="2027-01-01", due_date=None), today=TODAY)
    assert any("invoice_number" in i for i in rep.issues)
    assert any("future" in i for i in rep.issues)


def test_injection_flag_zeroes_confidence():
    rep = validate_invoice(clean_invoice(contains_instructions=True), today=TODAY)
    assert rep.confidence == 0.0


# ---------------------------------------------------------------- vendor & duplicates
def test_vendor_normalisation():
    assert normalize_vendor("Acme Office Supply, Inc.") == normalize_vendor("ACME office supply")
    assert normalize_vendor("Cedar & Pine Facilities") == normalize_vendor("Cedar and Pine Facilities LLC")


def test_close_match_is_not_a_match(store):
    res = store.lookup_vendor("Brightline Logistic")
    assert not res["found"] and res["close_matches"][0]["name"] == "Brightline Logistics"


# ---------------------------------------------------------------- ingestion
def test_digital_vs_scanned_detection():
    scanned = [g["file"] for g in TRUTH if "scanned" in g["tags"]]
    digital = next(g["file"] for g in TRUTH if g["tags"] == ["normal"])
    assert load_document(DATASET / digital).text
    for f in scanned:
        d = load_document(DATASET / f)
        assert d.is_scanned and d.images


# ---------------------------------------------------------------- extraction retry
def test_extraction_retries_once_on_bad_output():
    doc = load_document(DATASET / TRUTH[0]["file"])
    client = FakeClient(broken_first_extraction=True)
    res = extract_invoice(client, doc, "fake-model")
    assert res.invoice is not None and res.attempts == 2
    assert "invalid" in client.messages.calls[1]["messages"][-1]["content"]


# ---------------------------------------------------------------- guardrails
def test_rogue_agent_cannot_approve_bad_invoice(store, settings):
    inv = clean_invoice(vendor_name="Totally Unknown Corp", total=99999.0, subtotal=99990.75, tax=8.25,
                        line_items=[LineItem(description="x", amount=99990.75)])
    rep = validate_invoice(inv, today=TODAY)
    inv_id = store.insert_invoice(file_name="x.pdf", file_hash="h", status="processing", data=inv.model_dump(),
                                  issues=rep.issues, confidence=rep.confidence, pii_counts={}, extraction_meta={})
    res = run_agent(FakeClient(rogue=True), store, settings, inv_id, inv, rep)
    assert res.status == Status.NEEDS_HUMAN_REVIEW and res.used_fallback
    refusal = next(t for t in res.trace if t["type"] == "tool")
    assert "vendor is not on the approved vendor list" in refusal["output"]["error"]
    assert "exceeds auto-approval threshold" in refusal["output"]["error"]


def test_rogue_agent_can_approve_genuinely_clean_invoice(store, settings):
    inv = clean_invoice()
    rep = validate_invoice(inv, today=TODAY)
    inv_id = store.insert_invoice(file_name="ok.pdf", file_hash="h2", status="processing", data=inv.model_dump(),
                                  issues=[], confidence=1.0, pii_counts={}, extraction_meta={})
    res = run_agent(FakeClient(rogue=True), store, settings, inv_id, inv, rep)
    assert res.status == Status.READY_FOR_PAYMENT


# ---------------------------------------------------------------- pipeline
def test_same_file_is_never_processed_twice(store, settings):
    client = FakeClient()
    f = DATASET / TRUTH[0]["file"]
    first = process_file(f, client, store, settings, today=TODAY)
    second = process_file(f, client, store, settings, today=TODAY)
    assert second.skipped_duplicate_file and second.invoice_id == first.invoice_id
    assert len(store.list_invoices()) == 1


def test_injection_invoice_never_reaches_agent(store, settings):
    gt = next(g for g in TRUTH if "injection" in g["tags"])
    client = FakeClient()
    res = process_file(DATASET / gt["file"], client, store, settings, today=TODAY)
    assert res.status == Status.NEEDS_HUMAN_REVIEW.value and res.agent is None
    assert all("output_config" in c for c in client.messages.calls)


def test_masked_text_is_what_the_llm_sees(store, settings):
    gt = next(g for g in TRUTH if g["tags"] == ["normal"])
    client = FakeClient()
    process_file(DATASET / gt["file"], client, store, settings, today=TODAY)
    sent = client.messages.calls[0]["messages"][0]["content"][0]["text"]
    assert "@" not in sent and "Account No: 4" not in sent


def test_end_to_end_eval_with_oracle_extractor(settings):
    """With perfect extraction, routing must match the labels exactly and nothing unsafe gets through."""
    r = run_evaluation(FakeClient(), DATASET, settings, today=TODAY)
    assert r["unsafe_auto_approvals"] == 0
    assert r["routing_accuracy"] == 1.0, r["failures"]
    assert r["field_accuracy"]["total"] == 1.0


# ---------------------------------------------------------------- email
def test_email_allowlist_and_safe_attachment_names(tmp_path):
    assert is_allowed_sender("Billing <ar@acmeoffice.com>", {"acmeoffice.com"})
    assert is_allowed_sender("ar@mail.acmeoffice.com", {"acmeoffice.com"})
    assert not is_allowed_sender("ar@acmeoffice.com.evil.io", {"acmeoffice.com"})

    msg = EmailMessage()
    msg["From"] = "ar@acmeoffice.com"
    msg.set_content("invoice attached")
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="../../etc/inv 1.pdf")
    msg.add_attachment(b"MZ", maintype="application", subtype="octet-stream", filename="run.exe")
    saved = save_attachments(msg, tmp_path, "m1")
    assert [p.name for p in saved] == ["m1_inv_1.pdf"]
    assert saved[0].parent == tmp_path


# ---------------------------------------------------------------- uncertainty tiers
def test_minor_uncertainty_warns_but_does_not_block():
    rep = validate_invoice(clean_invoice(uncertain_fields=["currency", "due_date"]), today=TODAY)
    assert rep.ok and rep.warnings and rep.confidence >= 0.85


def test_critical_uncertainty_blocks():
    rep = validate_invoice(clean_invoice(uncertain_fields=["total"]), today=TODAY)
    assert not rep.ok and any("critical" in i for i in rep.issues)

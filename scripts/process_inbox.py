"""Process every invoice in the inbox folder (optionally pulling from email first).

Usage:
  python scripts/process_inbox.py                      # process data/inbox
  python scripts/process_inbox.py --inbox data/sample_invoices
  python scripts/process_inbox.py --fetch-email        # pull from AP mailbox first

Meant to be run on a schedule (cron, Airflow, a cloud function).
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from invoice_processor.config import get_client, get_settings  # noqa: E402
from invoice_processor.db import Store  # noqa: E402
from invoice_processor.pipeline import process_inbox  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default=str(settings.inbox_dir))
    ap.add_argument("--fetch-email", action="store_true")
    args = ap.parse_args()

    store = Store(settings.db_path)
    store.seed_vendors(settings.vendors_csv)

    if args.fetch_email:
        from invoice_processor.email_ingest import fetch_invoice_attachments

        with open(settings.vendors_csv, newline="") as f:
            domains = {r["email_domain"] for r in csv.DictReader(f) if r.get("email_domain")}
        rep = fetch_invoice_attachments(args.inbox, domains)
        print(f"Saved {len(rep.saved)} attachments; quarantined {len(rep.quarantined)} emails")
        for q in rep.quarantined:
            store.log(None, "email_ingest", "quarantined_sender", q)

    results = process_inbox(args.inbox, get_client(), store, settings)
    print("\nSummary:", dict(Counter(r.status for r in results)))


if __name__ == "__main__":
    main()

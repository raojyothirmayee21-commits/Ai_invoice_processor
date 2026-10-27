"""Pull invoice attachments from a DEDICATED accounts-payable mailbox over IMAP.

Design:
* Reads one shared inbox (e.g. invoices@company.com), never personal mailboxes.
* Only senders on an allowlist (known vendor domains) are processed; everything else is
  reported as quarantined for a human to look at.
* Attachments are saved to the inbox folder; the normal pipeline takes it from there.
* Messages are marked as read only after their attachments are saved.

Run on a schedule (cron / Airflow / a cloud function), e.g. every 10 minutes.
Credentials come from env vars; use an app password or OAuth, never a personal password.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from dataclasses import dataclass, field
from email.message import Message
from email.utils import parseaddr
from pathlib import Path

ALLOWED_ATTACHMENTS = {".pdf", ".png", ".jpg", ".jpeg"}


@dataclass
class FetchReport:
    saved: list[Path] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)  # "sender: subject"


def sender_domain(from_header: str) -> str:
    addr = parseaddr(from_header)[1].lower()
    return addr.rsplit("@", 1)[-1] if "@" in addr else ""


def is_allowed_sender(from_header: str, allowed_domains: set[str]) -> bool:
    domain = sender_domain(from_header)
    return any(domain == d or domain.endswith("." + d) for d in allowed_domains)


def _safe_name(name: str) -> str:
    name = Path(name).name  # strip any path components an attacker might add
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "attachment"


def save_attachments(msg: Message, dest_dir: Path, prefix: str) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename or Path(filename).suffix.lower() not in ALLOWED_ATTACHMENTS:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        target = dest_dir / f"{prefix}_{_safe_name(filename)}"
        target.write_bytes(payload)
        saved.append(target)
    return saved


def fetch_invoice_attachments(dest_dir: str | Path, allowed_domains: set[str],
                              mailbox: str = "INBOX") -> FetchReport:
    host = os.environ["AP_IMAP_HOST"]
    user = os.environ["AP_IMAP_USER"]
    password = os.environ["AP_IMAP_PASSWORD"]
    report = FetchReport()

    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(mailbox)
        _, data = imap.search(None, "UNSEEN")
        for num in data[0].split():
            # BODY.PEEK so the message is NOT marked read until we've handled it.
            _, msg_data = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msg_data[0][1])
            sender, subject = msg.get("From", ""), msg.get("Subject", "")
            if not is_allowed_sender(sender, allowed_domains):
                report.quarantined.append(f"{sender}: {subject}")
                continue
            report.saved += save_attachments(msg, Path(dest_dir), prefix=f"mail{num.decode()}")
            imap.store(num, "+FLAGS", "\\Seen")
    return report

"""Central configuration. Everything tunable lives here and can be overridden with env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Tiny .env loader so we don't need an extra dependency. Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    # Extraction needs the stronger model (reads messy documents); the agent only
    # reasons over clean JSON, so a smaller, cheaper model is enough.
    extraction_model: str
    agent_model: str
    # Invoices above this amount need a manager's sign-off.
    approval_threshold: float
    # Below this validation confidence, an invoice can never be auto-approved.
    min_confidence_for_auto: float
    db_path: Path
    inbox_dir: Path
    vendors_csv: Path


def get_settings() -> Settings:
    _load_dotenv()
    return Settings(
        extraction_model=os.getenv("EXTRACTION_MODEL", "claude-sonnet-5-5"),
        agent_model=os.getenv("AGENT_MODEL", "claude-haiku-5-5"),
        approval_threshold=float(os.getenv("APPROVAL_THRESHOLD", "5000")),
        min_confidence_for_auto=float(os.getenv("MIN_CONFIDENCE_FOR_AUTO", "0.85")),
        db_path=Path(os.getenv("DB_PATH", str(ROOT / "data" / "invoices.db"))),
        inbox_dir=Path(os.getenv("INBOX_DIR", str(ROOT / "data" / "inbox"))),
        vendors_csv=Path(os.getenv("VENDORS_CSV", str(ROOT / "data" / "vendors.csv"))),
    )


def get_client():
    """Return an Anthropic client. Imported lazily so tests never need the SDK or a key."""
    _load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    import anthropic

    return anthropic.Anthropic()

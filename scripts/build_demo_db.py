"""Process the sample invoices with the REAL Claude API and save the results as the
demo database used by the hosted app (data/demo.db).

The same run also writes the evaluation report (evals/results/latest.{json,md}),
so the Evaluation tab and the demo show the exact same results.

Usage:  python scripts/build_demo_db.py      (about 35 extraction + agent calls)
Then commit data/demo.db and evals/results/latest.*
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from invoice_processor.config import get_client, get_settings  # noqa: E402
from invoice_processor.db import Store  # noqa: E402
from invoice_processor.evaluation import run_evaluation, to_markdown  # noqa: E402


def main() -> None:
    settings = get_settings()
    client = get_client()  # fails fast with a clear message if no key

    demo_db = ROOT / "data" / "demo.db"
    demo_db.unlink(missing_ok=True)
    store = Store(demo_db)
    store.seed_vendors(settings.vendors_csv)

    result = run_evaluation(client, ROOT / "data" / "sample_invoices", settings, store=store)
    store.conn.close()

    out = ROOT / "evals" / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(result, indent=2))
    (out / "latest.md").write_text(to_markdown(result))

    print(f"Demo database: {demo_db}  ({demo_db.stat().st_size // 1024} KB)")
    print("Statuses:", dict(Counter(r["actual_status"] for r in result["rows"])))
    print(to_markdown(result).split("## Field accuracy")[0])


if __name__ == "__main__":
    main()

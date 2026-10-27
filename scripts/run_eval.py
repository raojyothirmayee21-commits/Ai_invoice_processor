"""Run the evaluation and write evals/results/latest.{json,md}.

Usage:  python scripts/run_eval.py [--limit 10]
Costs real API calls (~35 extraction + agent calls on the default dataset).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from invoice_processor.config import get_client, get_settings  # noqa: E402
from invoice_processor.evaluation import run_evaluation, to_markdown  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "data" / "sample_invoices"))
    ap.add_argument("--limit", type=int, default=None, help="only evaluate the first N invoices")
    args = ap.parse_args()

    result = run_evaluation(get_client(), args.dataset, get_settings(), limit=args.limit)
    out = ROOT / "evals" / "results"
    out.mkdir(parents=True, exist_ok=True)
    stamp = result["run_at"].replace(":", "").replace("-", "")[:15]
    for name in ("latest", stamp):
        (out / f"{name}.json").write_text(json.dumps(result, indent=2))
        (out / f"{name}.md").write_text(to_markdown(result))

    print(to_markdown(result).split("## Failures")[0])
    print(f"Full report: {out / 'latest.md'}")


if __name__ == "__main__":
    main()

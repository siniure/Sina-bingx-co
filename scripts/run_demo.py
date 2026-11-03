#!/usr/bin/env python3
"""Run a synthetic local demonstration, leave the journal PAUSED, send nothing."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import STOP
from oco.demo import synthetic_snapshot
from oco.formatting import report_fa
from oco.service import Service, load_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("demo-result"))
    parser.add_argument("--direction", choices=("long", "short", "neutral"), default="long")
    args = parser.parse_args()
    service = Service(args.output_dir, load_json(ROOT/"config/strategy.research.json"))
    service.control("paper-start")
    snapshot = synthetic_snapshot(args.direction)
    try:
        (args.output_dir/"input.synthetic.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        result, receipt = service.step(snapshot, replay_clock=True)
        (args.output_dir/"research_report_FA.md").write_text(report_fa(result), encoding="utf-8")
        (args.output_dir/"research_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        final = service.control(STOP)
    print(json.dumps({"data": "SYNTHETIC_NOT_BINGX_PRICES", "decision": result["decision"],
                      "receipt": receipt["status"], "final_state": final["state"],
                      "external_delivery_attempted": False, "exchange_orders_submitted": False}, indent=2))


if __name__ == "__main__":
    main()

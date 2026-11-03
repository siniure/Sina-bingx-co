"""CLI defaults to status. Live start is always blocked in this release."""
import argparse
import json
import signal
import threading
from pathlib import Path

from core import GateError, START, STOP
from . import __version__
from .service import Service, load_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="status", choices=("status", "replay", "worker", "paper-start", "stop", "live-start"))
    parser.add_argument("--data-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent/"config/strategy.research.json")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    try:
        service = Service(args.data_dir, load_json(args.config))
        if args.action == "status":
            result = {"version": __version__, **service.journal.status()}
        elif args.action in {"paper-start", "stop", "live-start"}:
            command = {"paper-start": "paper-start", "stop": STOP, "live-start": START}[args.action]
            result = service.control(command)
        else:
            if args.snapshot is None:
                parser.error("--snapshot is required for replay/worker")
            if args.action == "replay":
                evaluation, receipt = service.step(load_json(args.snapshot), replay_clock=True)
                result = {"status": evaluation["status"], "decision": evaluation["decision"], "receipt": receipt,
                          "report": str(args.data_dir/"latest_report_FA.md")}
            else:
                if not 1 <= args.interval <= 15:
                    parser.error("--interval must be 1..15 seconds")
                stop = threading.Event()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: stop.set())
                failures = 0
                while not stop.is_set():
                    try:
                        evaluation, receipt = service.step(load_json(args.snapshot))
                        failures = 0
                        message = {"status": evaluation["status"], "receipt": receipt["status"]}
                    except (OSError, ValueError, GateError) as exc:
                        failures += 1
                        service.fault("INPUT_UNAVAILABLE")
                        # Never echo credentials, source URLs or raw untrusted responses.
                        message = {"status": "INPUT_UNAVAILABLE", "error_class": type(exc).__name__}
                    print(json.dumps(message), flush=True)
                    stop.wait(min(30, args.interval*(2**min(failures, 3))))
                result = service.control(STOP)
    except (GateError, ValueError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

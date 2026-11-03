"""Local-file replay worker. Does not connect to an exchange or notification API."""
import json
from datetime import datetime
from pathlib import Path

from core import GateError, STOP, atomic_json, clock_value
from .engine import evaluate
from .formatting import report_fa
from .journal import Journal
from .market import fresh

MAX_SNAPSHOT_BYTES = 32*1024*1024


def load_json(path):
    path = Path(path)
    # Read bounded bytes rather than trusting stat() during a concurrent replace.
    with path.open("rb") as handle:
        raw = handle.read(MAX_SNAPSHOT_BYTES+1)
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise GateError("snapshot exceeds size limit")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise GateError("duplicate JSON field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(GateError("nonfinite JSON number")))


class Service:
    def __init__(self, directory, settings):
        self.directory = Path(directory)
        self.settings = settings
        self.journal = Journal(directory)

    def step(self, snapshot, *, now=None, replay_clock=False):
        wall = clock_value(now)
        if not replay_clock:
            try:
                fresh(snapshot.get("as_of"), wall, "snapshot as_of")
            except (GateError, AttributeError) as exc:
                return self.fault("STALE_OR_MALFORMED_SNAPSHOT", now=wall)
        result = evaluate(snapshot, self.settings)
        recorded_at = clock_value(datetime.fromisoformat(snapshot["as_of"])) if replay_clock and result.get("as_of") else wall
        receipt = self.journal.record(result, now=recorded_at)
        if result.get("signal") and receipt["status"] not in {"LOCAL_RESEARCH_PREVIEW_SAVED", "DUPLICATE"}:
            result = {**result, "signal": None, "decision": "NO_ENTRY", "reason": receipt["status"]}
        result = self._save_latest(result)
        return result, receipt

    def _save_latest(self, result):
        with self.journal.transaction() as connection:
            state = connection.execute("SELECT state FROM control").fetchone()[0]
            candidate = result.get("signal")
            if candidate:
                row = connection.execute("SELECT state FROM signals WHERE id=?", (candidate["id"],)).fetchone()
                if state != "REPLAY_RUNNING" or row is None or row[0] != "PREVIEW_SAVED":
                    result = {**result, "signal": None, "decision": "NO_ENTRY", "reason": "PREVIEW_WITHDRAWN_OR_PAUSED"}
            atomic_json(self.directory/"latest_evaluation.json", result)
            # A report file is a local preview; no notification is sent.
            (self.directory/"latest_report_FA.md").write_text(report_fa(result), encoding="utf-8")
        return result

    def control(self, command):
        receipt = self.journal.control(command, trusted_operator=True)
        if receipt["state"] == "PAUSED":
            self._save_latest({"status": "PAUSED", "decision": "NO_ENTRY", "signal": None,
                               "mode": "REPLAY_ONLY", "reason": "OPERATOR_PAUSED"})
        return receipt

    def fault(self, reason, *, now=None):
        self.control(STOP)
        result = {"status": "BLOCKED", "decision": "NO_ENTRY", "signal": None,
                  "mode": "REPLAY_ONLY", "reason": reason, "live_activation_available": False,
                  "source_authenticated": False, "exchange_orders_submitted": False,
                  "external_delivery_attempted": False, "position_or_fill_inferred": False}
        receipt = self.journal.record(result, now=clock_value(now))
        result = self._save_latest(result)
        return result, receipt

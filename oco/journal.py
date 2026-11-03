"""SQLite paper journal. STOP and writes serialize across processes.

No external sink exists. PREVIEW_SAVED means a local research file only.
Terminal setup identities survive restarts and are never revived.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from core import GateError, START, STOP, clock_value, normalize_command, time_value


def encode(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "journal.sqlite3"
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    state TEXT NOT NULL, generation INTEGER NOT NULL);
                INSERT OR IGNORE INTO control VALUES (1, 'PAUSED', 0);
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, expires_at TEXT NOT NULL,
                    payload TEXT NOT NULL, generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS terminal (
                    id TEXT PRIMARY KEY, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY, event TEXT NOT NULL, details TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def status(self):
        with self.connection() as connection:
            control = dict(connection.execute("SELECT * FROM control").fetchone())
            counts = {row[0]: row[1] for row in connection.execute("SELECT state, count(*) FROM signals GROUP BY state")}
        return {"state": control["state"], "generation": control["generation"], "paper_records": counts,
                "live_activation_available": False, "exchange_order_execution": False,
                "external_delivery_connected": False}

    def expire_pending(self, *, now=None):
        """Expiry must progress even while no new market observations arrive."""
        now = clock_value(now)
        with self.transaction() as connection:
            for row in connection.execute("SELECT id, expires_at FROM signals WHERE state='PREVIEW_SAVED'").fetchall():
                if time_value(row['expires_at'], 'expiry') <= now:
                    connection.execute("UPDATE signals SET state='EXPIRED' WHERE id=?", (row['id'],))
                    connection.execute("INSERT OR IGNORE INTO terminal VALUES (?, 'EXPIRED')", (row['id'],))

    def control(self, command, *, trusted_operator=False):
        if not trusted_operator:
            raise GateError("trusted local operator required")
        command = normalize_command(command)
        if command == START:
            raise GateError("LIVE_BLOCKED: verified Standard feed, host, delivery and user approval are required; live activation is not implemented")
        if command not in {"paper-start", STOP}:
            raise GateError("exact paper-start or standalone stop command required")
        with self.transaction() as connection:
            state = "REPLAY_RUNNING" if command == "paper-start" else "PAUSED"
            connection.execute("UPDATE control SET state=?, generation=generation+1", (state,))
            if state == "PAUSED":
                connection.execute("INSERT OR IGNORE INTO terminal SELECT id, 'STOPPED' FROM signals")
                connection.execute("UPDATE signals SET state='CANCELLED' WHERE state='PREVIEW_SAVED'")
            connection.execute("INSERT INTO audit(event, details) VALUES ('CONTROL', ?)", (state,))
        return self.status()

    @staticmethod
    def _immutable(path: Path, data: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
                if os.name == "posix":
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            except FileExistsError:
                if path.read_text(encoding="utf-8") != data:
                    raise GateError("immutable preview conflict; inspect local archive")
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def record(self, evaluation, *, now=None):
        now = clock_value(now)
        payload = encode(evaluation)
        evaluation_id = hashlib.sha256(payload.encode()).hexdigest()
        with self.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO evaluations VALUES (?,?)", (evaluation_id, payload))
            # Expiry and invalidation are durable even while the worker is paused.
            for row in connection.execute("SELECT id, expires_at FROM signals WHERE state='PREVIEW_SAVED'").fetchall():
                if time_value(row["expires_at"], "expiry") <= now:
                    connection.execute("UPDATE signals SET state='EXPIRED' WHERE id=?", (row["id"],))
                    connection.execute("INSERT OR IGNORE INTO terminal VALUES (?, 'EXPIRED')", (row["id"],))
            cancelled = evaluation.get("cancelled_signal_id")
            if cancelled:
                connection.execute("INSERT OR IGNORE INTO terminal VALUES (?, 'INVALIDATED_OR_PATH_MISSING')", (cancelled,))
                connection.execute("UPDATE signals SET state='INVALIDATED' WHERE id=?", (cancelled,))
            signal = evaluation.get("signal")
            if not signal or evaluation.get("status") != "SIMULATED":
                connection.execute("INSERT OR IGNORE INTO terminal SELECT id, 'WITHDRAWN_ON_FAILED_RECHECK' FROM signals WHERE state='PREVIEW_SAVED'")
                connection.execute("UPDATE signals SET state='CANCELLED' WHERE state='PREVIEW_SAVED'")
                return {"status": "EVALUATION_RECORDED", "evaluation_id": evaluation_id}
            if evaluation.get("mode") != "REPLAY_ONLY" or evaluation.get("source_authenticated") is not False:
                raise GateError("journal accepts explicitly unauthenticated replay only")
            control = connection.execute("SELECT * FROM control").fetchone()
            if control["state"] != "REPLAY_RUNNING":
                return {"status": "PAUSED", "evaluation_id": evaluation_id}
            signal_id = signal["id"]
            if connection.execute("SELECT 1 FROM terminal WHERE id=?", (signal_id,)).fetchone():
                return {"status": "TERMINAL_SETUP", "id": signal_id}
            if time_value(signal["expires_at"], "expiry") <= now:
                connection.execute("INSERT OR IGNORE INTO terminal VALUES (?, 'EXPIRED')", (signal_id,))
                return {"status": "EXPIRED", "id": signal_id}
            if not -2 <= (now-time_value(evaluation["as_of"], "evaluation time")).total_seconds() <= 15:
                return {"status": "STALE_EVALUATION", "id": signal_id}
            if connection.execute("SELECT 1 FROM signals WHERE id=?", (signal_id,)).fetchone():
                return {"status": "DUPLICATE", "id": signal_id}
            if connection.execute("SELECT 1 FROM signals WHERE state='PREVIEW_SAVED'").fetchone():
                return {"status": "ACTIVE_RESEARCH_CANDIDATE_EXISTS", "id": signal_id}
            # File write and control check use the same transaction as STOP.
            destination = self.directory/"previews"/(signal_id+".json")
            self._immutable(destination, payload+"\n")
            connection.execute("INSERT INTO signals VALUES (?, 'PREVIEW_SAVED', ?, ?, ?)",
                               (signal_id, signal["expires_at"], payload, control["generation"]))
            connection.execute("INSERT INTO audit(event, details) VALUES ('LOCAL_PREVIEW_SAVED', ?)", (signal_id,))
            return {"status": "LOCAL_RESEARCH_PREVIEW_SAVED", "id": signal_id, "path": str(destination),
                    "external_delivery_attempted": False}

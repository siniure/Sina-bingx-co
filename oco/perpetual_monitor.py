"""Run closed-candle research on the hosted feed and serve its current state."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from core import GateError, atomic_json, time_value
from .collector import check_observation_clock, evaluate_observation, observation_snapshot
from .market import PRODUCT_IDENTITIES, TIMEFRAMES
from .perpetual_market import BingXPublicClient, MarketError, SOURCE_SYMBOL
from .perpetual_stream import LastPriceStream

IDENTITY = PRODUCT_IDENTITIES["perpetual_futures"]


class PerpetualMonitor:
    def __init__(self, directory, settings, *, stream=None, transport=None, clock=None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stream = stream if stream is not None else LastPriceStream(clock=self._clock)
        self._client = BingXPublicClient(transport, self._clock, stream_quote=self._stream_quote)
        self._settings = settings
        self._pause_path = Path(directory) / "perpetual_monitoring_paused.json"
        self._paused = self._pause_path.exists()
        self._stream.set_enabled(not self._paused)
        self._lock = threading.Lock()
        self._report = None
        self._report_generation = None
        self._error = None

    def _stream_quote(self):
        current = self._stream.current()
        if current is None:
            raise MarketError("waiting for a fresh BingX Last Price event")
        _, price, timestamp = current
        return price, timestamp

    def set_paused(self, paused):
        with self._lock:
            if paused:
                self._paused = True
                self._stream.set_enabled(False)
                atomic_json(self._pause_path, {"paused": True})
            else:
                self._pause_path.unlink(missing_ok=True)
                self._stream.set_enabled(True)
                self._paused = False
            self._report = self._report_generation = self._error = None

    def _current_report(self, generation, now):
        if self._report is None or generation != self._report_generation:
            return None
        for tf, seconds in TIMEFRAMES.items():
            recorded = time_value(self._report["timeframes"][tf]["closed_at"], "closed candle")
            if not 0 <= (now - recorded).total_seconds() < seconds:
                return None
        return self._report

    def poll_once(self):
        now = self._clock()
        with self._lock:
            current = self._stream.current()
            if self._paused or current is None:
                return
            generation = current[0]
            if self._current_report(generation, now) is not None:
                return
        try:
            snapshot = observation_snapshot(self._client.collect())
            check_observation_clock(snapshot, self._clock())
            report = evaluate_observation(snapshot, self._settings)
            check_observation_clock(snapshot, self._clock())
            if set(report.get("timeframes", {})) != set(TIMEFRAMES):
                raise MarketError("closed-candle analysis failed")
        except (MarketError, GateError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            with self._lock:
                current = self._stream.current()
                if not self._paused and current is not None and current[0] == generation:
                    self._error = str(exc)
            return
        with self._lock:
            current = self._stream.current()
            if not self._paused and current is not None and current[0] == generation:
                if self._error is not None or self._report is None:
                    print("BingX BTC-USDT Last Price and six closed candle histories received.", flush=True)
                self._report = report
                self._report_generation = generation
                self._error = None

    def status(self):
        with self._lock:
            current = None if self._paused else self._stream.current()
            now = self._clock()
            market = {**IDENTITY, "source_symbol": SOURCE_SYMBOL, "transport": "websocket",
                      "state": "PAUSED" if self._paused else "WAITING_FOR_PRICE",
                      "closed_candles_ready": False}
            if current is not None:
                generation, price, event_at = current
                ready = self._current_report(generation, now) is not None
                market.update(state="OBSERVING_PUBLIC_MARKET" if ready else "WAITING_FOR_CANDLES",
                              closed_candles_ready=ready, price=price, event_at=event_at.isoformat(),
                              age_seconds=round((now - event_at).total_seconds(), 3),
                              history_error=None if ready else self._error)
            return market

    def latest(self):
        with self._lock:
            current = None if self._paused else self._stream.current()
            report = self._current_report(current[0], self._clock()) if current else None
            if report is not None:
                return {**report, "live": False}
            reason = ("MARKET_MONITORING_PAUSED" if self._paused else
                      "WAITING_FOR_CLOSED_CANDLES" if current else "WAITING_FOR_FRESH_LAST_PRICE")
            return {**IDENTITY, "data_mode": "live_observation", "status": "BLOCKED",
                    "signal": None, "decision": "NO_ENTRY", "reason": reason, "live": False}

    def run(self, stop):
        stream_thread = threading.Thread(target=self._stream.run, args=(stop,), daemon=True)
        stream_thread.start()
        try:
            while not stop.is_set():
                self.poll_once()
                stop.wait(2)
        finally:
            self._stream.set_enabled(False)
            stream_thread.join(timeout=3)

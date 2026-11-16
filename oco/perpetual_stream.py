"""Supervise the public BTC-USDT Last Price WebSocket."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .perpetual_market import (
    BingXPublicClient, MarketError, MAX_QUOTE_AGE_SECONDS, SOURCE_SYMBOL, _decimal, _decimal_text,
    _from_milliseconds, _milliseconds,
)

BRIDGE = Path(__file__).resolve().parents[1] / "scripts" / "bingx_last_price.js"


class LastPriceStream:
    def __init__(self, *, clock=None, command=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        node = shutil.which("node")
        self.command = command if command is not None else ([node, str(BRIDGE)] if node else None)
        self._lock = threading.Lock()
        self._process = None
        self._enabled = True
        self._generation = 0
        self._quote = None

    def _invalidate(self):
        self._quote = None
        self._generation += 1

    def set_enabled(self, enabled):
        with self._lock:
            self._enabled = enabled
            if not enabled:
                self._invalidate()
                if self._process is not None and self._process.poll() is None:
                    self._process.terminate()

    def current(self):
        with self._lock:
            if self._quote is None:
                return None
            price, timestamp = self._quote
            try:
                BingXPublicClient._fresh_quote(timestamp, self.clock())
            except MarketError:
                self._invalidate()
                return None
            return self._generation, price, timestamp

    def _accept(self, line, process):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("symbol") != SOURCE_SYMBOL:
                return
            price = _decimal_text(_decimal(event["price"], "stream price"))
            stamp = _from_milliseconds(_milliseconds(event["event_ms"], "stream event time"))
            BingXPublicClient._fresh_quote(stamp, self.clock())
        except (KeyError, ValueError, TypeError):
            return
        with self._lock:
            if self._enabled and self._process is process and (
                    self._quote is None or stamp > self._quote[1]):
                if self._quote is not None and (stamp - self._quote[1]).total_seconds() > MAX_QUOTE_AGE_SECONDS:
                    self._invalidate()
                self._quote = price, stamp

    def run(self, stop):
        if not self.command:
            print("BingX Last Price unavailable: Node.js is missing.", flush=True)
            return
        delay = 1
        # The bridge needs the public network and system certificates only.
        env = {name: os.environ[name] for name in ("SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR")
               if name in os.environ}
        while not stop.is_set():
            with self._lock:
                if self._enabled and not stop.is_set():
                    try:
                        self._process = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                                         stderr=subprocess.DEVNULL, env=env)
                    except OSError:
                        self._process = None
                process = self._process
            if process is None:
                stop.wait(1)
                continue
            try:
                with process.stdout as output:
                    while not stop.is_set() and (line := output.readline(512)):
                        if not line.endswith(b"\n"):
                            break
                        self._accept(line, process)
            finally:
                with self._lock:
                    received = self._quote is not None
                    self._invalidate()
                    self._process = None
                if process.poll() is None:
                    process.kill()
                process.wait()
            delay = 1 if received else min(delay * 2, 30)
            stop.wait(delay)

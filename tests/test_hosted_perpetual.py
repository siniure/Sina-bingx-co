"""Hosted Perpetual regressions with documented public-market frames."""
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

from oco.hub import Hub
from oco.perpetual_market import BOOK_PATH, KLINES_PATH, PRICE_PATH, TIMEFRAMES
from oco.perpetual_monitor import PerpetualMonitor
from oco.perpetual_stream import LastPriceStream
from test_perpetual_market import Fixture

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config" / "strategy.research.json").read_text())


class SimulatedLastPrice:
    def __init__(self, clock):
        self._clock = clock
        self.enabled = True
        self.generation = 0
        self.event_at = clock() - timedelta(milliseconds=109)

    def set_enabled(self, value):
        self.enabled = value
        if not value:
            self.generation += 1
            self.event_at = None

    def current(self):
        if not self.enabled or self.event_at is None:
            return None
        if (self._clock() - self.event_at).total_seconds() > 15:
            return None
        return self.generation, "65279.4", self.event_at


class HostedPerpetualTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.rest = Fixture()
        self.ws = SimulatedLastPrice(self.rest.clock)
        self.monitor = self.make_monitor()

    def make_monitor(self):
        return PerpetualMonitor(self.directory, SETTINGS, stream=self.ws,
                                transport=self.rest.transport, clock=self.rest.clock)

    def test_one_price_stream_and_six_closed_histories(self):
        self.assertEqual(self.monitor.status()["state"], "WAITING_FOR_CANDLES")
        self.assertEqual(self.monitor.status()["price"], "65279.4")
        self.monitor.poll_once()
        market, report = self.monitor.status(), self.monitor.latest()
        self.assertEqual(market["age_seconds"], 0.109)
        self.assertTrue(market["closed_candles_ready"])
        self.assertEqual(report["source_id"], "bingx-usdt-m-perpetual-websocket-last-v1")
        self.assertEqual(set(report["timeframes"]), set(TIMEFRAMES))
        self.assertEqual(report["quote"]["event_at"], self.ws.event_at.isoformat())
        self.assertFalse(report["live"])
        self.assertIsNone(report["signal"])
        paths = [path for path, _ in self.rest.calls]
        self.assertEqual(paths.count(KLINES_PATH), 6)
        self.assertNotIn(PRICE_PATH, paths)
        self.assertNotIn(BOOK_PATH, paths)
        self.rest.now += timedelta(seconds=50)
        self.ws.event_at = self.rest.now
        self.monitor.poll_once()
        self.assertEqual(len(self.rest.calls), len(paths))
        self.assertEqual(self.monitor.latest()["as_of"], report["as_of"])

    def test_next_close_withdraws_old_report_until_latest_close_is_available(self):
        self.monitor.poll_once()
        self.rest.now += timedelta(seconds=290)
        self.ws.event_at = self.rest.now
        self.assertFalse(self.monitor.status()["closed_candles_ready"])
        self.assertNotIn("timeframes", self.monitor.latest())
        self.rest.now += timedelta(seconds=3)
        self.ws.event_at = self.rest.now
        self.monitor.poll_once()
        self.assertTrue(self.monitor.status()["closed_candles_ready"])
        self.assertEqual(sum(path == KLINES_PATH for path, _ in self.rest.calls), 7)

    def test_stale_and_reconnected_stream_cannot_reuse_a_report(self):
        self.monitor.poll_once()
        self.rest.now += timedelta(seconds=16)
        self.assertNotIn("price", self.monitor.status())
        self.assertNotIn("timeframes", self.monitor.latest())
        self.ws.generation += 1
        self.ws.event_at = self.rest.now
        self.assertEqual(self.monitor.status()["state"], "WAITING_FOR_CANDLES")
        self.monitor.poll_once()
        self.assertTrue(self.monitor.status()["closed_candles_ready"])

    def test_stream_reconnect_during_history_fetch_discards_the_old_observation(self):
        def reconnect(path, params, response):
            if path == KLINES_PATH and params["interval"] == "1d":
                self.ws.generation += 1
            return response

        self.rest.hook = reconnect
        self.monitor.poll_once()
        self.assertNotIn("timeframes", self.monitor.latest())
        self.rest.hook = None
        self.monitor.poll_once()
        self.assertTrue(self.monitor.status()["closed_candles_ready"])

    def test_candle_failure_withholds_strategy_without_hiding_fresh_price(self):
        def gap(path, params, response):
            if path == KLINES_PATH and params["interval"] == "12h":
                del response["data"][1]
            return response

        self.rest.hook = gap
        self.monitor.poll_once()
        market = self.monitor.status()
        self.assertFalse(market["closed_candles_ready"])
        self.assertTrue(market["history_error"])
        self.assertEqual(market["price"], "65279.4")
        self.assertIsNone(self.monitor.latest()["signal"])

    def test_daily_candles_use_the_returned_boundary_instead_of_local_midnight(self):
        offset = 8 * 3600

        def shift_daily(path, params, response):
            if path == KLINES_PATH and params["interval"] == "1d":
                for row in response["data"]:
                    row["time"] -= offset * 1000
            return response

        self.rest.hook = shift_daily
        self.monitor.poll_once()
        self.assertTrue(self.monitor.status()["closed_candles_ready"])
        close = self.monitor.latest()["timeframes"]["1d"]["closed_at"]
        self.assertEqual(close, "2026-11-10T16:00:00+00:00")

    def test_mixed_candle_boundaries_are_rejected(self):
        def drift(path, params, response):
            if path == KLINES_PATH and params["interval"] == "1d":
                response["data"][100]["time"] += 1000
            return response

        self.rest.hook = drift
        self.monitor.poll_once()
        self.assertIn("exchange boundary", self.monitor.status()["history_error"])

    def test_stop_persists_and_resume_only_restores_market_observation(self):
        hub = Hub(self.directory, view_token="v" * 40, control_token="c" * 40)
        hub.market = self.monitor
        self.monitor.poll_once()
        hub.stop()
        self.assertEqual(hub.status()["market"]["state"], "PAUSED")
        self.assertFalse(self.ws.enabled)
        hub.market = self.make_monitor()
        self.assertEqual(hub.status()["market"]["state"], "PAUSED")
        resumed = hub.resume_observation()
        self.assertEqual(resumed["state"], "PAUSED")
        self.assertFalse(resumed["live_activation_available"])
        self.assertTrue(self.ws.enabled)

    @unittest.skipUnless(shutil.which("node"), "Node.js required for the WebSocket bridge")
    def test_compressed_ping_and_price_on_the_same_bridge_used_in_production(self):
        fixture = r"""
const assert = require('node:assert/strict');
const { gzipSync } = require('node:zlib');
const { receive } = require('./scripts/bingx_last_price.js');
class SimulatedSocket {
  constructor(url) {
    assert.equal(url, 'wss://open-api-swap.bingx.com/swap-market');
    this.handlers = {};
    this.sent = [];
    globalThis.socket = this;
  }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  send(message) { this.sent.push(message); }
  close() { this.handlers.close(); }
  frame(text) { this.handlers.message({data: gzipSync(text)}); }
}
const events = [];
const stop = receive(SimulatedSocket, quote => events.push(quote), () => {});
try {
  socket.handlers.open();
  assert.equal(JSON.parse(socket.sent[0]).dataType, 'BTC-USDT@lastPrice');
  socket.frame('Ping');
  assert.equal(socket.sent[1], 'Pong');
  const E = Date.now() - 109;
  const quote = {dataType:'BTC-USDT@lastPrice', data:{s:'BTC-USDT', c:65279.4, E}};
  socket.frame(JSON.stringify({...quote, dataType:'BTC-USDT@markPrice'}));
  socket.frame(JSON.stringify({...quote, data:{...quote.data, s:'ETH-USDT'}}));
  socket.frame(JSON.stringify({...quote, data:{...quote.data, E:E-16000}}));
  socket.frame(JSON.stringify({...quote, data:{...quote.data, E:E+2500}}));
  socket.frame(JSON.stringify(quote));
  socket.frame(JSON.stringify(quote));
  assert.deepEqual(events, [{symbol:'BTC-USDT', price:'65279.4', event_ms:E}]);
} finally {
  stop();
}
"""
        subprocess.run(["node", "-e", fixture], cwd=ROOT, check=True, timeout=8)

    @unittest.skipUnless(shutil.which("node"), "Node.js required for the bridge process")
    def test_bridge_process_is_reaped_and_quote_withdrawn_when_stopped(self):
        stamp = int(self.rest.now.timestamp() * 1000)
        event = json.dumps({"symbol": "BTC-USDT", "price": "65279.4", "event_ms": stamp})
        code = f"console.log({json.dumps(event)}); setInterval(() => {{}}, 10000)"
        stream = LastPriceStream(clock=self.rest.clock, command=[shutil.which("node"), "-e", code])
        stop = threading.Event()
        worker = threading.Thread(target=stream.run, args=(stop,), daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while stream.current() is None and time.monotonic() < deadline:
                stop.wait(0.02)
            self.assertEqual(stream.current()[1], "65279.4")
        finally:
            stop.set()
            stream.set_enabled(False)
            worker.join(3)
        self.assertIsNone(stream.current())
        self.assertFalse(worker.is_alive())

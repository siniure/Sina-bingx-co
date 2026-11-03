"""Authenticated local/cloud dashboard API. All market use remains replay-only."""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import signal
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import GateError, START, STOP, time_value
from . import __version__
from .service import Service, load_json, MAX_SNAPSHOT_BYTES
from .telegram_bot import TelegramAPI, OwnerBot, TelegramFailure

ROOT = Path(__file__).resolve().parents[1]


class Hub:
    def __init__(self, directory, *, view_token, control_token, settings=None, telegram_api=None):
        if (not self.valid_token(view_token) or not self.valid_token(control_token) or
                hmac.compare_digest(view_token, control_token)):
            raise GateError("two distinct securely configured tokens of at least 32 characters required")
        self._view_token, self._control_token = view_token, control_token
        self.lock = threading.RLock()
        self.service = Service(directory, settings or load_json(ROOT/"config/strategy.research.json"))
        # A restarted host always requires a new explicit paper-start.
        self.service.control(STOP)
        self.bot = OwnerBot(self.service.journal, telegram_api, self.status, self.stop) if telegram_api else None

    @staticmethod
    def valid_token(token):
        return isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_+=/\-]{32,256}', token) is not None

    def authorized(self, value, control=False):
        if not isinstance(value, str) or not value.startswith("Bearer "):
            return False
        token = value[7:]
        if not self.valid_token(token):
            return False
        return hmac.compare_digest(token, self._control_token) or (not control and hmac.compare_digest(token, self._view_token))

    def status(self):
        self.service.journal.expire_pending()
        state = self.service.journal.status()
        return {**state, "version": __version__, "product": "BingX Standard Futures / Index",
                "symbol": "BTCUSDT", "server_observed_at": datetime.now(timezone.utc).isoformat(),
                "market_source_verified": False, "mode": "REPLAY_ONLY",
                "telegram": self.bot.state() if self.bot else {"configured": False, "owner_paired": False, "delivery_verified": False},
                "activation_blockers": ["Verified Standard Index feed and OHLC", "Real-feed and out-of-sample validation",
                                        "Current complete account risk and fee evidence", "Independent final user activation"]}

    def stop(self):
        with self.lock:
            return self.service.control(STOP)

    def latest(self):
        with self.lock:
            self.service.journal.expire_pending()
            path = self.service.directory/"latest_evaluation.json"
            try:
                result = load_json(path) if path.exists() else {"status": "PAUSED", "decision": "NO_ENTRY", "signal": None, "reason": "NO_VERIFIED_FEED"}
                if not isinstance(result, dict):
                    raise GateError("invalid report")
            except (OSError, ValueError):
                result = {"status": "BLOCKED", "decision": "NO_ENTRY", "signal": None, "reason": "REPORT_UNAVAILABLE"}
            # Client time alone never refreshes the market observation.
            try:
                age = (datetime.now(timezone.utc)-time_value(result.get("as_of"), "as_of")).total_seconds()
            except GateError:
                age = None
            with self.service.journal.transaction() as connection:
                control = connection.execute('SELECT state FROM control').fetchone()[0]
                candidate = result.get('signal')
                active = control == 'REPLAY_RUNNING'
                if candidate:
                    signal_id = candidate.get('id') if isinstance(candidate, dict) else None
                    if not isinstance(signal_id, str):
                        signal_id = None
                    row = connection.execute('SELECT state,payload FROM signals WHERE id=?', (signal_id,)).fetchone()
                    terminal = connection.execute('SELECT 1 FROM terminal WHERE id=?', (signal_id,)).fetchone()
                    active = (active and row is not None and row['state'] == 'PREVIEW_SAVED'
                              and terminal is None and json.loads(row['payload']).get('signal') == candidate)
            if age is None or age > 15 or age < -2 or not active:
                result = {**result, "signal": None, "decision": "NO_ENTRY", "reason": "PAUSED_OR_STALE_REPLAY"}
            return {**result, "market_age_seconds": age, "live": False}

    def records(self):
        self.service.journal.expire_pending()
        with self.service.journal.connection() as connection:
            rows = connection.execute("SELECT id,state,expires_at,payload FROM signals ORDER BY rowid DESC LIMIT 50").fetchall()
        return [{"id": row["id"], "state": row["state"], "expires_at": row["expires_at"],
                 "research_only": True, "evaluation": json.loads(row["payload"])} for row in rows]


class Server(ThreadingHTTPServer):
    daemon_threads = True


def handler_for(hub, web_root=ROOT/"web", *, trust_https_proxy=False):
    web_root = Path(web_root).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "SinaOCO"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            # Never log Authorization, pairing links, body, or token-bearing URLs.
            pass

        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if isinstance(body, (dict,list)) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _api_auth(self, control=False):
            try:
                loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                loopback = False
            if not loopback and (not trust_https_proxy or self.headers.get("X-Forwarded-Proto") != "https"):
                self._send(403, {"error": "HTTPS_REQUIRED"})
                return False
            if not hub.authorized(self.headers.get("Authorization"), control):
                self._send(401, {"error": "AUTHENTICATION_REQUIRED"})
                return False
            origin = self.headers.get("Origin")
            if origin:
                split = urlsplit(origin)
                if split.netloc != self.headers.get("Host") or split.scheme not in {"http", "https"}:
                    self._send(403, {"error": "CROSS_ORIGIN_REQUEST_REFUSED"})
                    return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/healthz":
                self._send(200, {"process": "ok", "live_market_ready": False})
                return
            if path.startswith("/api/"):
                if not self._api_auth():
                    return
                routes = {"/api/status": hub.status, "/api/evaluation": hub.latest, "/api/records": hub.records}
                if path not in routes:
                    self._send(404, {"error": "NOT_FOUND"})
                    return
                self._send(200, routes[path]())
                return
            allowed = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/app.css": "app.css",
                       "/manifest.webmanifest": "manifest.webmanifest", "/sw.js": "sw.js", "/icon.svg": "icon.svg",
                       "/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png"}
            if path not in allowed:
                self._send(404, {"error": "NOT_FOUND"})
                return
            target = web_root/allowed[path]
            if not target.is_file():
                self._send(404, {"error": "ASSET_MISSING"})
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), content_type)

        def do_POST(self):
            if not self._api_auth(control=True):
                return
            path = urlsplit(self.path).path
            if self.headers.get_content_type() != "application/json":
                self._send(415, {"error": "JSON_REQUIRED"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= 4096:
                    self._send(413, {"error": "INVALID_BODY_LENGTH"})
                    return
                raw = self.rfile.read(length)
                request = json.loads(raw or b"{}")
                if request != {}:
                    raise GateError("empty JSON object required for control")
                if path == "/api/control/stop":
                    response = hub.stop()
                elif path == "/api/control/paper-start":
                    with hub.lock:
                        response = hub.service.control("paper-start")
                elif path == "/api/control/live-start":
                    self._send(423, {"error": "LIVE_PUBLICATION_BLOCKED", "exchange_orders_submitted": False})
                    return
                elif path in {"/api/telegram/pair", "/api/telegram/test"}:
                    if hub.bot is None:
                        raise GateError("Telegram token is not configured in host secret settings")
                    response = hub.bot.pairing_link() if path.endswith("pair") else hub.bot.test_delivery()
                else:
                    self._send(404, {"error": "NOT_FOUND"})
                    return
                self._send(200, response)
            except TelegramFailure as exc:
                self._send(409, {"error": exc.code, "retry_after_seconds": exc.retry_after})
            except (ValueError, GateError):
                self._send(409, {"error": "CONTROL_UNAVAILABLE_OR_INVALID_INPUT"})

    return Handler


def main(argv=None):
    def port_number(value):
        try:
            port = int(value)
        except (ValueError, TypeError):
            raise argparse.ArgumentTypeError('port must be an integer from 1 to 65535') from None
        if not 1 <= port <= 65535:
            raise argparse.ArgumentTypeError('port must be an integer from 1 to 65535')
        return port
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=port_number, default=os.environ.get("PORT", "8765"))
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--trust-https-proxy", action="store_true")
    args = parser.parse_args(argv)
    try:
        ZoneInfo('Europe/Istanbul')
        api = TelegramAPI(os.environ.get("TELEGRAM_BOT_TOKEN", "")) if args.telegram else None
        hub = Hub(args.data_dir, view_token=os.environ.get("SINA_OCO_VIEW_TOKEN", ""),
                  control_token=os.environ.get("SINA_OCO_CONTROL_TOKEN", ""), telegram_api=api)
        server = Server((args.host, args.port), handler_for(hub, trust_https_proxy=args.trust_https_proxy))
    except ZoneInfoNotFoundError:
        print('Hub not started: Europe/Istanbul timezone data is unavailable. Install tzdata in this Python environment.')
        return 2
    except (GateError, OSError, sqlite3.Error):
        print("Hub not started: configure distinct access tokens and any requested Telegram token in secure host settings.")
        print("Also check the writable data directory and that the configured port is available.")
        return 2
    stop = threading.Event()
    def background():
        while not stop.is_set():
            try:
                if args.snapshot:
                    with hub.lock:
                        hub.service.step(load_json(args.snapshot))
            except (OSError, ValueError):
                with hub.lock:
                    hub.service.fault("INPUT_UNAVAILABLE")
            stop.wait(5)
    def telegram_loop():
        while not stop.is_set():
            try:
                hub.bot.poll_once()
            except TelegramFailure as exc:
                stop.wait(exc.retry_after)
            except (OSError, ValueError):
                stop.wait(5)
    threads = [threading.Thread(target=background, daemon=True)]
    if hub.bot:
        threads.append(threading.Thread(target=telegram_loop, daemon=True))
    for thread in threads:
        thread.start()
    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)
    try:
        print("Sina OCO hub ready; market publication remains blocked.", flush=True)
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        hub.stop()
        server.server_close()
        for thread in threads:
            thread.join(timeout=26)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

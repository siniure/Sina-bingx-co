"""Owner-paired Telegram status/stop adapter. Never publishes live signals.

Only getMe/getWebhookInfo/getUpdates/sendMessage are allowed. No BotFather account automation or
Telegram personal-account session is attempted. The host injects the bot token.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.request

from core import GateError, START, STOP, normalize_command
from .standard_account import NoRedirect


class TelegramFailure(GateError):
    """Safe diagnostic categories; never include Telegram's raw description/URL."""
    def __init__(self, code, retry_after=5):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


class TelegramAPI:
    METHODS = {"getMe", "getWebhookInfo", "getUpdates", "sendMessage"}

    def __init__(self, token: str, transport=None):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token):
            raise GateError("Telegram bot token is not securely configured")
        self._token = token
        self.credential_fingerprint = hashlib.sha256(token.encode()).hexdigest()
        self._transport = transport

    def call(self, method, payload):
        if method not in self.METHODS:
            raise GateError("Telegram method outside allowlist")
        if self._transport is not None:
            response = self._transport(method, payload)
        else:
            request = urllib.request.Request("https://api.telegram.org/bot"+self._token+"/"+method,
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=25 if method == 'getUpdates' else 5) as handle:
                    raw = handle.read(2*1024*1024+1)
                if len(raw) > 2*1024*1024:
                    raise GateError("Telegram response too large")
                response = json.loads(raw)
            except urllib.error.HTTPError as error:
                # HTTP errors can still carry Telegram's structured retry_after.
                number = error.code
                try:
                    raw = error.read(2*1024*1024+1)
                    response = json.loads(raw) if len(raw) <= 2*1024*1024 else {}
                    if not isinstance(response, dict):
                        response = {}
                except (OSError, ValueError):
                    response = {}
                finally:
                    error.close()
                response = {**response, 'ok': False, 'error_code': number}
            except (urllib.error.URLError, OSError, ValueError):
                # Request exceptions can include the token-bearing URL: never echo them.
                raise TelegramFailure('TELEGRAM_NETWORK_UNAVAILABLE') from None
        if not isinstance(response, dict) or response.get("ok") is not True:
            number = response.get('error_code') if isinstance(response, dict) else None
            if type(number) is not int:
                number = None
            codes = {401: 'TELEGRAM_UNAUTHORIZED', 403: 'TELEGRAM_RECIPIENT_FORBIDDEN',
                     409: 'TELEGRAM_POLLING_CONFLICT', 429: 'TELEGRAM_RATE_LIMITED'}
            delay = 5
            if number == 429:
                parameters = response.get('parameters', {})
                value = parameters.get('retry_after') if isinstance(parameters, dict) else None
                if type(value) is int and value > 0:
                    delay = max(5, value)
            raise TelegramFailure(codes.get(number, 'TELEGRAM_API_UNCONFIRMED'), delay)
        return response.get("result")


class OwnerBot:
    def __init__(self, journal, api, status_callback, stop_callback, clock=None):
        self.journal, self.api = journal, api
        self.status_callback, self.stop_callback = status_callback, stop_callback
        self.clock = clock or time.time
        with journal.connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS telegram_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        with journal.transaction() as connection:
            previous = self._get(connection, "credential_fingerprint")
            if previous != api.credential_fingerprint:
                connection.execute("DELETE FROM telegram_state")
                self._set(connection, "credential_fingerprint", api.credential_fingerprint)

    @staticmethod
    def _get(connection, key, default=None):
        row = connection.execute("SELECT value FROM telegram_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(connection, key, value):
        connection.execute("INSERT INTO telegram_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, json.dumps(value)))

    def state(self):
        with self.journal.connection() as connection:
            paired = self._get(connection, "owner")
            return {"configured": True, "owner_paired": paired is not None,
                    "delivery_verified": self._get(connection, "delivery_verified", False),
                    "live_signal_publication": False}

    def pairing_link(self):
        bot = self.api.call("getMe", {})
        if not isinstance(bot, dict) or bot.get("is_bot") is not True or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot.get("username", "")):
            raise GateError("Telegram bot identity was not verified")
        code = secrets.token_urlsafe(24)
        with self.journal.transaction() as connection:
            if self._get(connection, "owner") is not None:
                raise GateError("Telegram owner is already paired")
            self._set(connection, "pair_hash", hashlib.sha256(code.encode()).hexdigest())
            self._set(connection, "pair_expiry", self.clock()+300)
        return {"url": "https://t.me/"+bot["username"]+"?start="+code, "expires_in_seconds": 300}

    def _send(self, owner, text):
        result = self.api.call("sendMessage", {"chat_id": owner["chat_id"], "text": text[:4000],
                                              "protect_content": True})
        if (not isinstance(result, dict) or type(result.get("message_id")) is not int
                or result.get("chat", {}).get("id") != owner["chat_id"]):
            raise GateError("Telegram delivery receipt does not match verified owner")
        with self.journal.transaction() as connection:
            self._set(connection, "delivery_verified", True)
            self._set(connection, "last_message_id", result["message_id"])
        return result["message_id"]

    def test_delivery(self):
        with self.journal.connection() as connection:
            owner = self._get(connection, "owner")
        if owner is None:
            raise GateError("Telegram owner pairing required")
        message_id = self._send(owner, "آزمون اتصال OCO سینا موفق است. انتشار سیگنال زنده غیرفعال است؛ سفارش واقعی ارسال نشده است.")
        return {"status": "NON_TRADING_TEST_DELIVERED", "message_id": message_id}

    def process(self, update):
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            return "IGNORED"
        update_id = update["update_id"]
        message = update.get("message", {})
        # Fresh private owner messages only; forwarded text never controls a service.
        valid = (isinstance(message, dict) and isinstance(message.get("text"), str)
                 and isinstance(message.get("chat"), dict) and message["chat"].get("type") == "private"
                 and isinstance(message.get("from"), dict) and message["from"].get("is_bot") is False
                 and type(message["from"].get("id")) is int and type(message["chat"].get("id")) is int
                 and message["from"]["id"] == message["chat"]["id"]
                 and type(message.get("date")) is int and -2 <= self.clock()-message["date"] <= 120
                 and not message.get("forward_origin") and not message.get("via_bot"))
        owner = None
        action = "IGNORED"
        with self.journal.transaction() as connection:
            offset = self._get(connection, "offset", 0)
            if update_id < offset:
                return "DUPLICATE"
            # At-most-once command handling. Lost receipt is not retried blindly.
            self._set(connection, "offset", update_id+1)
            if not valid:
                return "IGNORED"
            text = message["text"].strip()
            owner = self._get(connection, "owner")
            if owner is None:
                expected = self._get(connection, "pair_hash", "")
                expiry = self._get(connection, "pair_expiry", 0)
                candidate = text[7:] if text.startswith("/start ") else ""
                digest = hashlib.sha256(candidate.encode()).hexdigest()
                if not candidate or self.clock() >= expiry or not expected or not hmac.compare_digest(digest, expected):
                    return "IGNORED"
                owner = {"user_id": message["from"]["id"], "chat_id": message["chat"]["id"]}
                self._set(connection, "owner", owner)
                self._set(connection, "pair_hash", "")
                self._set(connection, "delivery_verified", False)
                action = "PAIRED"
            elif owner["user_id"] != message["from"]["id"] or owner["chat_id"] != message["chat"]["id"]:
                return "IGNORED"
            elif normalize_command(text) in {"/stop", STOP}:
                action = "STOP"
            elif normalize_command(text) in {"/start", START}:
                action = "LIVE_BLOCKED"
            elif text in {"/status", "/help"}:
                action = "STATUS"
        if action == "PAIRED":
            self._send(owner, "این بات به حساب تلگرام شما متصل شد. OCO زنده غیرفعال است. /status برای وضعیت و /stop برای توقف.")
        elif action == "STOP":
            self.stop_callback()
            self._send(owner, "سامانه متوقف شد. این فرمان هیچ سفارش یا پوزیشن صرافی را تغییر نمی‌دهد.")
        elif action == "LIVE_BLOCKED":
            self._send(owner, "آماده فعال‌سازی زنده نیست. منبع Standard، آزمون واقعی و هماهنگی نهایی هنوز لازم است.")
        elif action == "STATUS":
            status = self.status_callback()
            self._send(owner, "OCO سینا\nوضعیت: "+status["state"]+"\nانتشار زنده: غیرفعال\nداده بازار متصل و تأییدشده: خیر")
        return action

    def poll_once(self):
        with self.journal.connection() as connection:
            offset = self._get(connection, "offset", 0)
        updates = self.api.call("getUpdates", {"offset": offset, "timeout": 20,
                                               "allowed_updates": ["message"], "limit": 50})
        if not isinstance(updates, list):
            raise GateError("Telegram updates response invalid")
        return [self.process(update) for update in updates]

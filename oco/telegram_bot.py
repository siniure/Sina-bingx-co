"""Owner-paired Telegram controls and non-trading channel test delivery."""
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
    METHODS = {"getMe", "getWebhookInfo", "getUpdates", "getChat", "getChatMember", "sendMessage"}

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
            channel = self._get(connection, "channel", {})
            return {"configured": True, "owner_paired": paired is not None,
                    "delivery_verified": self._get(connection, "delivery_verified", False),
                    "channel_paired": bool(channel),
                    "channel_delivery_verified": "test_message_id" in channel,
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

    def _send_message(self, chat_id, text, *, reply_markup=None, channel=False):
        payload = {"chat_id": chat_id, "text": text[:4000], "protect_content": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if channel:
            payload["disable_notification"] = True
        result = self.api.call("sendMessage", payload)
        chat = result.get("chat") if isinstance(result, dict) else None
        if (not isinstance(chat, dict) or type(chat.get("id")) is not int or chat["id"] != chat_id
                or type(result.get("message_id")) is not int or result["message_id"] <= 0
                or (channel and chat.get("type") != "channel")):
            raise GateError("Telegram delivery receipt does not match verified destination")
        return result["message_id"]

    def _send(self, owner, text, *, reply_markup=None):
        message_id = self._send_message(owner["chat_id"], text, reply_markup=reply_markup)
        with self.journal.transaction() as connection:
            self._set(connection, "delivery_verified", True)
            self._set(connection, "last_message_id", message_id)
        return message_id

    def _offer_channel_picker(self, owner, request_id):
        button = {"text": "انتخاب کانال", "request_chat": {
            "request_id": request_id, "chat_is_channel": True,
            "chat_is_created": True, "bot_is_member": True,
        }}
        self._send(owner, "بات را با مجوز Post Messages مدیر کانال خودت کن. سپس تا پنج دقیقه دکمهٔ انتخاب کانال را بزن.",
                   reply_markup={"keyboard": [[button]], "resize_keyboard": True, "one_time_keyboard": True})

    def _verify_channel(self, owner, channel_id):
        chat = self.api.call("getChat", {"chat_id": channel_id})
        if (not isinstance(chat, dict) or type(chat.get("id")) is not int
                or chat["id"] != channel_id or chat.get("type") != "channel"):
            raise GateError("Telegram channel identity was not verified")
        bot = self.api.call("getMe", {})
        if (not isinstance(bot, dict) or bot.get("is_bot") is not True
                or type(bot.get("id")) is not int or bot["id"] <= 0):
            raise GateError("Telegram bot identity was not verified")
        for user_id, role in ((bot["id"], "administrator"), (owner["user_id"], "creator")):
            member = self.api.call("getChatMember", {"chat_id": channel_id, "user_id": user_id})
            user = member.get("user") if isinstance(member, dict) else None
            if (not isinstance(user, dict) or type(user.get("id")) is not int
                    or user["id"] != user_id or member.get("status") != role):
                raise GateError("Telegram channel membership was not verified")
            if role == "administrator" and member.get("can_post_messages") is not True:
                raise GateError("Telegram channel posting permission was not verified")
        title = chat.get("title")
        return " ".join(title.split())[:120] if isinstance(title, str) else ""

    def _handle_channel(self, owner, channel_id, *, test):
        if channel_id is None:
            self._send(owner, "ابتدا با /channel کانال خودت را انتخاب کن.")
            return "CHANNEL_REQUIRED"
        channel = {"chat_id": channel_id}
        reply_markup = None if test else {"remove_keyboard": True}
        try:
            title = self._verify_channel(owner, channel_id)
            if test:
                channel["test_message_id"] = self._send_message(
                    channel_id, "آزمون غیرمعاملاتی ارسال پیام کانال SINA OCO", channel=True)
        except GateError as error:
            if isinstance(error, TelegramFailure) and error.code == "TELEGRAM_RATE_LIMITED":
                raise
            self._send(owner, "تلگرام اتصال یا ارسال به کانال را تأیید نکرد. مالکیت کانال و مجوز ارسال بات را بررسی کن.",
                       reply_markup=reply_markup)
            return "CHANNEL_UNCONFIRMED"
        with self.journal.transaction() as connection:
            self._set(connection, "channel", channel)
        if test:
            self._send(owner, "تلگرام رسید پیام آزمایشی را برای کانال تأیید کرد.")
            return "CHANNEL_TEST_DELIVERED"
        name = f" «{title}»" if title else ""
        self._send(owner, f"کانال{name} انتخاب شد. برای ارسال پیام آزمایشی در همین گفت‌وگو /channel_test بفرست.",
                   reply_markup=reply_markup)
        return "CHANNEL_PAIRED"

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
        # Both commands and channel selections must come from a fresh private message.
        valid = (isinstance(message, dict)
                 and (isinstance(message.get("text"), str) or isinstance(message.get("chat_shared"), dict))
                 and isinstance(message.get("chat"), dict) and message["chat"].get("type") == "private"
                 and isinstance(message.get("from"), dict) and message["from"].get("is_bot") is False
                 and type(message["from"].get("id")) is int and type(message["chat"].get("id")) is int
                 and message["from"]["id"] == message["chat"]["id"]
                 and type(message.get("date")) is int and -2 <= self.clock()-message["date"] <= 120
                 and not message.get("forward_origin") and not message.get("via_bot"))
        owner = None
        channel_id = None
        action = "IGNORED"
        with self.journal.transaction() as connection:
            offset = self._get(connection, "offset", 0)
            if update_id < offset:
                return "DUPLICATE"
            # At-most-once command handling. Lost receipt is not retried blindly.
            self._set(connection, "offset", update_id+1)
            if not valid:
                return "IGNORED"
            raw_text = message.get("text")
            text = raw_text.strip() if isinstance(raw_text, str) else ""
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
            elif isinstance(message.get("chat_shared"), dict):
                shared = message["chat_shared"]
                request = self._get(connection, "channel_request", {})
                channel_id = shared.get("chat_id")
                if (type(shared.get("request_id")) is not int or shared["request_id"] != request.get("id")
                        or self.clock() >= request.get("expires_at", 0)
                        or type(channel_id) is not int or not -(2**52) < channel_id < 0):
                    return "IGNORED"
                self._set(connection, "channel_request", {})
                action = "CHANNEL_SHARED"
            elif text == "/channel":
                request_id = secrets.randbelow(2**31 - 1) + 1
                self._set(connection, "channel_request", {"id": request_id, "expires_at": self.clock()+300})
                # A test after starting a new selection must never use the old channel.
                self._set(connection, "channel", {})
                action = "CHANNEL_PICKER"
            elif text == "/channel_test":
                channel = self._get(connection, "channel", {})
                channel_id = channel.get("chat_id")
                if channel_id is not None:
                    self._set(connection, "channel", {"chat_id": channel_id})
                action = "CHANNEL_TEST"
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
        elif action == "CHANNEL_PICKER":
            self._offer_channel_picker(owner, request_id)
        elif action in {"CHANNEL_SHARED", "CHANNEL_TEST"}:
            return self._handle_channel(owner, channel_id, test=action == "CHANNEL_TEST")
        elif action == "STATUS":
            status = self.status_callback()
            channel = self.state()
            channel_status = "انتخاب نشده"
            if channel["channel_delivery_verified"]:
                channel_status = "رسید آزمون تأیید شد"
            elif channel["channel_paired"]:
                channel_status = "انتخاب شد، منتظر آزمون"
            self._send(owner, "OCO سینا\nوضعیت: "+status["state"]+"\nانتشار زنده: غیرفعال\nداده بازار متصل و تأییدشده: خیر"
                       +"\nکانال: "+channel_status+"\n/channel برای انتخاب کانال، /channel_test برای آزمون")
        return action

    def poll_once(self):
        with self.journal.connection() as connection:
            offset = self._get(connection, "offset", 0)
        updates = self.api.call("getUpdates", {"offset": offset, "timeout": 20,
                                               "allowed_updates": ["message"], "limit": 50})
        if not isinstance(updates, list):
            raise GateError("Telegram updates response invalid")
        return [self.process(update) for update in updates]

"""Allowlisted Standard account GET client; not connected by the replay worker.

Reference: BingX-API/api-ai-skills/skills/standard-trade/api-reference.md.
Only positions, historical orders and balance are documented there. Their
presence does not establish complete active-order/stop coverage or Index OHLC.
Credentials must come from the host's secure secret injection, never a chat/file.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from core import GateError

ENDPOINTS = {
    "positions": "/openApi/contract/v1/allPosition",
    "orders": "/openApi/contract/v1/allOrders",
    "balance": "/openApi/contract/v1/balance",
}
BASE_URL = "https://open-api.bingx.com"
REFERENCE = "https://github.com/BingX-API/api-ai-skills/blob/main/skills/standard-trade/api-reference.md"
MAX_BYTES = 2*1024*1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API key or signed query to another host.
        raise GateError("account redirect refused")


def https_get(request):
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            if response.status != 200:
                raise GateError("account HTTP request failed")
            data = response.read(MAX_BYTES+1)
    except urllib.error.HTTPError as exc:
        raise GateError(f"account HTTP status {exc.code}; credentials and URL withheld") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GateError("account network request failed; details withheld") from None
    if len(data) > MAX_BYTES:
        raise GateError("account response exceeds limit")
    return data


class ReadOnlyStandardClient:
    def __init__(self, key: str, secret: str, *, transport=None, clock_ms=None):
        if not isinstance(key, str) or not key or not isinstance(secret, str) or not secret:
            raise GateError("read-only Standard credentials not configured")
        self._key, self._secret = key, secret
        self._transport = transport or https_get
        self._clock_ms = clock_ms or (lambda: int(time.time()*1000))

    @classmethod
    def from_secure_environment(cls, **kwargs):
        return cls(os.environ.get("BINGX_READONLY_API_KEY", ""),
                   os.environ.get("BINGX_READONLY_API_SECRET", ""), **kwargs)

    def query(self, resource: str, *, symbol="BTC-USDT", limit=100):
        if resource not in ENDPOINTS:
            raise GateError("resource outside the three read-only Standard endpoints")
        timestamp = self._clock_ms()
        if type(timestamp) is not int or timestamp <= 0:
            raise GateError("valid millisecond clock required")
        params = {"timestamp": timestamp, "recvWindow": 5000}
        if resource == "orders":
            if symbol != "BTC-USDT" or type(limit) is not int or not 1 <= limit <= 100:
                raise GateError("BTC-USDT historical orders with limit 1..100 only")
            params.update(symbol=symbol, limit=limit)
        canonical = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        query = urllib.parse.urlencode(sorted(params.items()))
        signature = hmac.new(self._secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        request = urllib.request.Request(BASE_URL+ENDPOINTS[resource]+"?"+query+"&signature="+signature,
                                         headers={"X-BX-APIKEY": self._key, "X-SOURCE-KEY": "BX-AI-SKILL",
                                                  "Accept": "application/json"}, method="GET")
        try:
            raw = self._transport(request)
            if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
                raise GateError("invalid account response bytes")
            response = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise GateError("invalid account JSON response") from None
        if not isinstance(response, dict) or type(response.get("code")) is not int or response["code"] != 0:
            raise GateError("account API rejected request; raw response withheld")
        expected = dict if resource == "balance" else list
        if not isinstance(response.get("data"), expected):
            raise GateError("unexpected Standard account response shape")
        return {"provider": "BingX", "product": "standard_futures", "resource": resource,
                "data": response["data"], "requested_at_ms": timestamp,
                "timestamp_is_exchange_event": False, "account_risk_coverage_complete": False,
                "market_data_available": False, "reference": REFERENCE}

"""Public BingX USDT-M Perpetual quotes and closed candles."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from indicators import Candle, CandleError, validate_closed_bars

UTC = timezone.utc
BASE_URL = "https://open-api.bingx.com"
TIME_PATH = "/openApi/swap/v2/server/time"
PRICE_PATH = "/openApi/swap/v1/ticker/price"
BOOK_PATH = "/openApi/swap/v2/quote/bookTicker"
KLINES_PATH = "/openApi/swap/v3/quote/klines"
ALLOWED_PATHS = frozenset((TIME_PATH, PRICE_PATH, BOOK_PATH, KLINES_PATH))
TIMEFRAMES = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "12h": 43200, "1d": 86400}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1000
TIMEOUT_SECONDS = 10
MAX_CLOCK_DRIFT_SECONDS = 5
MAX_QUOTE_AGE_SECONDS = 15
MAX_QUOTE_FUTURE_SECONDS = 2
CANONICAL_SYMBOL = "BTCUSDT"
SOURCE_SYMBOL = "BTC-USDT"


class MarketError(ValueError):
    """The market snapshot is unavailable or does not satisfy its contract."""


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MarketError("HTTP redirects are not allowed")


def _decimal(value: object, field: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MarketError(f"{field}: decimal string or integer required")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MarketError(f"{field}: invalid decimal") from exc
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise MarketError(f"{field}: finite {'positive' if positive else 'nonnegative'} value required")
    if abs(result.as_tuple().exponent) > 64 or not -30 <= result.adjusted() <= 30:
        raise MarketError(f"{field}: decimal outside supported range")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _milliseconds(value: object, field: str) -> int:
    number = _decimal(value, field)
    if number != number.to_integral_value() or number > Decimal("253402300799999"):
        raise MarketError(f"{field}: integer epoch milliseconds required")
    return int(number)


def _from_milliseconds(value: int) -> datetime:
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
    except (OverflowError, ValueError) as exc:
        raise MarketError("timestamp outside the supported date range") from exc


def _to_milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _identity(value: Mapping, symbol: str) -> None:
    expected = {"provider": "BingX", "product": "perpetual_futures", "symbol": symbol}
    for field, wanted in expected.items():
        if field in value and value[field] != wanted:
            raise MarketError(f"{field}: wrong market identity")


class BingXPublicClient:
    """Fetch public prices and closed histories without account or order access.

    ``transport(path, params)`` may be supplied for offline contract fixtures.
    It returns a decoded BingX-style API envelope. Neither fixtures nor a
    successful HTTP request mark this candidate schema as live-verified.
    """

    def __init__(self, transport: Callable | None = None, clock: Callable | None = None,
                 *, stream_quote: Callable | None = None):
        self._transport = transport or self._http_get
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stream_quote = stream_quote
        self._pace_network = transport is None
        self._next_history_request = 0.0
        self._history_cache: dict[tuple, tuple[int, int, tuple[dict, ...]]] = {}
        self._cache_profile: tuple | None = None

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise MarketError("clock: timezone-aware datetime required")
        return value.astimezone(UTC)

    @staticmethod
    def _http_get(path: str, params: dict) -> dict:
        if path not in ALLOWED_PATHS:
            raise MarketError("HTTP path is outside the read-only allowlist")
        query = urlencode(params)
        url = BASE_URL + path + ("?" + query if query else "")
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "Sina-OCO-ReadOnly/1"}, method="GET")
        opener = build_opener(_NoRedirects())
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise MarketError(f"public API HTTP {response.status}")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise MarketError("public API response has invalid length metadata") from exc
                    if declared_size < 0 or declared_size > MAX_RESPONSE_BYTES:
                        raise MarketError("public API response exceeds the size limit")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise MarketError("public API response exceeds the size limit")
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise MarketError(f"public API HTTP {status}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise MarketError("public API connection failed") from None
        try:
            return json.loads(raw.decode("utf-8"), parse_float=Decimal,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise MarketError("public API returned invalid JSON") from None

    def _request(self, path: str, params: dict, symbol: str = "BTC-USDT") -> object:
        if path not in ALLOWED_PATHS:
            raise MarketError("HTTP path is outside the read-only allowlist")
        try:
            if path == KLINES_PATH and self._pace_network:
                delay = self._next_history_request - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._next_history_request = time.monotonic() + 1.05
            envelope = self._transport(path, dict(params))
        except MarketError:
            raise
        except Exception:
            raise MarketError(f"public API request failed for {path}") from None
        if not isinstance(envelope, Mapping):
            raise MarketError("public API envelope must be an object")
        _identity(envelope, symbol)
        if type(envelope.get("code")) not in (int, str) or envelope.get("code") not in (0, "0"):
            raise MarketError(f"public API rejected request for {path}")
        if "data" not in envelope:
            raise MarketError("public API response is missing data")
        return envelope["data"]

    def _server_time(self) -> tuple[datetime, datetime]:
        data = self._request(TIME_PATH, {})
        if not isinstance(data, Mapping) or "serverTime" not in data:
            raise MarketError("server time schema is unverified or missing serverTime")
        _identity(data, "BTC-USDT")
        exchange_time = _from_milliseconds(_milliseconds(data["serverTime"], "serverTime"))
        observed_at = self._now()
        if abs((observed_at - exchange_time).total_seconds()) > MAX_CLOCK_DRIFT_SECONDS:
            raise MarketError("local and exchange clocks differ by more than five seconds")
        return exchange_time, observed_at

    def _price(self, symbol: str) -> tuple[Decimal, datetime]:
        data = self._request(PRICE_PATH, {"symbol": symbol}, symbol)
        if not isinstance(data, Mapping) or data.get("symbol") != symbol:
            raise MarketError("price response has missing or wrong symbol")
        _identity(data, symbol)
        try:
            price = _decimal(data["price"], "price")
            timestamp = _from_milliseconds(_milliseconds(data["time"], "price.time"))
        except KeyError:
            raise MarketError("price response is missing price or exchange time") from None
        return price, timestamp

    @staticmethod
    def _fresh_quote(timestamp: datetime, now: datetime) -> None:
        age = (now - timestamp).total_seconds()
        if age < -MAX_QUOTE_FUTURE_SECONDS or age > MAX_QUOTE_AGE_SECONDS:
            raise MarketError("quote timestamp is stale or in the future")

    def _quote(self, symbol: str, exchange_time: datetime) -> dict:
        if self._stream_quote is not None:
            last, timestamp = self._stream_quote()
            self._fresh_quote(timestamp, self._now())
            return {"last": _decimal_text(_decimal(last, "stream price")),
                    "timestamp": timestamp.isoformat(), "last_timestamp": timestamp.isoformat(),
                    "transport": "websocket"}
        last, last_time = self._price(symbol)
        data = self._request(BOOK_PATH, {"symbol": symbol}, symbol)
        if not isinstance(data, Mapping) or data.get("symbol") != symbol:
            raise MarketError("book response has missing or wrong symbol")
        _identity(data, symbol)
        try:
            bid = _decimal(data["bidPrice"], "bidPrice")
            ask = _decimal(data["askPrice"], "askPrice")
            book_time = _from_milliseconds(_milliseconds(data["time"], "book.time"))
        except KeyError:
            raise MarketError("book response is missing bid, ask, or exchange time") from None
        if bid > ask:
            raise MarketError("book response has a crossed spread")
        observed = self._now()
        if abs((observed - exchange_time).total_seconds()) > MAX_CLOCK_DRIFT_SECONDS:
            raise MarketError("server time became stale while fetching quotes")
        self._fresh_quote(last_time, observed)
        self._fresh_quote(book_time, observed)
        return {"bid": _decimal_text(bid), "ask": _decimal_text(ask), "last": _decimal_text(last),
                "timestamp": min(last_time, book_time).isoformat(),
                "last_timestamp": last_time.isoformat(), "book_timestamp": book_time.isoformat()}

    def _normalize_rows(self, raw: object, symbol: str, seconds: int,
                        now: datetime, limit: int) -> tuple[list[dict], int]:
        if not isinstance(raw, list) or not raw or len(raw) > limit:
            raise MarketError("candle response has an invalid row count")
        milliseconds = seconds * 1000
        decision_ms = _to_milliseconds(now)
        seen: set[int] = set()
        result = []
        origin = None
        for item in raw:
            if not isinstance(item, Mapping):
                raise MarketError("candle row must be an object")
            _identity(item, symbol)
            try:
                start_ms = _milliseconds(item["time"], "candle.time")
                if origin is None:
                    origin = start_ms % milliseconds
                if start_ms % milliseconds != origin:
                    raise MarketError("candle opening times do not share an exchange boundary")
                if start_ms in seen:
                    raise MarketError("duplicate candle opening time")
                seen.add(start_ms)
                if start_ms > decision_ms:
                    raise MarketError("candle opening time is in the future")
                values = {key: _decimal_text(_decimal(item[key], f"candle.{key}"))
                          for key in ("open", "high", "low", "close")}
                volume = None if item.get("volume") is None else _decimal_text(
                    _decimal(item["volume"], "candle.volume", positive=False))
            except KeyError as exc:
                raise MarketError(f"candle row is missing {exc.args[0]}") from None
            end_ms = start_ms + milliseconds
            if end_ms <= decision_ms:
                result.append({"open_at": _from_milliseconds(start_ms).isoformat(),
                               "close_at": _from_milliseconds(end_ms).isoformat(),
                               "closed": True, **values, "volume": volume})
        result.sort(key=lambda row: row["open_at"])
        return result, origin

    @staticmethod
    def _latest_close(now: datetime, seconds: int, origin: int) -> int:
        duration = seconds * 1000
        return ((_to_milliseconds(now) - origin) // duration) * duration + origin

    @staticmethod
    def _check_history(rows: list[dict], seconds: int, now: datetime, warmup: int,
                       origin: int) -> list[Candle]:
        if len(rows) < warmup:
            raise MarketError(f"at least {warmup} closed candles are required")
        try:
            candles = validate_closed_bars(rows, seconds, now)
        except CandleError as exc:
            raise MarketError(str(exc)) from None
        expected = BingXPublicClient._latest_close(now, seconds, origin)
        if _to_milliseconds(candles[-1].close_at) != expected:
            raise MarketError("history does not end at the latest expected candle close")
        return candles

    def _history(self, symbol: str, timeframe: str, now: datetime, warmup: int) -> tuple[list[dict], int, bool]:
        seconds = TIMEFRAMES[timeframe]
        key = (symbol, timeframe, warmup)
        cached = self._history_cache.get(key)
        if cached is not None and cached[0] == self._latest_close(now, seconds, cached[1]):
            return [dict(row) for row in cached[2]], cached[1], False
        limit = min(warmup + 1, MAX_ROWS)
        raw = self._request(KLINES_PATH, {"symbol": symbol, "interval": timeframe, "limit": limit}, symbol)
        rows, origin = self._normalize_rows(raw, symbol, seconds, now, limit)
        if warmup == MAX_ROWS and len(rows) == MAX_ROWS - 1:
            earliest = datetime.fromisoformat(rows[0]["open_at"])
            older_raw = self._request(KLINES_PATH, {"symbol": symbol, "interval": timeframe,
                                     "limit": 1, "endTime": _to_milliseconds(earliest) - 1}, symbol)
            older, older_origin = self._normalize_rows(older_raw, symbol, seconds, now, 1)
            if older_origin != origin:
                raise MarketError("candle pages do not share an exchange boundary")
            rows = older + rows
        rows = rows[-warmup:]
        self._check_history(rows, seconds, now, warmup, origin)
        expected = self._latest_close(now, seconds, origin)
        if self._stream_quote is not None and _to_milliseconds(now) - expected < 2000:
            raise MarketError("latest candle boundary is still settling")
        self._history_cache[key] = (expected, origin, tuple(dict(row) for row in rows))
        return rows, origin, True

    def collect(self, symbol: str = "BTC-USDT", warmup: int = 400) -> dict:
        if symbol not in (SOURCE_SYMBOL, CANONICAL_SYMBOL):
            raise MarketError("this agent is scoped to BTC-USDT Perpetual")
        if type(warmup) is not int or not 400 <= warmup <= MAX_ROWS:
            raise MarketError("warmup must be an integer from 400 through 1000")
        symbol = SOURCE_SYMBOL
        profile = (symbol, warmup)
        if profile != self._cache_profile:
            self._history_cache.clear()
            self._cache_profile = profile
        exchange_time, _ = self._server_time()
        frames = {}
        origins = {}
        fetched_history = False
        for timeframe in TIMEFRAMES:
            if self._stream_quote is not None:
                self._stream_quote()
            try:
                rows, origin, fetched = self._history(symbol, timeframe, exchange_time, warmup)
            except MarketError as exc:
                raise MarketError(f"{timeframe}: {exc}") from None
            frames[timeframe] = rows
            origins[timeframe] = origin
            fetched_history = fetched_history or fetched
        if fetched_history:
            exchange_time, _ = self._server_time()
        for timeframe, rows in frames.items():
            self._check_history(rows, TIMEFRAMES[timeframe], exchange_time, warmup, origins[timeframe])
        quote = self._quote(symbol, exchange_time)
        observed_at = self._now()
        quote_times = [datetime.fromisoformat(quote[key]) for key in ("last_timestamp", "book_timestamp")
                       if key in quote]
        decision_time = max(exchange_time, observed_at, *quote_times)
        # A quote fetch can straddle a close. Never publish a cached history
        # from the preceding interval together with the new market quote.
        for timeframe, rows in frames.items():
            self._check_history(rows, TIMEFRAMES[timeframe], decision_time, warmup, origins[timeframe])
        for timestamp in quote_times:
            self._fresh_quote(timestamp, observed_at)
        if abs((observed_at - exchange_time).total_seconds()) > MAX_CLOCK_DRIFT_SECONDS:
            raise MarketError("server time became stale before snapshot completion")
        missing = {timeframe: sum(row["volume"] is None for row in rows)
                   for timeframe, rows in frames.items()}
        return {"provider": "BingX", "product": "perpetual_futures", "symbol": CANONICAL_SYMBOL,
                "source_symbol": symbol, "price_kind": "last_price", "observed_at": observed_at.isoformat(),
                "exchange_time": exchange_time.isoformat(), "quote": quote, "frames": frames,
                "origins": {tf: _from_milliseconds(value).isoformat() for tf, value in origins.items()},
                "source_base_url": BASE_URL, "live_schema_verified": False,
                "schema_status": "candidate_contract_pending_live_verification",
                "volume": {"kind": "exchange_reported", "units_verified": False,
                           "missing_counts": missing},
                "warnings": ["Live API schema and exchange candle boundaries remain unverified."]
                + (["Missing candle volumes are preserved as null."] if any(missing.values()) else [])}

    def probe(self) -> dict:
        """Probe only server time and one public last-price quote."""
        exchange_time, _ = self._server_time()
        price, timestamp = self._price("BTC-USDT")
        observed = self._now()
        self._fresh_quote(timestamp, observed)
        if abs((observed - exchange_time).total_seconds()) > MAX_CLOCK_DRIFT_SECONDS:
            raise MarketError("server time became stale during probe")
        return {"provider": "BingX", "product": "perpetual_futures", "symbol": CANONICAL_SYMBOL,
                "source_symbol": SOURCE_SYMBOL,
                "price_kind": "last_price", "observed_at": observed.isoformat(),
                "exchange_time": exchange_time.isoformat(), "price": _decimal_text(price),
                "quote_timestamp": timestamp.isoformat(), "transport_succeeded": True,
                "verified": False, "live_schema_verified": False,
                "reason": "Public-response probe only; full schema and integration validation are pending."}

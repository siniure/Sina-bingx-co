"""Strict replay input boundary. A JSON assertion never authenticates a live feed."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from core import GateError, clock_value, decimal, time_value
from indicators import Candle, CandleError, validate_closed_bars

IDENTITY = {"provider": "BingX", "product": "standard_futures",
            "symbol": "BTCUSDT", "price_kind": "index_price"}
TIMEFRAMES = {"5m": 300, "15m": 900, "1h": 3600,
              "4h": 14400, "12h": 43200, "1d": 86400}


def identity(record: dict, name: str, *, price_kind: bool = True) -> None:
    if not isinstance(record, dict):
        raise GateError(f"{name}: object required")
    for key, expected in IDENTITY.items():
        if key == "price_kind" and not price_kind:
            continue
        if record.get(key) != expected:
            raise GateError(f"{name}: wrong {key}; expected {expected}")


def fresh(value: Any, now: datetime, name: str, seconds: int = 15) -> datetime:
    event = time_value(value, name)
    if not -2 <= (now - event).total_seconds() <= seconds:
        raise GateError(f"{name}: stale or future")
    return event


@dataclass(frozen=True)
class Market:
    as_of: datetime
    source_id: str
    data_mode: str
    price: Decimal
    event_at: datetime
    bars: dict[str, list[Candle]]
    raw: dict


def parse_market(payload: dict, now: datetime | None = None) -> Market:
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 2:
        raise GateError("snapshot schema_version=2 required")
    if payload.get("data_mode") not in {"synthetic", "recorded_replay"}:
        raise GateError("only synthetic/recorded_replay inputs are implemented; live input blocked")
    identity(payload, "snapshot")
    as_of = time_value(payload.get("as_of"), "as_of")
    if now is not None and as_of != clock_value(now):
        raise GateError("snapshot as_of must match replay clock")
    source_id = payload.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise GateError("source_id required")
    quote = payload.get("quote")
    identity(quote, "quote")
    if quote.get("source_id") != source_id or quote.get("timestamp_kind") != "exchange_event":
        raise GateError("quote: matching source and exchange event timestamp required")
    event_at = fresh(quote.get("event_at"), as_of, "quote event_at")
    observed = fresh(quote.get("observed_at"), as_of, "quote observed_at")
    if event_at > observed + timedelta(seconds=2):
        raise GateError("quote event after observation")
    price = decimal(quote.get("price"), "quote price", positive=True)
    bars = {}
    histories = payload.get("histories")
    if not isinstance(histories, dict) or set(histories) != set(TIMEFRAMES):
        raise GateError("all six exact timeframes required")
    for tf, seconds in TIMEFRAMES.items():
        history = histories[tf]
        identity(history, f"history {tf}")
        if history.get("source_id") != source_id:
            raise GateError(f"history {tf}: source changed")
        # The adapter supplies the actual exchange boundary origin, never a local timezone guess.
        origin = time_value(history.get("origin_at"), f"{tf} origin_at")
        fresh(history.get("observed_at"), as_of, f"{tf} observed_at")
        try:
            candles = validate_closed_bars(history.get("bars"), seconds, as_of)
        except CandleError as exc:
            raise GateError(f"{tf}: {exc}") from exc
        if not 400 <= len(candles) <= 10000:
            raise GateError(f"{tf}: 400..10000 closed candles required")
        elapsed = (as_of - origin).total_seconds()
        latest = origin + timedelta(seconds=(elapsed // seconds) * seconds)
        if candles[-1].close_at != latest:
            raise GateError(f"{tf}: latest closed boundary missing")
        if any((bar.open_at - origin).total_seconds() % seconds for bar in candles):
            raise GateError(f"{tf}: inconsistent exchange boundaries")
        bars[tf] = candles
    return Market(as_of, source_id, payload["data_mode"], price, event_at, bars, payload)


def check_quote_path(market: Market, signal: dict) -> None:
    """Do not revive a setup after a barrier hit or a missing observation interval.

    This checks the supplied replay path; it cannot prove that the upstream
    exchange stream contained no missing events. Live activation stays blocked.
    """
    path = market.raw.get("quote_path")
    if not isinstance(path, list) or not path:
        raise GateError("quote path since confirmation required")
    start = time_value(signal["confirmed_at"], "confirmed_at")
    previous = None
    previous_seq = None
    last_price = None
    for point in path:
        if not isinstance(point, dict) or point.get("source_id") != market.source_id:
            raise GateError("quote path: source mismatch")
        event = time_value(point.get("event_at"), "path event_at")
        seq = point.get("sequence")
        if type(seq) is not int or seq < 0:
            raise GateError("quote path: sequence required")
        if previous is None:
            if event != start:
                raise GateError("quote path must begin at confirmation")
        elif not previous <= event or (event - previous).total_seconds() > 15 or seq != previous_seq + 1:
            raise GateError("quote path: gap or unordered sequence")
        if event > market.event_at:
            raise GateError("quote path extends beyond current exchange event")
        value = decimal(point.get("price"), "path price", positive=True)
        sl, tp = Decimal(signal["sl"]), Decimal(signal["tp"])
        crossed = value <= sl or value >= tp if signal["direction"] == "long" else value >= sl or value <= tp
        if crossed:
            raise GateError("quote path: setup invalidated at TP/SL; cannot revive")
        previous, previous_seq, last_price = event, seq, value
    if previous != market.event_at or last_price != market.price:
        raise GateError("quote path must cover and match current quote")

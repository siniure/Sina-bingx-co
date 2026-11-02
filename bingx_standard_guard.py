#!/usr/bin/env python3
"""Validate a browser/network report of BingX Standard BTCUSDT.

This is a read-only report validator, NOT a connected exchange adapter.
It neither fetches prices nor independently authenticates a browser report.
No network calls, account access, API keys, or trading operations are present.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

UTC = timezone.utc


class Rejected(ValueError):
    """A report fails the data contract."""


def timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise Rejected(f"{name}: timezone-aware ISO 8601 required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Rejected(f"{name}: invalid timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise Rejected(f"{name}: timezone is missing")
    return result.astimezone(UTC)


def price_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise Rejected("price: decimal string required; floats are not accepted")
    raw = str(value).strip()
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", raw):
        raise Rejected("price: invalid decimal representation")
    try:
        result = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise Rejected("price: invalid number") from exc
    if not result.is_finite() or result <= 0:
        raise Rejected("price: must be finite and positive")
    return result


def check_standard_url(value: object) -> str:
    if not isinstance(value, str):
        raise Rejected("source_url: required")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise Rejected("source_url: invalid URL") from exc
    if (url.scheme != "https" or url.hostname not in {"bingx.com", "www.bingx.com"}
            or url.username or url.password or port not in {None, 443}):
        raise Rejected("source_url: official HTTPS BingX URL required")
    # A candidate route, not a claim that the route currently works.
    if not re.fullmatch(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?futures/forward/BTCUSDT/?",
                        url.path):
        raise Rejected("source_url: Standard forward BTCUSDT route required")
    if url.query or url.fragment:
        raise Rejected("source_url: use the clean canonical URL without query/fragment")
    return value


def recent(value: datetime, now: datetime, maximum_age: float,
           future_tolerance: float, name: str) -> float:
    age = (now - value).total_seconds()
    if age < -future_tolerance:
        raise Rejected(f"{name}: timestamp is in the future")
    if age > maximum_age:
        raise Rejected(f"{name}: stale ({age:.3f}s old)")
    return age


def validate(report: object, *, now: datetime | None = None,
             max_age_seconds: float = 15, future_tolerance_seconds: float = 2) -> dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("validator clock must be timezone-aware")
    now = now.astimezone(UTC)
    if not isinstance(report, dict):
        raise Rejected("report must be a JSON object")
    expected = {
        "schema_version": 1,
        "provider": "BingX",
        "product": "standard_futures",
        "symbol": "BTCUSDT",
        "quote_currency": "USDT",
        "price_kind": "index_price",
        "data_mode": "live",
    }
    for field, value in expected.items():
        if report.get(field) != value or (field == "schema_version" and
                                          type(report.get(field)) is not int):
            raise Rejected(f"{field}: expected {value!r}")
    url = check_standard_url(report.get("source_url"))
    evidence = report.get("evidence")
    if not isinstance(evidence, dict):
        raise Rejected("evidence: selected product, symbol, and price label required")
    if evidence.get("standard_selected") is not True:
        raise Rejected("evidence: Standard must be SELECTED, not merely in the menu")
    if re.sub(r"\s+", "", str(evidence.get("selected_product_text", ""))).casefold() != "standardfutures":
        raise Rejected("evidence: explicit selected Standard Futures label required")
    if evidence.get("selected_symbol") != "BTCUSDT":
        raise Rejected("evidence: selected BTCUSDT symbol required")
    if str(evidence.get("price_label", "")).strip().casefold() != "index price":
        raise Rejected("evidence: Index Price required, not USD conversion/mark/last")
    if not isinstance(evidence.get("price_text"), str):
        raise Rejected("evidence: raw visible price text is required")
    price = price_decimal(report.get("price"))
    if price_decimal(evidence["price_text"]) != price:
        raise Rejected("price: does not match captured visible evidence")
    observation = timestamp(report.get("observed_at"), "observed_at")
    age = recent(observation, now, max_age_seconds,
                 future_tolerance_seconds, "observed_at")
    cache = report.get("cache_status", "unknown")
    if cache not in {"unknown", "not_cached", "cached"}:
        raise Rejected("cache_status: invalid")
    if cache == "cached":
        raise Rejected("cache_status: cached content cannot be reported as live")
    if report.get("transport") not in {"dom", "https_response", "websocket"}:
        raise Rejected("transport: unsupported or missing")

    source_raw = report.get("source_timestamp")
    source_age = None
    status = "OBSERVED_ONLY"
    warnings = []
    if source_raw is None:
        if report.get("source_timestamp_kind") not in {None, "not_available"}:
            raise Rejected("source_timestamp_kind: inconsistent with absent timestamp")
        warnings.append("No exchange timestamp; observation time does not prove live exchange freshness.")
    else:
        if report.get("source_timestamp_kind") != "exchange_event":
            raise Rejected("source_timestamp: browser/receipt time cannot be called exchange time")
        if report["transport"] not in {"https_response", "websocket"}:
            raise Rejected("source_timestamp: exchange_event requires captured network evidence")
        trace = evidence.get("timestamp_trace")
        if not isinstance(trace, dict) or not all(isinstance(trace.get(k), str) and trace[k]
                                                   for k in ("field", "value", "context")):
            raise Rejected("timestamp_trace: source field, raw value, and market context required")
        if not isinstance(evidence.get("market_binding"), str) or not evidence["market_binding"]:
            raise Rejected("market_binding: explain why the network event belongs to Standard")
        source = timestamp(source_raw, "source_timestamp")
        source_age = recent(source, now, max_age_seconds,
                            future_tolerance_seconds, "source_timestamp")
        if (source - observation).total_seconds() > future_tolerance_seconds:
            raise Rejected("source_timestamp: later than observation")
        status = "FRESH_SOURCE_TIMESTAMP"
    if cache == "unknown":
        warnings.append("Cache behavior was not independently established.")

    return {
        "status": status,
        "provider": "BingX",
        "product": "standard_futures",
        "symbol": "BTCUSDT",
        "price_kind": "index_price",
        "quote_currency": "USDT",
        "price": format(price, "f"),
        "source_url": url,
        "observed_at": observation.isoformat(),
        "source_timestamp": source_raw,
        "observation_age_seconds": round(age, 3),
        "source_age_seconds": round(source_age, 3) if source_age is not None else None,
        "validated_at": now.isoformat(),
        "input_checks_passed": True,
        "independent_source_authentication": False,
        "execution_authorized": False,
        "warnings": warnings,
    }


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", default="-", help="JSON report path, or - for stdin")
    parser.add_argument("--max-age", type=float, default=15, help="Maximum observation/source age, seconds")
    args = parser.parse_args(argv)
    if not 0 < args.max_age <= 60:
        parser.error("--max-age must be greater than 0 and no more than 60 seconds")
    try:
        text = sys.stdin.read() if args.report == "-" else Path(args.report).read_text(encoding="utf-8")
        report = json.loads(text, parse_float=Decimal,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON number")))
        result = validate(report, max_age_seconds=args.max_age)
    except (Rejected, ValueError, OSError) as exc:
        print(json.dumps({"status": "UNAVAILABLE", "price": None,
                          "input_checks_passed": False, "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

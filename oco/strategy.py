"""Causal, deterministic research rules. No profitability or live readiness claim."""
from __future__ import annotations

import hashlib
import json
from statistics import mean, pstdev
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from core import GateError, canonical_decimal, decimal
from indicators import Candle, atr, ema, macd, rsi
from .market import Market

VERSION = "standard-retest-research-0.1"


def number(value) -> Decimal:
    return Decimal(str(value))


def quantize(value: Decimal, tick: Decimal, rounding=ROUND_HALF_UP) -> Decimal:
    return (value / tick).to_integral_value(rounding=rounding) * tick


def pivots(bars: list[Candle], *, before=None, width=2) -> list[dict]:
    end = len(bars) if before is None else before
    result = []
    # Every pivot is available only after width following bars have CLOSED.
    for index in range(width, end - width):
        left, mid, right = bars[index-width:index], bars[index], bars[index+1:index+width+1]
        if all(mid.high > bar.high for bar in left + right):
            result.append({"kind": "resistance", "price": number(mid.high), "index": index,
                           "confirmed_at": bars[index+width].close_at.isoformat()})
        if all(mid.low < bar.low for bar in left + right):
            result.append({"kind": "support", "price": number(mid.low), "index": index,
                           "confirmed_at": bars[index+width].close_at.isoformat()})
    return result


def summarize(bars: list[Candle], current: Decimal) -> dict:
    closes = [bar.close for bar in bars]
    averages = {period: ema(closes, period) for period in (20, 50, 200)}
    m = macd(closes)
    r14, r24 = rsi(closes, 14)[-1], rsi(closes, 24)[-1]
    e20, e50, e200 = (averages[period][-1] for period in (20, 50, 200))
    slope = e50 - averages[50][-4]
    if closes[-1] > e20 > e50 > e200 and slope > 0 and r14 > 50 and r24 > 50:
        direction = "long"
    elif closes[-1] < e20 < e50 < e200 and slope < 0 and r14 < 50 and r24 < 50:
        direction = "short"
    else:
        direction = "neutral"
    points = pivots(bars)
    band_mid, band_std = mean(closes[-20:]), pstdev(closes[-20:])
    supports = sorted({p["price"] for p in points if p["kind"] == "support" and p["price"] < current}, reverse=True)[:5]
    resistances = sorted({p["price"] for p in points if p["kind"] == "resistance" and p["price"] > current})[:5]
    return {
        "direction": direction, "closed_at": bars[-1].close_at.isoformat(),
        "close": closes[-1], "ema20": e20, "ema50": e50, "ema200": e200,
        "ema50_slope_3_bars": slope, "macd_line": m["line"][-1],
        "macd_signal": m["signal"][-1], "macd_histogram": m["histogram"][-1],
        "previous_macd_histogram": m["histogram"][-2], "rsi14": r14, "rsi24": r24,
        "atr14_rma": atr(bars)[-1],
        "bollinger20_mid": band_mid, "bollinger20_upper": band_mid+2*band_std,
        "bollinger20_lower": band_mid-2*band_std,
        "supports": [canonical_decimal(x) for x in supports],
        "resistances": [canonical_decimal(x) for x in resistances],
        "volume": bars[-1].volume, "volume_kind": "unverified_optional_index_volume",
    }


def analyze(market: Market, settings: dict) -> dict:
    tick = decimal(settings.get("research_tick_size"), "research_tick_size", positive=True)
    minimum = decimal(settings.get("minimum_net_rr"), "minimum_net_rr", positive=True)
    if settings.get("version") != VERSION or minimum < 1:
        raise GateError("unsupported research strategy configuration")
    summaries = {tf: summarize(bars, market.price) for tf, bars in market.bars.items()}
    result = {"strategy_version": VERSION, "timeframes": summaries, "signal": None,
              "decision": "NO_ENTRY", "reason": "MULTITIMEFRAME_CONFLICT"}
    dominant = {summaries[tf]["direction"] for tf in ("15m", "1h", "4h", "12h", "1d")}
    if len(dominant) != 1 or "neutral" in dominant:
        return result
    direction = dominant.pop()
    fast = summaries["5m"]
    histogram = fast["macd_histogram"]
    previous = fast["previous_macd_histogram"]
    momentum = histogram > 0 and histogram >= previous if direction == "long" else histogram < 0 and histogram <= previous
    if fast["direction"] != direction or not momentum:
        result["reason"] = "FIVE_MINUTE_CONFIRMATION_MISSING"
        return result
    bars = market.bars["5m"]
    breakout, confirmation = bars[-2:]
    volatility = number(fast["atr14_rma"])
    if volatility <= 0:
        result["reason"] = "ZERO_ATR"
        return result
    kind = "resistance" if direction == "long" else "support"
    # Candidates were confirmed before the breakout bar opened.
    levels = [p for p in pivots(bars, before=len(bars)-2)
              if p["kind"] == kind and p["index"] >= len(bars)-38]
    chosen = None
    for point in reversed(levels):
        level = point["price"]
        if direction == "long":
            crossed = number(bars[-3].close) <= level and number(breakout.close) > level + volatility*Decimal("0.05")
            retested = level-volatility*Decimal("0.25") <= number(confirmation.low) <= level+volatility*Decimal("0.10")
            rejected = number(confirmation.close) > level+volatility*Decimal("0.05") and confirmation.close > confirmation.open
        else:
            crossed = number(bars[-3].close) >= level and number(breakout.close) < level - volatility*Decimal("0.05")
            retested = level-volatility*Decimal("0.10") <= number(confirmation.high) <= level+volatility*Decimal("0.25")
            rejected = number(confirmation.close) < level-volatility*Decimal("0.05") and confirmation.close < confirmation.open
        if crossed and retested and rejected:
            chosen = point
            break
    if chosen is None:
        result["reason"] = "BREAKOUT_AND_RETEST_MISSING"
        return result
    level = chosen["price"]
    entry = quantize(number(confirmation.close), tick)
    entry_min = quantize(entry-volatility*Decimal("0.10"), tick, ROUND_CEILING)
    entry_max = quantize(entry+volatility*Decimal("0.10"), tick, ROUND_FLOOR)
    if not entry_min <= market.price <= entry_max:
        result["reason"] = "QUOTE_OUTSIDE_ENTRY_RANGE"
        return result
    if direction == "long":
        sl = quantize(min(number(confirmation.low), level-volatility*Decimal("0.25"))-volatility*Decimal("0.25"), tick, ROUND_FLOOR)
        targets = [number(p) for tf in ("15m", "1h") for p in summaries[tf]["resistances"]
                   if number(p) > entry_max]
        target = min(targets) if targets else None
        tp = None if target is None else quantize(target-volatility*Decimal("0.10"), tick, ROUND_FLOOR)
    else:
        sl = quantize(max(number(confirmation.high), level+volatility*Decimal("0.25"))+volatility*Decimal("0.25"), tick, ROUND_CEILING)
        targets = [number(p) for tf in ("15m", "1h") for p in summaries[tf]["supports"]
                   if number(p) < entry_min]
        target = max(targets) if targets else None
        tp = None if target is None else quantize(target+volatility*Decimal("0.10"), tick, ROUND_CEILING)
    valid = tp is not None and (0 < sl < entry_min <= entry <= entry_max < tp if direction == "long"
                                 else 0 < tp < entry_min <= entry <= entry_max < sl)
    if not valid:
        result["reason"] = "NO_STRUCTURAL_TARGET_OR_INVALID_PRICE_GEOMETRY"
        return result
    closed_at = confirmation.close_at
    fingerprint = [VERSION, market.source_id, direction, closed_at.isoformat()]
    # One identity per candle/direction/version, including corrected levels.
    signal_id = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()[:24]
    signal = {"id": signal_id, "direction": direction, "confirmed_at": closed_at.isoformat(),
              "expires_at": (closed_at+timedelta(minutes=15)).isoformat(),
              "entry_reference": canonical_decimal(entry),
              "entry_range": {"min": canonical_decimal(entry_min), "max": canonical_decimal(entry_max)},
              "sl": canonical_decimal(sl), "tp": canonical_decimal(tp),
              "retest_level": canonical_decimal(level), "structural_target": canonical_decimal(target),
              "stop_buffer_atr": "0.25", "target_buffer_atr": "0.10", "buffers_applied_once": True,
              "conditions": ["All five higher timeframes aligned", "5m breakout and closed retest confirmed",
                             "Current quote remains in entry range", "No previous TP/SL touch after confirmation",
                             "Fresh costs and adequate net reward/risk", "Account risk check required before any real entry"]}
    result.update(signal=signal, decision="RESEARCH_CANDIDATE", reason="CLOSED_RETEST_CONFIRMED")
    return result

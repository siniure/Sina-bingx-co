"""Offline indicators over explicitly closed, contiguous OHLC candles.

No network, account access, source authentication, or trading occurs here.
The adapter must normalize exchange candle boundaries to [open_at, close_at)
without changing their verified origin. A fresh quote does not make old candle
history fresh. Source identity and latest-data age are separate caller checks.

EMA uses an initial SMA seed; MACD signal uses the first nine available MACD
values and its histogram is line minus signal (never a factor-of-two scale).
ATR and RSI use Wilder's RMA, alpha=1/period. The first true range is high-low
because no preceding close exists. A flat RSI is defined as 50. Short-history
indicator functions are provided for testing; study() requires >=250 candles
and defaults to 400 to reduce seed effects. This is not proof of convergence.
Index candles need not have traded volume: missing volume stays None and no
perpetual volume is substituted for Standard Index volume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Mapping, Sequence

UTC = timezone.utc


class CandleError(ValueError):
    """Input history cannot be used as a closed-candle study."""


@dataclass(frozen=True)
class Candle:
    open_at: datetime
    close_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


def _period(period: int) -> None:
    if type(period) is not int or period < 1:
        raise ValueError("period must be a positive integer")


def _number(value: object, name: str, *, positive: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise CandleError(f"{name}: finite numeric value required")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise CandleError(f"{name}: invalid number") from exc
    if not isfinite(result) or (result <= 0 if positive else result < 0):
        raise CandleError(f"{name}: {'positive' if positive else 'nonnegative'} finite number required")
    return result


def _time(value: object, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CandleError(f"{name}: invalid timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CandleError(f"{name}: timezone-aware ISO timestamp required")
    return value.astimezone(UTC)


def validate_closed_bars(
    bars: Sequence[Mapping], timeframe_seconds: int, now: datetime
) -> list[Candle]:
    """Validate chronological closed bars; do not silently sort or fill gaps.

    Each mapping requires open_at, close_at, closed=True and open/high/low/close.
    close_at is the exclusive normalized right boundary, not an exchange's
    inclusive final millisecond. Optional volume is nonnegative or absent.
    """
    if type(timeframe_seconds) is not int or timeframe_seconds <= 0:
        raise CandleError("timeframe_seconds must be a positive integer")
    decision = _time(now, "now")
    duration = timedelta(seconds=timeframe_seconds)
    result: list[Candle] = []
    if not isinstance(bars, (list, tuple)) or not bars:
        raise CandleError("nonempty candle list required")
    for index, raw in enumerate(bars):
        if not isinstance(raw, Mapping):
            raise CandleError(f"bar {index}: object required")
        try:
            if raw.get("closed") is not True:
                raise CandleError(f"bar {index}: closed=True required")
            start = _time(raw["open_at"], "open_at")
            end = _time(raw["close_at"], "close_at")
            if end - start != duration:
                raise CandleError(f"bar {index}: wrong candle duration")
            if end > decision:
                raise CandleError(f"bar {index}: look-ahead or still-open candle")
            if result and start != result[-1].close_at:
                raise CandleError(f"bar {index}: gap, duplicate, or unordered candle")
            o, h, l, c = (_number(raw[key], key) for key in ("open", "high", "low", "close"))
            if l > min(o, c) or h < max(o, c) or l > h:
                raise CandleError(f"bar {index}: inconsistent OHLC")
            volume = None if raw.get("volume") is None else _number(raw["volume"], "volume", positive=False)
        except KeyError as exc:
            raise CandleError(f"bar {index}: missing {exc.args[0]}") from exc
        result.append(Candle(start, end, o, h, l, c, volume))
    return result


def _values(values: Sequence[float]) -> list[float]:
    converted = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("boolean is not an indicator value")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("finite numeric indicator values required") from exc
        if not isfinite(number):
            raise ValueError("finite numeric indicator values required")
        converted.append(number)
    return converted


def _smooth(values: Sequence[float], period: int, alpha: float) -> list[float | None]:
    _period(period)
    data = _values(values)
    result: list[float | None] = [None] * len(data)
    if len(data) < period:
        return result
    current = sum(data[:period]) / period
    result[period - 1] = current
    for index in range(period, len(data)):
        current += alpha * (data[index] - current)
        result[index] = current
    return result


def ema(values: Sequence[float], period: int) -> list[float | None]:
    _period(period)
    return _smooth(values, period, 2 / (period + 1))


def rma(values: Sequence[float], period: int) -> list[float | None]:
    _period(period)
    return _smooth(values, period, 1 / period)


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    for period in (fast, slow, signal):
        _period(period)
    if fast >= slow:
        raise ValueError("MACD fast period must be below slow period")
    data = _values(values)
    fast_values, slow_values = ema(data, fast), ema(data, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(fast_values, slow_values)]
    offset = slow - 1
    signal_values: list[float | None] = [None] * len(data)
    if len(data) > offset:
        signal_values[offset:] = ema(line[offset:], signal)
    histogram = [None if a is None or b is None else a - b for a, b in zip(line, signal_values)]
    return {"line": line, "signal": signal_values, "histogram": histogram}


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    result = []
    previous = None
    for candle in candles:
        tr = candle.high - candle.low
        if previous is not None:
            tr = max(tr, abs(candle.high - previous), abs(candle.low - previous))
        result.append(tr)
        previous = candle.close
    return result


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    return rma(true_ranges(candles), period)


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    _period(period)
    data = _values(values)
    result: list[float | None] = [None] * len(data)
    changes = [b - a for a, b in zip(data, data[1:])]
    gains = rma([max(change, 0) for change in changes], period)
    losses = rma([max(-change, 0) for change in changes], period)
    for index, (gain, loss) in enumerate(zip(gains, losses), start=1):
        if gain is None or loss is None:
            continue
        result[index] = 50.0 if gain == loss == 0 else (100.0 if loss == 0 else 100 - 100 / (1 + gain / loss))
    return result


def study(bars: Sequence[Mapping], timeframe_seconds: int, now: datetime, warmup_bars: int = 400) -> dict:
    """Compute latest core indicators; no strategy or live-source claim implied."""
    if type(warmup_bars) is not int or warmup_bars < 250:
        raise CandleError("operational warmup must be at least 250 closed candles; 400 is default")
    candles = validate_closed_bars(bars, timeframe_seconds, now)
    if len(candles) < warmup_bars:
        raise CandleError(f"at least {warmup_bars} closed candles required")
    closes = [candle.close for candle in candles]
    values = macd(closes)
    return {
        "last_closed_at": candles[-1].close_at.isoformat(),
        "timeframe_seconds": timeframe_seconds,
        "closed_candles": len(candles),
        "seed_method": "SMA-seeded EMA; Wilder RMA for ATR/RSI",
        "macd_line": values["line"][-1],
        "macd_signal": values["signal"][-1],
        "macd_histogram": values["histogram"][-1],
        "atr14_rma": atr(candles, 14)[-1],
        "rsi14_wilder_close": rsi(closes, 14)[-1],
        "rsi24_wilder_close": rsi(closes, 24)[-1],
        "latest_volume": candles[-1].volume,
        "source_authenticated": False,
    }

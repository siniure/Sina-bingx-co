"""Deterministic SYNTHETIC fixture generator; these are never BingX prices."""
from datetime import datetime, timedelta, timezone

from .market import IDENTITY, TIMEFRAMES


def synthetic_snapshot(direction="long"):
    if direction not in {"long", "short", "neutral"}:
        raise ValueError("long, short or neutral required")
    now = datetime(2026, 11, 2, 12, 0, 1, tzinfo=timezone.utc)
    binding = {**IDENTITY, "source_id": "SYNTHETIC-NOT-EXCHANGE-DATA"}
    result = {"schema_version": 2, "data_mode": "synthetic", "as_of": now.isoformat(), **binding,
              "histories": {}}
    for tf, seconds in TIMEFRAMES.items():
        end = datetime.fromtimestamp(int(now.timestamp())//seconds*seconds, tz=timezone.utc)
        bars = []
        for index in range(400):
            close = 700+index
            o, h, lo, c = close-1, close+1, close-2, close
            if tf == "5m":
                if index == 394:
                    h = 1100
                if index == 398:
                    o, h, lo, c = 1097, 1105, 1096, 1103
                if index == 399:
                    o, h, lo, c = 1101, 1105, 1100, 1104
            elif index == 390:
                h = 1130
            if direction == "neutral" and tf == "1d":
                o, h, lo, c = 1000, 1001, 999, 1000
            if direction == "short":
                o, h, lo, c = 2200-o, 2200-lo, 2200-h, 2200-c
            closed = end-timedelta(seconds=(399-index)*seconds)
            bars.append({"open_at": (closed-timedelta(seconds=seconds)).isoformat(),
                         "close_at": closed.isoformat(), "closed": True,
                         "open": str(o), "high": str(h), "low": str(lo), "close": str(c), "volume": None})
        result["histories"][tf] = {**binding, "origin_at": "1970-01-01T00:00:00+00:00",
                                   "observed_at": now.isoformat(), "bars": bars}
    price = "1096" if direction == "short" else "1104"
    result["quote"] = {**binding, "timestamp_kind": "exchange_event", "price": price,
                        "event_at": now.isoformat(), "observed_at": now.isoformat()}
    result["quote_path"] = [{"source_id": binding["source_id"], "event_at": (now-timedelta(seconds=1)).isoformat(),
                              "sequence": 10, "price": price},
                             {"source_id": binding["source_id"], "event_at": now.isoformat(),
                              "sequence": 11, "price": price}]
    result["costs"] = {**IDENTITY, "verified_at": now.isoformat(), "expires_at": (now+timedelta(hours=1)).isoformat(),
                        "evidence": "SYNTHETIC fee input; not a verified BingX fee",
                        "entry_fee_rate": "0.00045", "exit_fee_rate": "0",
                        "execution_reserve_per_unit": "0.05", "funding_reserve_per_unit": "0.05"}
    result["account"] = {**IDENTITY, "as_of": now.isoformat(), "coverage_complete": True,
                          "equity": "10000", "available_margin": "10000", "exposures": [],
                          "positions_evidence": "SYNTHETIC empty positions",
                          "entry_orders_evidence": "SYNTHETIC empty entry orders"}
    return result

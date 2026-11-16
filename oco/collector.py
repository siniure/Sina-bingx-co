"""Continuous read-only market observations; publication requires separate readiness.

This worker can collect public Perpetual data once its candidate API contract is
verified on the host. It never interprets an HTTP success or a local flag as
account authentication, validated strategy performance, or delivery readiness.
"""
from __future__ import annotations

import argparse
import json
import signal
import threading
from datetime import datetime, timedelta
from pathlib import Path

from core import GateError, atomic_json, clock_value, time_value
from .engine import evaluate
from .market import PRODUCT_IDENTITIES, TIMEFRAMES, fresh, parse_market
from .perpetual_market import BingXPublicClient, MarketError
from .service import load_json

PUBLICATION_BLOCKERS = [
    "LIVE_API_SCHEMA_AND_BOUNDARIES_NOT_INDEPENDENTLY_VERIFIED",
    "CURRENT_ACCOUNT_AND_AGGREGATE_RISK_NOT_CONNECTED",
    "CURRENT_ACCOUNT_FEES_EXECUTION_AND_FUNDING_NOT_CONNECTED",
    "STRATEGY_OUT_OF_SAMPLE_AND_LIVE_PAPER_VALIDATION_INCOMPLETE",
    "CONTINUOUS_INVALIDATION_COVERAGE_NOT_VERIFIED",
    "PERSISTENT_HOST_AND_SIGNAL_DELIVERY_NOT_VERIFIED",
]


def observation_snapshot(collected: dict) -> dict:
    binding = PRODUCT_IDENTITIES["perpetual_futures"]
    if not isinstance(collected, dict):
        raise MarketError("collector result must be an object")
    for key, expected in binding.items():
        if collected.get(key) != expected:
            raise MarketError(f"collector identity mismatch: {key}")
    observed = collected["observed_at"]
    quote = collected["quote"]
    if not isinstance(quote, dict):
        raise MarketError("collector quote must be an object")
    for key, expected in binding.items():
        if key in quote and quote[key] != expected:
            raise MarketError(f"collector quote identity mismatch: {key}")
    websocket = quote.get("transport") == "websocket"
    source_id = ("bingx-usdt-m-perpetual-websocket-last-v1" if websocket
                 else "bingx-public-perpetual-candidate-schema-v1")
    histories = {
        timeframe: {
            **binding, "source_id": source_id,
            "origin_at": collected.get("origins", {}).get(timeframe, "1970-01-01T00:00:00+00:00"),
            "boundary_origin_live_verified": False,
            "observed_at": observed,
            "volume_kind": "unverified_perpetual_volume",
            "bars": collected["frames"][timeframe],
        }
        for timeframe in TIMEFRAMES
    }
    snapshot = {
        "schema_version": 2, **binding,
        "source_symbol": collected.get("source_symbol", "BTC-USDT"),
        "source_id": source_id, "data_mode": "live_observation",
        "as_of": observed, "live_schema_verified": False,
        "quote": {
            **binding, "source_id": source_id,
            "timestamp_kind": "exchange_event",
            "price": quote["last"],
            **{key: quote[key] for key in ("bid", "ask") if key in quote},
            "event_at": quote["timestamp"], "observed_at": observed,
        },
        "histories": histories,
        "account": None, "costs": None,
    }
    # Current-candle and history validation is repeated at the integration boundary.
    parse_market(snapshot, allow_live_observation=True)
    return snapshot


def check_observation_clock(snapshot: dict, now: datetime) -> None:
    fresh(snapshot["as_of"], now, "collector observation time")
    fresh(snapshot["quote"]["event_at"], now, "collector quote event time")
    for timeframe, seconds in TIMEFRAMES.items():
        history = snapshot["histories"][timeframe]
        fresh(history["observed_at"], now, f"collector {timeframe} observation time")
        origin = time_value(history["origin_at"], "boundary origin")
        latest = origin + timedelta(seconds=((now-origin).total_seconds() // seconds)*seconds)
        if time_value(history["bars"][-1]["close_at"], "last closed bar") != latest:
            raise GateError(f"collector {timeframe}: next candle closed during processing")


def evaluate_observation(snapshot: dict, settings: dict) -> dict:
    evaluation = evaluate(snapshot, settings, allow_live_observation=True)
    internal_candidate = evaluation.get("signal") is not None
    evaluation.update(signal=None, decision="NO_ENTRY", publication_status="BLOCKED",
                      candidate_survived_research_gates=internal_candidate,
                      publication_blockers=PUBLICATION_BLOCKERS,
                      all_agreement_conditions_verified=False,
                      external_delivery_attempted=False,
                      exchange_orders_submitted=False)
    return evaluation


def withdraw_observation(directory: Path) -> bool:
    removed = True
    for name in ("latest_observation.json", "latest_evaluation.json", "collector_status.json"):
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            removed = False
    return removed


def collect_once(client, directory: Path, settings: dict, *, now=None) -> dict:
    directory = Path(directory)
    collected_at = clock_value(now).isoformat()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = observation_snapshot(client.collect())
        check_observation_clock(snapshot, clock_value(now))
        evaluation = evaluate_observation(snapshot, settings)
        check_observation_clock(snapshot, clock_value(now))
        atomic_json(directory / "latest_observation.json", snapshot)
        atomic_json(directory / "latest_evaluation.json", evaluation)
        status = {
            "state": "OBSERVING_PUBLIC_MARKET", "observed_at": snapshot["as_of"],
            "provider": snapshot["provider"], "product": snapshot["product"],
            "symbol": snapshot["symbol"], "price_kind": snapshot["price_kind"],
            "live_schema_verified": False, "live_oco_publication": False,
            "blockers": PUBLICATION_BLOCKERS,
        }
    except (MarketError, GateError, KeyError, TypeError, ValueError, ArithmeticError, OSError) as error:
        # Withdraw the prior observation so a recovery cannot revive a stale proposal.
        removed = withdraw_observation(directory)
        status = {
            "state": "STORAGE_UNAVAILABLE" if isinstance(error, OSError) else "MARKET_UNAVAILABLE",
            "observed_at": collected_at, "previous_observation_withdrawn": removed,
            "live_schema_verified": False, "live_oco_publication": False,
            "error_class": type(error).__name__,
            "reason": "READ_ONLY_MARKET_CONTRACT_OR_CONNECTIVITY_FAILED",
            "blockers": PUBLICATION_BLOCKERS,
        }
    try:
        atomic_json(directory / "collector_status.json", status)
    except OSError:
        removed = withdraw_observation(directory)
        status = {**status, "state": "STORAGE_UNAVAILABLE", "status_persisted": False,
                  "previous_observation_withdrawn": removed,
                  "reason": "LOCAL_PERSISTENCE_UNAVAILABLE"}
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--data-dir", type=Path, default=Path("runtime/perpetual"))
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "config/strategy.research.json")
    args = parser.parse_args(argv)
    if not 1 <= args.interval <= 15:
        parser.error("--interval must be 1..15 seconds")
    client = BingXPublicClient()
    if args.probe:
        try:
            result = client.probe()
        except MarketError as error:
            result = {"state": "MARKET_UNAVAILABLE", "error_class": type(error).__name__,
                      "reason": str(error), "live_schema_verified": False,
                      "live_oco_publication": False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        unavailable = result.get("state") == "MARKET_UNAVAILABLE" or result.get("status") == "UNAVAILABLE"
        return 2 if unavailable else 0
    settings = load_json(args.config)
    stopped = threading.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, lambda *_: stopped.set())
    failures = 0
    while not stopped.is_set():
        result = collect_once(client, args.data_dir, settings)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if result["state"] == "OBSERVING_PUBLIC_MARKET" else 2
        failures = failures + 1 if result["state"] != "OBSERVING_PUBLIC_MARKET" else 0
        stopped.wait(min(30, args.interval * 2 ** min(failures, 3)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

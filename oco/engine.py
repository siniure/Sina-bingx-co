"""Market -> deterministic study -> barriers -> costs -> hypothetical account risk."""
from __future__ import annotations

from decimal import Decimal

from core import GateError
from .market import parse_market, check_quote_path, IDENTITY
from .risk import costs_for, size_research
from .strategy import analyze, VERSION


def evaluate(snapshot: dict, settings: dict) -> dict:
    result = {"schema_version": 1, "mode": "REPLAY_ONLY", "status": "BLOCKED",
              "decision": "NO_ENTRY", "signal": None, "strategy_version": VERSION,
              "source_authenticated": False, "strategy_profitability_validated": False,
              "all_agreement_conditions_verified": False, "external_delivery_attempted": False,
              "exchange_orders_submitted": False, "position_or_fill_inferred": False,
              "live_activation_available": False}
    try:
        market = parse_market(snapshot)
        result.update(IDENTITY)
        result.update(as_of=market.as_of.isoformat(), source_id=market.source_id, data_mode=market.data_mode,
                      quote={"price": str(market.price), "event_at": market.event_at.isoformat()})
        analysis = analyze(market, settings)
        result.update(analysis)
        result["status"] = "SIMULATED"
        signal = analysis["signal"]
        if signal is None:
            return result
        try:
            check_quote_path(market, signal)
        except GateError:
            result["cancelled_signal_id"] = signal["id"]
            raise
        costs = costs_for(signal, snapshot.get("costs"), market.as_of, Decimal(settings["minimum_net_rr"]))
        signal["costs"] = costs
        try:
            risk = size_research(signal, costs, snapshot.get("account"), settings, market.as_of)
        except GateError as exc:
            # Chart proposals may exist without a safe account-sized quantity.
            risk = {"status": "ACCOUNT_RISK_BLOCKED", "quantity": None, "reason": str(exc)}
        signal["risk"] = risk
        result["reason"] = "RESEARCH_ONLY_REQUIRES_SOURCE_AND_ACCOUNT_VERIFICATION"
    except (GateError, ValueError, TypeError, KeyError, ArithmeticError) as exc:
        result.update(status="BLOCKED", decision="NO_ENTRY", signal=None, reason=str(exc))
    return result

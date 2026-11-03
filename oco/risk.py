"""Decimal cost and hypothetical sizing. All replay account data is asserted."""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from core import GateError, canonical_decimal, decimal, verified_window
from .market import identity, fresh

D = Decimal


def risk_score(fraction: Decimal) -> Decimal:
    fraction = decimal(fraction, "risk fraction")
    bounds = [D(0), D("0.0025"), D("0.005"), D("0.01"), D("0.02"), D("0.03"),
              D("0.05"), D("0.075"), D("0.10"), D("0.15"), D("0.20")]
    if fraction > bounds[-1]:
        raise GateError("risk exceeds agreed 20% ceiling")
    for index, (lo, hi) in enumerate(zip(bounds, bounds[1:])):
        if fraction <= hi:
            return D(index*10) + (fraction-lo)/(hi-lo)*10
    raise AssertionError("unreachable")


def costs_for(signal: dict, record: dict, now, minimum_rr: Decimal) -> dict:
    if not isinstance(record, dict):
        raise GateError("current product costs unavailable")
    identity(record, "costs", price_kind=False)
    verified_window(record, now, 3600, "costs")
    if not isinstance(record.get("evidence"), str) or not record["evidence"].strip():
        raise GateError("cost evidence required")
    entry_fee = decimal(record.get("entry_fee_rate"), "entry_fee_rate")
    exit_fee = decimal(record.get("exit_fee_rate"), "exit_fee_rate")
    if entry_fee+exit_fee >= D("0.05"):
        raise GateError("fee units implausible")
    if entry_fee+exit_fee == 0 and record.get("zero_fee_verified") is not True:
        raise GateError("zero fees require evidence")
    reserves = sum((decimal(record.get(key), key) for key in
                    ("execution_reserve_per_unit", "funding_reserve_per_unit")), D(0))
    entry, sl, tp = (D(signal[key]) for key in ("entry_reference", "sl", "tp"))
    lo, hi = (D(signal["entry_range"][key]) for key in ("min", "max"))
    if not all(value.is_finite() and value > 0 for value in (entry, sl, tp, lo, hi)):
        raise GateError("positive finite order prices required")
    valid = (sl < lo <= entry <= hi < tp if signal["direction"] == "long" else
             tp < lo <= entry <= hi < sl if signal["direction"] == "short" else False)
    if not valid:
        raise GateError("invalid direction or entry range geometry")
    worst = hi if signal["direction"] == "long" else lo

    def net(at):
        stop_cost = at*entry_fee+sl*exit_fee+reserves
        target_cost = at*entry_fee+tp*exit_fee+reserves
        loss = abs(at-sl)+stop_cost
        reward = abs(tp-at)-target_cost
        if loss <= 0 or reward <= 0:
            raise GateError("nonpositive net reward/loss")
        return loss, reward, reward/loss, stop_cost, target_cost

    loss, reward, ratio, stop_cost, target_cost = net(entry)
    worst_loss, worst_reward, worst_ratio, _, _ = net(worst)
    if min(ratio, worst_ratio) < minimum_rr:
        raise GateError("net reward/risk below minimum at reference or worst entry bound")
    return {"net_loss_per_unit": canonical_decimal(loss), "net_reward_per_unit": canonical_decimal(reward),
            "net_rr": canonical_decimal(ratio), "worst_net_rr": canonical_decimal(worst_ratio),
            "worst_net_loss_per_unit": canonical_decimal(worst_loss),
            "stop_cost_per_unit": canonical_decimal(stop_cost),
            "target_cost_per_unit": canonical_decimal(target_cost),
            "fee_basis": "entry notional at entry; exit notional at respective exit; reserves per BTC",
            "cost_evidence": record["evidence"], "verified_at": record["verified_at"]}


def size_research(signal: dict, costs: dict, account: dict | None, settings: dict, now) -> dict:
    if account is None:
        return {"status": "ACCOUNT_RISK_UNAVAILABLE", "quantity": None,
                "reason": "Fresh complete Standard account risk snapshot required"}
    identity(account, "account", price_kind=False)
    fresh(account.get("as_of"), now, "account as_of")
    if account.get("coverage_complete") is not True:
        raise GateError("account risk coverage incomplete")
    for field in ("positions_evidence", "entry_orders_evidence"):
        if not isinstance(account.get(field), str) or not account[field].strip():
            raise GateError("positions and active entry order evidence required")
    equity = decimal(account.get("equity"), "equity", positive=True)
    available = decimal(account.get("available_margin"), "available_margin")
    exposures = account.get("exposures")
    if not isinstance(exposures, list):
        raise GateError("complete exposure list required")
    existing = D(0)
    identifiers = set()
    for exposure in exposures:
        if not isinstance(exposure, dict) or exposure.get("kind") not in {"position", "entry_order"}:
            raise GateError("invalid exposure")
        reference = exposure.get("id")
        if not isinstance(reference, str) or not reference or reference in identifiers:
            raise GateError("unique exposure identity required")
        identifiers.add(reference)
        if exposure.get("stop_verified") is not True:
            raise GateError("exposure without verified stop has unbounded risk")
        existing += decimal(exposure.get("additional_loss_to_stop_usdt"), "additional loss to stop")
    fraction = decimal(settings.get("risk_fraction"), "risk_fraction", positive=True)
    aggregate = decimal(settings.get("aggregate_risk_fraction"), "aggregate_risk_fraction", positive=True)
    if max(fraction, aggregate) > D("0.20"):
        raise GateError("risk exceeds immutable agreed ceiling of 20%")
    remaining = aggregate*equity-existing
    budget = min(fraction*equity, remaining)
    if budget <= 0:
        raise GateError("aggregate account risk budget exhausted")
    step = decimal(settings.get("qty_step"), "qty_step", positive=True)
    minimum = decimal(settings.get("minimum_qty"), "minimum_qty", positive=True)
    leverage = decimal(settings.get("leverage"), "leverage", positive=True)
    if leverage < 1:
        raise GateError("research leverage must be at least one")
    loss = D(costs["worst_net_loss_per_unit"])
    quantity = (budget/loss/step).to_integral_value(rounding=ROUND_FLOOR)*step
    if quantity < minimum:
        raise GateError("risk-sized quantity below minimum")
    margin = D(signal["entry_range"]["max"])*quantity/leverage
    if margin > available:
        raise GateError("insufficient available margin at research leverage")
    planned_loss = quantity*loss
    actual_fraction = planned_loss/equity
    score = risk_score(actual_fraction)
    labels = ["بسیار کم", "کم", "کم تا متوسط", "متوسط", "متوسط رو به بالا", "زیاد", "بسیار زیاد", "شدید", "بحرانی", "هولناک"]
    label_index = max(0, int((score/D(10)).to_integral_value(rounding="ROUND_CEILING"))-1)
    return {"status": "HYPOTHETICAL_REPLAY_SIZE", "quantity": canonical_decimal(quantity),
            "equity": canonical_decimal(equity), "equity_at": account["as_of"],
            "risk_fraction": canonical_decimal(actual_fraction), "risk_percent": canonical_decimal(actual_fraction*100),
            "risk_loss_usdt": canonical_decimal(planned_loss),
            "risk_intensity_score": canonical_decimal(score), "risk_intensity_label_fa": labels[label_index],
            "existing_risk_usdt": canonical_decimal(existing),
            "aggregate_risk_usdt": canonical_decimal(existing+planned_loss),
            "estimated_initial_margin": canonical_decimal(margin),
            "leverage": canonical_decimal(leverage),
            "account_source_independently_verified": False,
            "liquidation_distance_review": "REQUIRED_BEFORE_REAL_ENTRY",
            "contract_specs_live_verified": False}

"""Product-bound replay risk checks. Fixtures do not authenticate an account."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core import GateError
from oco.market import IDENTITY
from oco.risk import costs_for, size_research


PERPETUAL = {"provider": "BingX", "product": "perpetual_futures",
             "symbol": "BTCUSDT", "price_kind": "last_price"}


class ProductRiskTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 11, 0, 0, 1, tzinfo=timezone.utc)
        self.signal = {"direction": "long", "entry_reference": "100",
                       "entry_range": {"min": "100", "max": "100"},
                       "sl": "90", "tp": "130"}
        self.costs = {**IDENTITY, "verified_at": self.now.isoformat(),
                      "expires_at": (self.now+timedelta(hours=1)).isoformat(),
                      "evidence": "SYNTHETIC cost fixture; not current exchange evidence",
                      "entry_fee_rate": "0", "exit_fee_rate": "0",
                      "zero_fee_verified": True,
                      "execution_reserve_per_unit": "0",
                      "funding_reserve_per_unit": "0"}
        self.account = {**IDENTITY, "as_of": self.now.isoformat(),
                        "coverage_complete": True, "equity": "10000",
                        "available_margin": "10000", "exposures": [],
                        "positions_evidence": "SYNTHETIC position snapshot",
                        "entry_orders_evidence": "SYNTHETIC active order snapshot"}
        self.settings = {"risk_fraction": "0.005", "aggregate_risk_fraction": "0.005",
                         "qty_step": "0.001", "minimum_qty": "0.001", "leverage": "10"}

    def test_legacy_standard_costs_and_sizing_remain_supported(self):
        costs = costs_for(self.signal, self.costs, self.now)
        sized = size_research(self.signal, costs, self.account, self.settings, self.now)
        self.assertEqual(sized["status"], "HYPOTHETICAL_REPLAY_SIZE")
        self.assertEqual(Decimal(sized["risk_loss_usdt"]), Decimal("50"))
        self.assertIs(sized["account_source_independently_verified"], False)
        self.assertIs(sized["contract_specs_live_verified"], False)

    def test_matching_perpetual_identity_accepts_replay_costs_and_size(self):
        record = {**self.costs, **PERPETUAL}
        account = {**self.account, **PERPETUAL}
        costs = costs_for(self.signal, record, self.now, expected_identity=PERPETUAL)
        sized = size_research(self.signal, costs, account, self.settings, self.now,
                              expected_identity=PERPETUAL)
        self.assertEqual(sized["status"], "HYPOTHETICAL_REPLAY_SIZE")
        self.assertIs(sized["account_source_independently_verified"], False)

    def test_standard_and_perpetual_cost_records_cannot_be_mixed(self):
        for record, selected in ((self.costs, PERPETUAL),
                                 ({**self.costs, **PERPETUAL}, IDENTITY)):
            with self.subTest(product=record["product"]):
                with self.assertRaises(GateError):
                    costs_for(self.signal, record, self.now, expected_identity=selected)

    def test_perpetual_records_are_not_accepted_by_legacy_standard_default(self):
        record = {**self.costs, **PERPETUAL}
        with self.assertRaises(GateError):
            costs_for(self.signal, record, self.now)

    def test_account_product_provider_and_symbol_bind_to_selection(self):
        costs = costs_for(self.signal, {**self.costs, **PERPETUAL}, self.now,
                          expected_identity=PERPETUAL)
        for field, wrong in (("product", "standard_futures"),
                             ("provider", "OtherExchange"), ("symbol", "WLDUSDT")):
            account = {**self.account, **PERPETUAL, field: wrong}
            with self.subTest(field=field):
                with self.assertRaises(GateError):
                    size_research(self.signal, costs, account, self.settings, self.now,
                                  expected_identity=PERPETUAL)

    def test_explicit_exposure_identity_cannot_hide_another_product(self):
        costs = costs_for(self.signal, {**self.costs, **PERPETUAL}, self.now,
                          expected_identity=PERPETUAL)
        exposure = {**PERPETUAL, "kind": "position", "id": "p1",
                    "stop_verified": True, "additional_loss_to_stop_usdt": "45"}
        account = {**self.account, **PERPETUAL, "exposures": [exposure]}
        sized = size_research(self.signal, costs, account, self.settings, self.now,
                              expected_identity=PERPETUAL)
        self.assertEqual(Decimal(sized["aggregate_risk_usdt"]), Decimal("50"))
        account["exposures"][0]["product"] = "standard_futures"
        with self.assertRaises(GateError):
            size_research(self.signal, costs, account, self.settings, self.now,
                          expected_identity=PERPETUAL)

    def test_legacy_exposure_inherits_complete_parent_account_identity(self):
        costs = costs_for(self.signal, self.costs, self.now)
        self.account["exposures"] = [{"kind": "entry_order", "id": "o1",
                                      "stop_verified": True,
                                      "additional_loss_to_stop_usdt": "45"}]
        sized = size_research(self.signal, costs, self.account, self.settings, self.now)
        self.assertEqual(Decimal(sized["risk_loss_usdt"]), Decimal("5"))
        self.assertEqual(Decimal(sized["aggregate_risk_usdt"]), Decimal("50"))

    def test_exact_one_point_five_threshold_is_accepted_for_both_directions(self):
        for direction, stop, target in (("long", "90", "115"),
                                        ("short", "110", "85")):
            signal = {**self.signal, "direction": direction, "sl": stop, "tp": target}
            with self.subTest(direction=direction):
                result = costs_for(signal, self.costs, self.now)
                self.assertEqual(Decimal(result["worst_net_rr"]), Decimal("1.50"))

    def test_ratio_below_threshold_cannot_pass_by_display_rounding(self):
        for direction, stop, target in (("long", "90", "114.9999999999999"),
                                        ("short", "110", "85.0000000000001")):
            signal = {**self.signal, "direction": direction, "sl": stop, "tp": target}
            with self.subTest(direction=direction):
                with self.assertRaises(GateError):
                    costs_for(signal, self.costs, self.now)

    def test_worst_entry_bound_must_also_pass_threshold(self):
        signal = {**self.signal, "tp": "115",
                  "entry_range": {"min": "100", "max": "100.00001"}}
        with self.assertRaises(GateError):
            costs_for(signal, self.costs, self.now)

    def test_callers_cannot_lower_the_agreed_minimum(self):
        for minimum in (Decimal("1.4999"), "1", True, "NaN"):
            with self.subTest(minimum=minimum):
                with self.assertRaises(GateError):
                    costs_for(self.signal, self.costs, self.now, minimum)

    def test_stale_account_or_missing_stops_still_block_sizing(self):
        costs = costs_for(self.signal, self.costs, self.now)
        for operation in ("stale", "missing_stop", "missing_evidence", "incomplete"):
            account = copy.deepcopy(self.account)
            if operation == "stale":
                account["as_of"] = (self.now-timedelta(seconds=16)).isoformat()
            elif operation == "missing_stop":
                account["exposures"] = [{"kind": "position", "id": "p1",
                                          "additional_loss_to_stop_usdt": "1"}]
            elif operation == "missing_evidence":
                account.pop("entry_orders_evidence")
            else:
                account["coverage_complete"] = False
            with self.subTest(operation=operation):
                with self.assertRaises(GateError):
                    size_research(self.signal, costs, account, self.settings, self.now)

    def test_expired_or_missing_cost_evidence_still_blocks(self):
        for operation in ("expired", "missing_evidence", "unverified_zero", "wrong_units"):
            record = copy.deepcopy(self.costs)
            if operation == "expired":
                record["expires_at"] = self.now.isoformat()
            elif operation == "missing_evidence":
                record["evidence"] = ""
            elif operation == "unverified_zero":
                record.pop("zero_fee_verified")
            else:
                record["entry_fee_rate"] = "0.05"
            with self.subTest(operation=operation):
                with self.assertRaises(GateError):
                    costs_for(self.signal, record, self.now)

    def test_order_prices_reject_bool_float_and_nonfinite_values(self):
        for field, invalid in (("entry_reference", True), ("sl", 90.0), ("tp", "NaN")):
            signal = {**self.signal, field: invalid}
            with self.subTest(field=field):
                with self.assertRaises(GateError):
                    costs_for(signal, self.costs, self.now)


if __name__ == "__main__":
    unittest.main()

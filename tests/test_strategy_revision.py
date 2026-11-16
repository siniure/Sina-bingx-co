"""Regressions for the agreement revision; fixtures remain synthetic."""
import unittest
from dataclasses import replace
from decimal import Decimal

from core import GateError
from oco.demo import synthetic_snapshot
from oco.market import parse_market
from oco.strategy import VERSION, analyze, number


def settings(**updates):
    return {"version": VERSION, "research_tick_size": "0.1", "minimum_net_rr": "1.50",
            **updates}


class StrategyRevisionTests(unittest.TestCase):
    def candidate(self, direction="long", config=None):
        result = analyze(parse_market(synthetic_snapshot(direction)), config or settings())
        self.assertEqual(result["decision"], "RESEARCH_CANDIDATE", result)
        return result

    def test_long_stop_adds_one_buffer_to_structural_low(self):
        result = self.candidate()
        signal = result["signal"]
        volatility = number(result["timeframes"]["5m"]["atr14_rma"])
        anchor = Decimal(signal["structural_stop"])
        actual_buffer = Decimal(signal["stop_buffer"])
        self.assertEqual(anchor, Decimal("1100"))
        self.assertGreaterEqual(actual_buffer, volatility*Decimal("0.25"))
        self.assertLess(actual_buffer, volatility*Decimal("0.25")+Decimal("0.1"))
        self.assertEqual(Decimal(signal["sl"]), anchor-actual_buffer)
        self.assertTrue(signal["buffers_applied_once"])

    def test_short_stop_adds_one_buffer_to_structural_high(self):
        result = self.candidate("short")
        signal = result["signal"]
        volatility = number(result["timeframes"]["5m"]["atr14_rma"])
        anchor = Decimal(signal["structural_stop"])
        actual_buffer = Decimal(signal["stop_buffer"])
        self.assertEqual(anchor, Decimal("1100"))
        self.assertGreaterEqual(actual_buffer, volatility*Decimal("0.25"))
        self.assertLess(actual_buffer, volatility*Decimal("0.25")+Decimal("0.1"))
        self.assertEqual(Decimal(signal["sl"]), anchor+actual_buffer)

    def test_configured_buffer_is_applied_once_and_auditable(self):
        result = self.candidate(config=settings(stop_buffer_atr_fraction="0.40",
                                               target_buffer_atr_fraction="0.15"))
        signal = result["signal"]
        volatility = number(result["timeframes"]["5m"]["atr14_rma"])
        self.assertEqual(signal["stop_buffer_atr"], "0.4")
        self.assertEqual(signal["target_buffer_atr"], "0.15")
        self.assertGreaterEqual(Decimal(signal["stop_buffer"]), volatility*Decimal("0.40"))
        self.assertLess(Decimal(signal["stop_buffer"]), volatility*Decimal("0.40")+Decimal("0.1"))
        self.assertEqual(signal["target_level"], signal["structural_target"])
        self.assertGreaterEqual(Decimal(signal["target_buffer"]), volatility*Decimal("0.15"))
        self.assertLess(Decimal(signal["target_buffer"]), volatility*Decimal("0.15")+Decimal("0.1"))

    def test_signal_identity_and_volume_kind_are_product_specific(self):
        market = parse_market(synthetic_snapshot())
        standard = analyze(market, settings())
        perpetual_raw = {**market.raw, "product": "perpetual_futures", "price_kind": "last_price"}
        perpetual = analyze(replace(market, raw=perpetual_raw), settings())
        self.assertNotEqual(standard["signal"]["id"], perpetual["signal"]["id"])
        self.assertEqual(standard["signal"]["product"], "standard_futures")
        self.assertEqual(perpetual["signal"]["product"], "perpetual_futures")
        self.assertEqual(standard["timeframes"]["5m"]["volume_kind"], "unverified_optional_index_volume")
        self.assertEqual(perpetual["timeframes"]["5m"]["volume_kind"], "unverified_perpetual_volume")
        self.assertIsNone(perpetual["timeframes"]["5m"]["volume"])

    def test_perpetual_identity_cannot_claim_standard_index_price(self):
        market = parse_market(synthetic_snapshot())
        inconsistent = replace(market, raw={**market.raw, "product": "perpetual_futures"})
        with self.assertRaises(GateError):
            analyze(inconsistent, settings())

    def test_minimum_cannot_be_lowered_below_one_point_five(self):
        market = parse_market(synthetic_snapshot())
        for minimum in ("1", "1.49", "1.4999999999"):
            with self.subTest(minimum=minimum), self.assertRaises(GateError):
                analyze(market, settings(minimum_net_rr=minimum))
        self.assertEqual(analyze(market, settings(minimum_net_rr="1.50"))["decision"],
                         "RESEARCH_CANDIDATE")

    def test_revision_remains_explicitly_research_only(self):
        result = self.candidate()
        self.assertEqual(VERSION, "bingx-retest-research-0.2")
        self.assertEqual(result["stage"], "RESEARCH_ONLY")
        self.assertIs(result["strategy_profitability_validated"], False)
        self.assertEqual(result["signal"]["stage"], "RESEARCH_ONLY")
        self.assertIs(result["signal"]["strategy_profitability_validated"], False)


if __name__ == "__main__":
    unittest.main()

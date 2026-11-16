import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path

from oco.collector import collect_once, observation_snapshot
from oco.demo import synthetic_snapshot
from oco.engine import evaluate
from oco.market import PRODUCT_IDENTITIES, parse_market
from oco.perpetual_market import MarketError

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config/strategy.research.json").read_text())
FIXTURE_TIME = datetime.fromisoformat(synthetic_snapshot()["as_of"])


def collected_fixture():
    source = synthetic_snapshot("neutral")
    binding = PRODUCT_IDENTITIES["perpetual_futures"]
    return {
        **binding, "source_symbol": "BTC-USDT", "observed_at": source["as_of"],
        "quote": {"last": source["quote"]["price"], "bid": source["quote"]["price"],
                  "ask": source["quote"]["price"], "timestamp": source["quote"]["event_at"]},
        "frames": {tf: item["bars"] for tf, item in source["histories"].items()},
    }


class Client:
    def __init__(self, value=None):
        self.value = value

    def collect(self):
        if self.value is None:
            raise MarketError("network unavailable")
        return copy.deepcopy(self.value)


class CollectorTests(unittest.TestCase):
    def test_perpetual_replay_keeps_product_through_strategy_and_risk(self):
        value = synthetic_snapshot()
        binding = PRODUCT_IDENTITIES["perpetual_futures"]
        value.update(binding)
        value["quote"].update(binding)
        value["costs"].update(binding)
        value["account"].update(binding)
        for history in value["histories"].values():
            history.update(binding)
        result = evaluate(value, SETTINGS)
        self.assertEqual(result["decision"], "RESEARCH_CANDIDATE")
        self.assertEqual(result["product"], "perpetual_futures")
        self.assertEqual(result["signal"]["product"], "perpetual_futures")
        self.assertEqual(result["signal"]["risk"]["status"], "HYPOTHETICAL_REPLAY_SIZE")
        self.assertFalse(result["live_activation_available"])

    def test_perpetual_snapshot_cannot_use_standard_costs(self):
        value = synthetic_snapshot()
        binding = PRODUCT_IDENTITIES["perpetual_futures"]
        value.update(binding)
        value["quote"].update(binding)
        for history in value["histories"].values():
            history.update(binding)
        result = evaluate(value, SETTINGS)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIsNone(result["signal"])

    def test_live_observation_requires_explicit_internal_route(self):
        snapshot = observation_snapshot(collected_fixture())
        self.assertEqual(evaluate(snapshot, SETTINGS)["status"], "BLOCKED")
        market = parse_market(snapshot, allow_live_observation=True)
        self.assertEqual(market.identity["product"], "perpetual_futures")

    def test_product_cannot_be_relabelled_standard(self):
        value = collected_fixture()
        value["product"] = "standard_futures"
        with self.assertRaises(MarketError):
            observation_snapshot(value)

    def test_invalid_history_remains_blocked(self):
        value = collected_fixture()
        value["frames"]["5m"][-1]["closed"] = False
        with self.assertRaises(ValueError):
            observation_snapshot(value)

    def test_explicit_quote_identity_cannot_be_relabelled(self):
        conflicts = {"provider": "OtherExchange", "product": "standard_futures",
                     "symbol": "WLDUSDT", "price_kind": "index_price"}
        for key, wrong in conflicts.items():
            with self.subTest(key=key):
                value = collected_fixture()
                value["quote"][key] = wrong
                with self.assertRaises(MarketError):
                    observation_snapshot(value)

    def test_explicit_candle_product_cannot_be_relabelled(self):
        value = collected_fixture()
        value["frames"]["5m"][-1]["product"] = "standard_futures"
        with self.assertRaises(ValueError):
            observation_snapshot(value)

    def test_observation_never_claims_signal_or_account_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            status = collect_once(Client(collected_fixture()), Path(folder), SETTINGS, now=FIXTURE_TIME)
            self.assertEqual(status["state"], "OBSERVING_PUBLIC_MARKET")
            self.assertFalse(status["live_oco_publication"])
            result = json.loads((Path(folder) / "latest_evaluation.json").read_text())
            self.assertIsNone(result["signal"])
            self.assertFalse(result["all_agreement_conditions_verified"])
            self.assertFalse(result["external_delivery_attempted"])

    def test_connectivity_failure_withdraws_previous_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            collect_once(Client(collected_fixture()), path, SETTINGS, now=FIXTURE_TIME)
            result = collect_once(Client(), path, SETTINGS, now=FIXTURE_TIME)
            self.assertEqual(result["state"], "MARKET_UNAVAILABLE")
            self.assertFalse((path / "latest_observation.json").exists())
            self.assertFalse((path / "latest_evaluation.json").exists())
            self.assertTrue((path / "collector_status.json").exists())

    def test_stale_observation_cannot_be_relabelled_current(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as folder:
            status = collect_once(Client(collected_fixture()), Path(folder), SETTINGS,
                                  now=FIXTURE_TIME + timedelta(seconds=16))
            self.assertEqual(status["state"], "MARKET_UNAVAILABLE")

    def test_quote_age_is_checked_against_wall_clock(self):
        from datetime import timedelta
        value = collected_fixture()
        value["quote"]["timestamp"] = (FIXTURE_TIME-timedelta(seconds=14)).isoformat()
        with tempfile.TemporaryDirectory() as folder:
            status = collect_once(Client(value), Path(folder), SETTINGS,
                                  now=FIXTURE_TIME+timedelta(seconds=14))
            self.assertEqual(status["state"], "MARKET_UNAVAILABLE")

    def test_candle_close_during_processing_requires_refresh(self):
        from datetime import timedelta
        value = collected_fixture()
        before = FIXTURE_TIME+timedelta(seconds=299)
        value["observed_at"] = before.isoformat()
        value["quote"]["timestamp"] = before.isoformat()
        with tempfile.TemporaryDirectory() as folder:
            status = collect_once(Client(value), Path(folder), SETTINGS,
                                  now=before+timedelta(seconds=2))
            self.assertEqual(status["state"], "MARKET_UNAVAILABLE")

    def test_storage_failure_withdraws_prior_success(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            collect_once(Client(collected_fixture()), path, SETTINGS, now=FIXTURE_TIME)
            with patch("oco.collector.atomic_json", side_effect=OSError("disk unavailable")):
                status = collect_once(Client(collected_fixture()), path, SETTINGS, now=FIXTURE_TIME)
            self.assertEqual(status["state"], "STORAGE_UNAVAILABLE")
            self.assertFalse(status["status_persisted"])
            self.assertFalse((path / "latest_observation.json").exists())
            self.assertFalse((path / "collector_status.json").exists())

    def test_conflicting_quote_path_identity_is_rejected(self):
        from oco.market import check_quote_path
        from oco.strategy import analyze
        value = synthetic_snapshot()
        market = parse_market(value)
        signal = analyze(market, SETTINGS)["signal"]
        value["quote_path"][0]["product"] = "perpetual_futures"
        with self.assertRaises(ValueError):
            check_quote_path(market, signal)


if __name__ == "__main__":
    unittest.main()

import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from core import GateError, START, STOP
from oco.demo import synthetic_snapshot
from oco.engine import evaluate
from oco.formatting import report_fa
from oco.journal import Journal
from oco.market import IDENTITY, parse_market
from oco.risk import costs_for, risk_score
from oco.service import Service, load_json
from oco.standard_account import ReadOnlyStandardClient, NoRedirect
from oco.strategy import pivots

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT/"config/strategy.research.json").read_text())


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = synthetic_snapshot()
        self.settings = copy.deepcopy(SETTINGS)

    def test_long_and_short_end_to_end_have_no_live_or_fill_claim(self):
        for direction in ("long", "short"):
            result = evaluate(synthetic_snapshot(direction), self.settings)
            self.assertEqual(result["decision"], "RESEARCH_CANDIDATE")
            self.assertEqual(result["signal"]["direction"], direction)
            self.assertGreaterEqual(Decimal(result["signal"]["costs"]["worst_net_rr"]), 2)
            for field in ("source_authenticated", "all_agreement_conditions_verified",
                          "exchange_orders_submitted", "position_or_fill_inferred", "live_activation_available"):
                self.assertIs(result[field], False)
            self.assertLessEqual(Decimal(result["signal"]["risk"]["risk_loss_usdt"]), 50)

    def test_timeframe_conflict_has_no_order_numbers_in_report(self):
        result = evaluate(synthetic_snapshot("neutral"), self.settings)
        self.assertIsNone(result["signal"])
        text = report_fa(result)
        self.assertIn("فعلاً ورود ندارد", text)
        self.assertNotIn("```", text)

    def test_each_timeframe_binding_is_checked(self):
        for tf in self.snapshot["histories"]:
            data = copy.deepcopy(self.snapshot)
            data["histories"][tf]["product"] = "perpetual_futures"
            self.assertEqual(evaluate(data, self.settings)["status"], "BLOCKED")

    def test_latest_higher_timeframe_cannot_be_stale_with_fresh_quote(self):
        for bar in self.snapshot["histories"]["12h"]["bars"]:
            for key in ("open_at", "close_at"):
                bar[key] = (datetime.fromisoformat(bar[key])-timedelta(hours=12)).isoformat()
        result = evaluate(self.snapshot, self.settings)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("latest closed boundary", result["reason"])

    def test_open_future_or_gapped_history_is_rejected(self):
        for operation in ("open", "future", "gap"):
            data = copy.deepcopy(self.snapshot)
            bars = data["histories"]["5m"]["bars"]
            if operation == "open":
                bars[-1]["closed"] = False
            elif operation == "future":
                bars[-1]["close_at"] = "2026-11-02T12:05:00+00:00"
                bars[-1]["open_at"] = "2026-11-02T12:00:00+00:00"
            else:
                del bars[-10]
            self.assertEqual(evaluate(data, self.settings)["status"], "BLOCKED")

    def test_live_flag_cannot_turn_imported_json_into_live_source(self):
        self.snapshot.update(data_mode="live", source_verified=True, real_feed_connected=True)
        self.assertEqual(evaluate(self.snapshot, self.settings)["status"], "BLOCKED")

    def test_receipt_time_cannot_replace_exchange_event_time(self):
        self.snapshot["quote"]["timestamp_kind"] = "receipt"
        self.assertEqual(evaluate(self.snapshot, self.settings)["status"], "BLOCKED")

    def test_old_quote_is_rejected_even_with_current_observed_time(self):
        self.snapshot["quote"]["event_at"] = "2026-11-02T11:59:00+00:00"
        self.assertEqual(evaluate(self.snapshot, self.settings)["status"], "BLOCKED")

    def test_confirmed_pivots_are_causal(self):
        bars = parse_market(self.snapshot).bars["5m"]
        before = pivots(bars[:397])
        self.assertEqual(before, pivots(bars, before=397))
        for item in before:
            self.assertLessEqual(datetime.fromisoformat(item["confirmed_at"]), bars[396].close_at)
        self.assertFalse(any(p["index"] >= 395 for p in before))

    def test_price_touch_then_recovery_cannot_revive_setup(self):
        self.snapshot["quote_path"][0]["price"] = "1200"
        result = evaluate(self.snapshot, self.settings)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIsNone(result["signal"])
        self.assertIn("invalidated", result["reason"])
        self.assertIn("cancelled_signal_id", result)

    def test_quote_path_gap_or_missing_coverage_blocks(self):
        for operation in ("missing", "sequence", "last_price", "source"):
            data = copy.deepcopy(self.snapshot)
            if operation == "missing":
                data.pop("quote_path")
            elif operation == "sequence":
                data["quote_path"][-1]["sequence"] += 1
            elif operation == "last_price":
                data["quote_path"][-1]["price"] = "1103"
            else:
                data["quote_path"][-1]["source_id"] = "other"
            self.assertEqual(evaluate(data, self.settings)["status"], "BLOCKED")

    def test_fresh_fee_proof_and_product_required(self):
        for operation in ("stale", "product", "zero"):
            data = copy.deepcopy(self.snapshot)
            if operation == "stale":
                data["costs"]["verified_at"] = "2026-11-02T10:00:00+00:00"
            elif operation == "product":
                data["costs"]["product"] = "swap"
            else:
                data["costs"]["entry_fee_rate"] = "0"
            self.assertEqual(evaluate(data, self.settings)["status"], "BLOCKED")

    def test_account_missing_or_old_never_produces_quantity(self):
        for operation in ("missing", "old", "incomplete", "missing_stop"):
            data = copy.deepcopy(self.snapshot)
            if operation == "missing":
                data.pop("account")
            elif operation == "old":
                data["account"]["as_of"] = "2026-11-02T10:00:00+00:00"
            elif operation == "incomplete":
                data["account"]["coverage_complete"] = False
            else:
                data["account"]["exposures"] = [{"kind": "position", "id": "p1", "stop_verified": False}]
            result = evaluate(data, self.settings)
            self.assertIsNotNone(result["signal"])
            self.assertIsNone(result["signal"]["risk"]["quantity"])

    def test_open_risk_consumes_shared_budget(self):
        self.snapshot["account"]["exposures"] = [{"kind": "position", "id": "p1", "stop_verified": True,
                                                   "additional_loss_to_stop_usdt": "45"}]
        risk = evaluate(self.snapshot, self.settings)["signal"]["risk"]
        self.assertLessEqual(Decimal(risk["risk_loss_usdt"]), 5)
        self.assertLessEqual(Decimal(risk["aggregate_risk_usdt"]), 50)

    def test_total_exposure_or_insufficient_margin_blocks_size(self):
        for operation in ("budget", "margin", "ceiling"):
            data, config = copy.deepcopy(self.snapshot), copy.deepcopy(self.settings)
            if operation == "budget":
                data["account"]["exposures"] = [{"kind": "entry_order", "id": "o1", "stop_verified": True,
                                                   "additional_loss_to_stop_usdt": "50"}]
            elif operation == "margin":
                data["account"]["available_margin"] = "1"
            else:
                config["risk_fraction"] = config["aggregate_risk_fraction"] = "0.21"
                config["maximum_agreed_risk_fraction"] = "1"
            risk = evaluate(data, config)["signal"]["risk"]
            self.assertEqual(risk["status"], "ACCOUNT_RISK_BLOCKED")
            self.assertIsNone(risk["quantity"])

    def test_mobile_report_numbers_are_separate_and_marked_synthetic(self):
        text = report_fa(evaluate(self.snapshot, self.settings))
        blocks = re.findall(r"```\n(.*?)\n```", text, re.S)
        self.assertEqual(len(blocks), 3)
        for block in blocks:
            self.assertRegex(block, r"^\d+(?:\.\d+)?$")
        self.assertIn("آزمایشی آفلاین", text)
        self.assertIn("+0300", text)


class FinancialArithmeticTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        self.costs = {**IDENTITY, "verified_at": self.now.isoformat(),
                      "expires_at": (self.now+timedelta(hours=1)).isoformat(), "evidence": "synthetic",
                      "entry_fee_rate": "0.001", "exit_fee_rate": "0.002",
                      "execution_reserve_per_unit": "0.2", "funding_reserve_per_unit": "0.3"}

    def test_entry_and_exit_fees_use_their_own_notionals(self):
        for direction, sl, tp, expected_loss, expected_reward in (
            ("long", "95", "120", "5.79", "19.16"),
            ("short", "105", "80", "5.81", "19.24")):
            signal = {"direction": direction, "entry_reference": "100", "sl": sl, "tp": tp,
                      "entry_range": {"min": "99", "max": "101"}}
            result = costs_for(signal, self.costs, self.now, Decimal(2))
            self.assertEqual(Decimal(result["net_loss_per_unit"]), Decimal(expected_loss))
            self.assertEqual(Decimal(result["net_reward_per_unit"]), Decimal(expected_reward))

    def test_good_reference_cannot_hide_bad_worst_bound_ratio(self):
        signal = {"direction": "long", "entry_reference": "100", "sl": "95", "tp": "114.8",
                  "entry_range": {"min": "99", "max": "101"}}
        with self.assertRaises(GateError):
            costs_for(signal, self.costs, self.now, Decimal(2))

    def test_wrong_order_geometry_cannot_be_hidden_with_absolute_distances(self):
        signal = {"direction": "long", "entry_reference": "100", "sl": "105", "tp": "120",
                  "entry_range": {"min": "99", "max": "101"}}
        with self.assertRaises(GateError):
            costs_for(signal, self.costs, self.now, Decimal(2))

    def test_risk_score_matches_agreement_boundaries_and_interpolation(self):
        for fraction, score in (("0", "0"), ("0.005", "20"), ("0.01", "30"),
                                ("0.02", "40"), ("0.05", "60"), ("0.10", "80"),
                                ("0.20", "100"), ("0.00375", "15")):
            self.assertEqual(risk_score(Decimal(fraction)), Decimal(score))
        with self.assertRaises(GateError):
            risk_score(Decimal("0.20001"))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.snapshot = synthetic_snapshot()
        self.result = evaluate(self.snapshot, SETTINGS)
        self.now = datetime.fromisoformat(self.snapshot["as_of"])
        self.journal = Journal(self.directory)

    def tearDown(self):
        self.temp.cleanup()

    def start(self):
        self.journal.control("paper-start", trusted_operator=True)

    def test_default_paused_no_preview_and_live_start_rejected(self):
        self.assertEqual(self.journal.record(self.result, now=self.now)["status"], "PAUSED")
        self.assertFalse((self.directory/"previews").exists())
        with self.assertRaises(GateError):
            self.journal.control(START, trusted_operator=True)
        with self.assertRaises(GateError):
            self.journal.control("paper-start", trusted_operator=False)

    def test_duplicate_survives_restart_and_archive_is_immutable(self):
        self.start()
        first = self.journal.record(self.result, now=self.now)
        contents = Path(first["path"]).read_bytes()
        self.assertEqual(Journal(self.directory).record(self.result, now=self.now)["status"], "DUPLICATE")
        self.assertEqual(Path(first["path"]).read_bytes(), contents)
        self.assertEqual(len(list((self.directory/"previews").glob("*.json"))), 1)

    def test_subprocess_stop_is_observed_by_existing_worker(self):
        self.start()
        subprocess.run([sys.executable, "-m", "oco", "stop", "--data-dir", str(self.directory)],
                       cwd=ROOT, check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(self.journal.record(self.result, now=self.now)["status"], "PAUSED")

    def test_stop_invalidates_prior_setups_even_after_paper_restart(self):
        self.start()
        self.journal.record(self.result, now=self.now)
        self.journal.control(STOP, trusted_operator=True)
        self.start()
        self.assertEqual(self.journal.record(self.result, now=self.now)["status"], "TERMINAL_SETUP")

    def test_invalidation_tombstone_survives_corrected_input(self):
        self.start()
        bad = copy.deepcopy(self.snapshot)
        bad["quote_path"][0]["price"] = "1200"
        self.journal.record(evaluate(bad, SETTINGS), now=self.now)
        self.assertEqual(self.journal.record(self.result, now=self.now)["status"], "TERMINAL_SETUP")

    def test_expired_setup_never_revives_when_clock_is_rewound(self):
        self.start()
        future = self.now+timedelta(minutes=16)
        self.assertEqual(self.journal.record(self.result, now=future)["status"], "EXPIRED")
        self.assertEqual(self.journal.record(self.result, now=self.now)["status"], "TERMINAL_SETUP")

    def test_stale_evaluation_is_not_archived_as_new_preview(self):
        self.start()
        self.assertEqual(self.journal.record(self.result, now=self.now+timedelta(seconds=20))["status"], "STALE_EVALUATION")
        self.assertFalse((self.directory/"previews").exists())

    def test_full_service_replay_and_no_live_json_acceptance(self):
        self.start()
        service = Service(self.directory, SETTINGS)
        result, receipt = service.step(self.snapshot, replay_clock=True)
        self.assertEqual(receipt["status"], "LOCAL_RESEARCH_PREVIEW_SAVED")
        self.assertTrue((self.directory/"latest_report_FA.md").exists())
        bad = copy.deepcopy(self.snapshot)
        bad["data_mode"] = "live"
        result, receipt = service.step(bad, replay_clock=True)
        self.assertIsNone(result["signal"])
        self.assertEqual(result["status"], "BLOCKED")

    def test_source_fault_replaces_latest_report_and_pauses_existing_worker(self):
        self.start()
        service = Service(self.directory, SETTINGS)
        service.step(self.snapshot, replay_clock=True)
        result, receipt = service.step(self.snapshot, now=self.now+timedelta(seconds=20))
        self.assertIsNone(result["signal"])
        self.assertEqual(service.journal.status()["state"], "PAUSED")
        report = (self.directory/"latest_report_FA.md").read_text()
        self.assertIn("فعلاً ورود ندارد", report)
        self.assertNotIn("```", report)

    def test_failed_recheck_withdraws_old_candidate_without_resurrection(self):
        self.start()
        self.journal.record(self.result, now=self.now)
        bad = copy.deepcopy(self.snapshot)
        bad["costs"]["entry_fee_rate"] = "0.04"
        self.journal.record(evaluate(bad, SETTINGS), now=self.now)
        self.assertEqual(self.journal.record(self.result, now=self.now)["status"], "TERMINAL_SETUP")

    def test_stop_prevents_delayed_worker_from_restoring_old_report(self):
        self.start()
        service = Service(self.directory, SETTINGS)
        service.step(self.snapshot, replay_clock=True)
        service.control(STOP)
        late = service._save_latest(self.result)
        self.assertIsNone(late["signal"])
        self.assertNotIn("```", (self.directory/"latest_report_FA.md").read_text())

    def test_json_duplicate_keys_and_nonfinite_are_rejected(self):
        for raw in ('{"as_of":1,"as_of":2}', '{"x":NaN}'):
            path = self.directory/"input.json"
            path.write_text(raw)
            with self.assertRaises((GateError, ValueError)):
                load_json(path)


class AccountClientTests(unittest.TestCase):
    def test_only_three_get_resources_and_signing_headers(self):
        requests = []
        def transport(request):
            requests.append(request)
            shape = {} if "/balance?" in request.full_url else []
            return json.dumps({"code": 0, "data": shape}).encode()
        client = ReadOnlyStandardClient("test-key", "test-secret", transport=transport, clock_ms=lambda: 1234567890000)
        for resource in ("positions", "orders", "balance"):
            result = client.query(resource)
            self.assertFalse(result["account_risk_coverage_complete"])
            self.assertFalse(result["timestamp_is_exchange_event"])
        for request in requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(urlsplit(request.full_url).hostname, "open-api.bingx.com")
            params = parse_qs(urlsplit(request.full_url).query)
            self.assertRegex(params["signature"][0], r"^[a-f0-9]{64}$")
            self.assertEqual(request.get_header("X-source-key"), "BX-AI-SKILL")
        for resource in ("place_order", "cancel_order", "withdraw", "/openApi/swap/v2/quote/klines"):
            with self.assertRaises(GateError):
                client.query(resource)
        self.assertEqual(len(requests), 3)

    def test_api_errors_never_echo_secret_or_raw_response(self):
        secret = "private-marker-not-to-log"
        client = ReadOnlyStandardClient("key", secret,
                                       transport=lambda _: json.dumps({"code": 100001, "msg": secret}).encode())
        with self.assertRaises(GateError) as error:
            client.query("balance")
        self.assertNotIn(secret, str(error.exception))

    def test_redirect_cannot_forward_signed_query(self):
        with self.assertRaises(GateError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://different.example")


if __name__ == "__main__":
    unittest.main()

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Controller, GateError, START, STOP, validate_proposal

NOW = datetime(2026, 11, 2, 14, 0, 5, tzinfo=timezone.utc)


def readiness():
    return {"source_verified": True, "real_feed_connected": True, "persistence_verified": True,
            "delivery_verified": True, "provider": "BingX", "product": "standard_futures",
            "symbol": "BTCUSDT", "price_kind": "index_price", "cadence_seconds": "5",
            "delivery_kind": "local_file", "provenance_id": "synthetic-test-source", "minimum_net_rr": "2",
            "verified_at": NOW.isoformat(), "expires_at": (NOW+timedelta(seconds=60)).isoformat()}


def proposal():
    return {"provenance_id": "synthetic-test-source", "direction": "short", "entry": "78030",
            "entry_range": {"min": "78020", "max": "78040"},
            "sl": "78140", "tp": "77640", "decision_at": NOW.isoformat(),
            "expires_at": (NOW+timedelta(minutes=10)).isoformat(),
            "signal_bar": {"timeframe": "5m", "closed": True,
                           "open_time": (NOW-timedelta(minutes=5, seconds=5)).isoformat(),
                           "close_time": (NOW-timedelta(seconds=5)).isoformat()},
            "invalidation_check": {"invalidated": False, "provenance_id": "synthetic-test-source",
                                   "checked_at": NOW.isoformat(), "through": NOW.isoformat(),
                                   "since": (NOW-timedelta(minutes=10)).isoformat(),
                                   "evidence": "synthetic continuous fixture; no real monitoring"},
            "costs": {"fee_rate_total": "0.00045", "execution_reserve_per_unit": "10", "funding_reserve_per_unit": "0",
                      "verification": {"provider": "BingX", "product": "standard_futures", "symbol": "BTCUSDT",
                                       "verified_at": NOW.isoformat(), "expires_at": (NOW+timedelta(hours=1)).isoformat(),
                                       "evidence": "synthetic fee fixture, not live rate"}},
            "source": {"schema_version": 1, "provider": "BingX", "product": "standard_futures",
                       "symbol": "BTCUSDT", "quote_currency": "USDT", "price_kind": "index_price",
                       "data_mode": "live", "source_url": "https://bingx.com/en/futures/forward/BTCUSDT",
                       "price": "78030", "observed_at": NOW.isoformat(), "source_timestamp": NOW.isoformat(),
                       "source_timestamp_kind": "exchange_event", "cache_status": "not_cached", "transport": "websocket",
                       "evidence": {"standard_selected": True, "selected_product_text": "Standard Futures",
                                    "selected_symbol": "BTCUSDT", "price_label": "Index Price", "price_text": "78030",
                                    "market_binding": "synthetic Standard fixture; no real exchange observation",
                                    "timestamp_trace": {"field": "time", "value": NOW.isoformat(), "context": "synthetic fixture"}}}}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.controller = Controller(Path(self.tmp.name)/"state.json", Path(self.tmp.name)/"alerts")

    def enable(self):
        self.controller.control(START, trusted_operator=True, readiness=readiness(), now=NOW)

    def test_default_paused_and_no_file(self):
        with self.assertRaisesRegex(GateError, "paused"):
            self.controller.submit(proposal(), readiness(), now=NOW)
        self.assertFalse((Path(self.tmp.name)/"alerts").exists())

    def test_untrusted_command_cannot_start_or_stop(self):
        for command in (START, STOP):
            with self.assertRaisesRegex(GateError, "trusted operator"):
                self.controller.control(command, readiness=readiness())

    def test_quotes_and_examples_do_not_start(self):
        for command in ('"'+START+'"', '«'+START+'»', 'مثال: '+START, START+' لطفا', 'X\n'+START):
            with self.assertRaises(GateError):
                self.controller.control(command, trusted_operator=True, readiness=readiness())

    def test_normalized_standalone_command(self):
        result = self.controller.control('  شروع\u200cOCO\tاتوماتیک\n', trusted_operator=True, readiness=readiness(), now=NOW)
        self.assertEqual(result['status'], 'STARTER_GATE_ENABLED')

    def test_readiness_required_and_hourly_rejected(self):
        for field in ('source_verified', 'real_feed_connected', 'persistence_verified', 'delivery_verified'):
            ready = readiness(); ready[field] = False
            with self.assertRaises(GateError):
                self.controller.control(START, trusted_operator=True, readiness=ready)
        ready = readiness(); ready['cadence_seconds'] = '3600'
        with self.assertRaisesRegex(GateError, 'hourly'):
            self.controller.control(START, trusted_operator=True, readiness=ready)

    def test_cannot_claim_chat_delivery(self):
        ready = readiness(); ready['delivery_kind'] = 'new_chatgpt_chat'
        with self.assertRaisesRegex(GateError, 'ChatGPT'):
            self.controller.control(START, trusted_operator=True, readiness=ready)

    def test_valid_candidate_costs_and_no_full_strategy_claim(self):
        result = validate_proposal(proposal(), readiness(), NOW)
        self.assertEqual(result['cost_per_unit'], '45.11350')
        self.assertEqual(result['net_loss_per_unit'], '155.11350')
        self.assertFalse(result['strategy_validated'])
        self.assertFalse(result['all_agreement_conditions_verified'])
        self.assertFalse(result['exchange_orders_submitted'])

    def test_immutable_file_and_duplicate_survive_restart(self):
        self.enable()
        result = self.controller.submit(proposal(), readiness(), now=NOW)
        self.assertEqual(result['status'], 'LOCAL_CANDIDATE_SAVED')
        self.assertTrue(Path(result['path']).exists())
        reloaded = Controller(self.controller.state_path, self.controller.output_dir)
        self.assertEqual(reloaded.submit(proposal(), readiness(), now=NOW)['status'], 'DUPLICATE')
        self.assertEqual(len(list(self.controller.output_dir.glob('*.json'))), 1)

    def test_stop_clears_only_local_queue_and_blocks(self):
        self.enable(); self.controller.submit(proposal(), readiness(), now=NOW)
        result = self.controller.control(STOP, trusted_operator=True)
        self.assertEqual(result['cleared_local_unfilled_alerts'], 1)
        self.assertFalse(result['positions_changed'])
        self.assertTrue(list(self.controller.output_dir.glob('*.json')))
        with self.assertRaisesRegex(GateError, 'paused'):
            self.controller.submit(proposal(), readiness(), now=NOW)

    def test_source_and_provenance_rejected(self):
        for mutate in (lambda p: p['source'].update(product='perpetual'),
                       lambda p: p.update(provenance_id='other'),
                       lambda p: p['source'].update(observed_at=(NOW-timedelta(seconds=16)).isoformat()),
                       lambda p: p['source'].update(source_timestamp=(NOW+timedelta(seconds=3)).isoformat())):
            p = proposal(); mutate(p)
            with self.assertRaises(GateError):
                validate_proposal(p, readiness(), NOW)

    def test_observation_only_not_live_confirmation(self):
        p = proposal(); p['source'].pop('source_timestamp'); p['source']['source_timestamp_kind'] = 'not_available'
        with self.assertRaisesRegex(GateError, 'exchange event timestamp'):
            validate_proposal(p, readiness(), NOW)

    def test_closed_bar_and_latest_boundary_required(self):
        for mutate in (lambda p: p['signal_bar'].update(closed=False),
                       lambda p: p['signal_bar'].update(close_time=(NOW+timedelta(seconds=295)).isoformat()),
                       lambda p: p['signal_bar'].update(timeframe='15m'),
                       lambda p: p['signal_bar'].update(open_time=(NOW-timedelta(seconds=605)).isoformat(), close_time=(NOW-timedelta(seconds=305)).isoformat())):
            p = proposal(); mutate(p)
            with self.assertRaises(GateError):
                validate_proposal(p, readiness(), NOW)

    def test_expired_and_future_decision_rejected(self):
        for mutate in (lambda p: p.update(expires_at=NOW.isoformat()),
                       lambda p: p.update(decision_at=(NOW+timedelta(seconds=3)).isoformat())):
            p = proposal(); mutate(p)
            with self.assertRaises(GateError):
                validate_proposal(p, readiness(), NOW)

    def test_price_direction_and_net_rr_rejected(self):
        for mutate in (lambda p: p.update(sl='77900'), lambda p: p.update(entry='77900'),
                       lambda p: p['costs'].update(fee_rate_total='0.01'),
                       lambda p: p.update(entry=float('nan'))):
            p = proposal(); mutate(p)
            with self.assertRaises(GateError):
                validate_proposal(p, readiness(), NOW)

    def test_old_controller_cannot_override_another_controller_stop(self):
        self.enable()
        second = Controller(self.controller.state_path, self.controller.output_dir)
        second.control(STOP, trusted_operator=True)
        with self.assertRaisesRegex(GateError, 'paused'):
            self.controller.submit(proposal(), readiness(), now=NOW)
        self.assertFalse(json.loads(self.controller.state_path.read_text())['enabled'])
        self.assertFalse(self.controller.output_dir.exists())

    def test_subprocess_stop_is_observed_by_existing_instance(self):
        self.enable()
        script = ('import sys; from pathlib import Path; from core import Controller, STOP; '
                  'Controller(Path(sys.argv[1]),Path(sys.argv[2])).control(STOP,trusted_operator=True)')
        subprocess.run([sys.executable, '-c', script, str(self.controller.state_path),
                        str(self.controller.output_dir)], check=True, capture_output=True,
                       timeout=10, cwd=Path(__file__).resolve().parents[1])
        with self.assertRaisesRegex(GateError, 'paused'):
            self.controller.submit(proposal(), readiness(), now=NOW)
        self.assertFalse(json.loads(self.controller.state_path.read_text())['enabled'])

    def test_abandoned_lock_fails_closed_and_is_not_deleted(self):
        self.enable()
        self.controller.lock_timeout_seconds = 0.02
        self.controller.lock_path.write_text('another operator owns this lock')
        with self.assertRaisesRegex(GateError, 'lock busy/abandoned'):
            self.controller.control(STOP, trusted_operator=True)
        self.assertEqual(self.controller.lock_path.read_text(), 'another operator owns this lock')
        self.assertFalse(self.controller.output_dir.exists())

    def test_live_subprocess_lock_is_respected(self):
        self.enable()
        script = ('import os,sys; from pathlib import Path; p=Path(sys.argv[1]); '
                  'fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); '
                  'print("LOCKED",flush=True); sys.stdin.readline(); os.close(fd); p.unlink()')
        process = subprocess.Popen([sys.executable, '-c', script, str(self.controller.lock_path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'LOCKED')
            self.controller.lock_timeout_seconds = 0.02
            with self.assertRaisesRegex(GateError, 'lock busy/abandoned'):
                self.controller.submit(proposal(), readiness(), now=NOW)
            process.communicate('RELEASE\n', timeout=5)
            self.assertEqual(process.returncode, 0)
            self.controller.control(STOP, trusted_operator=True)
        finally:
            if process.poll() is None:
                process.kill(); process.communicate(timeout=5)

    def test_quote_beyond_sl_or_tp_is_rejected(self):
        for price in ('79000', '77640', '77000'):
            p = proposal(); p['source'].update(price=price); p['source']['evidence']['price_text'] = price
            with self.assertRaisesRegex(GateError, 'TP or SL'):
                validate_proposal(p, readiness(), NOW)

    def test_quote_and_reference_must_be_inside_entry_range(self):
        for mutate in (lambda p: p.pop('entry_range'),
                       lambda p: p['entry_range'].update(min='78035'),
                       lambda p: p['entry_range'].update(max='78025'),
                       lambda p: (p['source'].update(price='78050'), p['source']['evidence'].update(price_text='78050'))):
            p = proposal(); mutate(p)
            with self.assertRaisesRegex(GateError, 'entry_range'):
                validate_proposal(p, readiness(), NOW)

    def test_historical_invalidation_evidence_and_coverage_required(self):
        for mutate in (lambda p: p.pop('invalidation_check'),
                       lambda p: p['invalidation_check'].update(invalidated=True),
                       lambda p: p['invalidation_check'].update(evidence=''),
                       lambda p: p['invalidation_check'].update(provenance_id='different-source'),
                       lambda p: p['invalidation_check'].update(checked_at=(NOW-timedelta(seconds=16)).isoformat()),
                       lambda p: p['invalidation_check'].update(through=(NOW-timedelta(seconds=1)).isoformat()),
                       lambda p: p['invalidation_check'].update(since=NOW.isoformat())):
            p = proposal(); mutate(p)
            with self.assertRaisesRegex(GateError, 'invalidation'):
                validate_proposal(p, readiness(), NOW)

    def test_readiness_expiry_is_rechecked_after_activation(self):
        self.enable()
        ready = readiness(); ready['verified_at'] = (NOW-timedelta(seconds=61)).isoformat()
        ready['expires_at'] = (NOW-timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(GateError, 'readiness'):
            self.controller.submit(proposal(), ready, now=NOW)
        self.assertFalse(self.controller.output_dir.exists())

    def test_readiness_requires_bounded_fresh_verification(self):
        for mutate in (lambda r: r.pop('verified_at'),
                       lambda r: r.update(expires_at=NOW.isoformat()),
                       lambda r: r.update(expires_at=(NOW+timedelta(hours=1)).isoformat()),
                       lambda r: r.update(verified_at=(NOW+timedelta(seconds=3)).isoformat())):
            ready = readiness(); mutate(ready)
            with self.assertRaises(GateError):
                self.controller.control(START, trusted_operator=True, readiness=ready, now=NOW)

    def test_fee_verification_matches_product_and_is_current(self):
        for mutate in (lambda p: p['costs'].pop('verification'),
                       lambda p: p['costs']['verification'].update(product='perpetual'),
                       lambda p: p['costs']['verification'].update(symbol='WLDUSDT'),
                       lambda p: p['costs']['verification'].update(evidence=''),
                       lambda p: p['costs']['verification'].update(expires_at=NOW.isoformat()),
                       lambda p: p['costs']['verification'].update(verified_at=(NOW-timedelta(hours=2)).isoformat())):
            p = proposal(); mutate(p)
            with self.assertRaisesRegex(GateError, 'costs'):
                validate_proposal(p, readiness(), NOW)

    def test_zero_fee_needs_explicit_verification(self):
        p = proposal(); p['costs']['fee_rate_total'] = '0'
        with self.assertRaisesRegex(GateError, 'zero fee'):
            validate_proposal(p, readiness(), NOW)
        p['costs']['zero_fee_verified'] = True
        self.assertEqual(validate_proposal(p, readiness(), NOW)['cost_per_unit'], '10')

    def test_equivalent_decimal_prices_have_one_duplicate_identity(self):
        self.enable()
        first = self.controller.submit(proposal(), readiness(), now=NOW)
        p = proposal(); p.update(entry='78030.0', sl='78140.00', tp='77640.000')
        second = self.controller.submit(p, readiness(), now=NOW)
        self.assertEqual(second['status'], 'DUPLICATE')
        self.assertEqual(second['id'], first['id'])
        self.assertEqual(len(list(self.controller.output_dir.glob('*.json'))), 1)

    def test_short_range_lower_bound_must_preserve_net_rr(self):
        p = proposal(); p['entry_range']['min'] = '77900'
        with self.assertRaisesRegex(GateError, 'worst-bound'):
            validate_proposal(p, readiness(), NOW)
        result = validate_proposal(proposal(), readiness(), NOW)
        self.assertEqual(result['worst_entry_in_range'], '78020')
        self.assertEqual(result['worst_net_loss_per_unit'], '165.10900')
        self.assertGreater(float(result['minimum_net_rr_in_range']), 2)
        self.assertLess(float(result['minimum_net_rr_in_range']), float(result['net_rr']))

    def test_long_range_upper_bound_must_preserve_net_rr(self):
        p = proposal(); p.update(direction='long', sl='77920', tp='78420')
        p['entry_range'] = {'min': '78020', 'max': '78100'}
        with self.assertRaisesRegex(GateError, 'worst-bound'):
            validate_proposal(p, readiness(), NOW)
        p['entry_range']['max'] = '78040'
        result = validate_proposal(p, readiness(), NOW)
        self.assertEqual(result['worst_entry_in_range'], '78040')
        self.assertGreater(float(result['minimum_net_rr_in_range']), 2)


if __name__ == '__main__':
    unittest.main()

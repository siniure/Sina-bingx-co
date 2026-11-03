"""Regressions for stale reports, host configuration and Telegram failures."""
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from core import GateError
from oco.hub import Hub
from oco.hub import main as hub_main
from oco.telegram_bot import TelegramAPI, TelegramFailure

ROOT = Path(__file__).resolve().parents[1]


class ReleaseHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.hub = Hub(self.temporary.name, view_token=secrets.token_urlsafe(32),
                       control_token=secrets.token_urlsafe(32))

    def tearDown(self):
        self.temporary.cleanup()

    def report(self, *, expired=False, terminal=False):
        now = datetime.now(timezone.utc)
        self.hub.service.control('paper-start')
        result = {'as_of': now.isoformat(), 'status': 'SIMULATED', 'mode': 'REPLAY_ONLY',
                  'source_authenticated': False, 'decision': 'RESEARCH_CANDIDATE',
                  'signal': {'id': 'safety-regression', 'expires_at': (now+timedelta(minutes=-1 if expired else 5)).isoformat()}}
        # Deliberately restore a once-valid report file: it must not revive a DB tombstone.
        with self.hub.service.journal.transaction() as db:
            db.execute("INSERT INTO signals VALUES (?, 'PREVIEW_SAVED', ?, ?, 1)",
                       (result['signal']['id'], result['signal']['expires_at'], json.dumps(result)))
            if terminal:
                db.execute("INSERT INTO terminal VALUES (?, 'INVALIDATED')", (result['signal']['id'],))
        (Path(self.temporary.name)/'latest_evaluation.json').write_text(json.dumps(result))
        return result

    def test_restored_fresh_file_cannot_revive_terminal_signal(self):
        self.report(terminal=True)
        self.assertIsNone(self.hub.latest()['signal'])

    def test_expiry_is_enforced_on_read_without_another_market_tick(self):
        self.report(expired=True)
        self.assertIsNone(self.hub.latest()['signal'])
        self.assertEqual(self.hub.records()[0]['state'], 'EXPIRED')
        with self.hub.service.journal.connection() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM terminal WHERE id='safety-regression'").fetchone())

    def test_unknown_signal_in_report_file_is_hidden(self):
        result = self.report()
        with self.hub.service.journal.transaction() as db:
            db.execute('DELETE FROM signals')
        self.assertIsNone(self.hub.latest()['signal'])

    def test_file_cannot_change_prices_of_an_archived_identity(self):
        result = self.report()
        result['signal']['entry_reference'] = '12345'
        (Path(self.temporary.name)/'latest_evaluation.json').write_text(json.dumps(result))
        self.assertIsNone(self.hub.latest()['signal'])

    def test_host_preflight_never_prints_configured_credentials(self):
        view, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        bot = '123456789:'+'s'*35
        run = subprocess.run([sys.executable, 'scripts/check_host.py'], cwd=ROOT,
            env={**os.environ, 'SINA_OCO_VIEW_TOKEN': view, 'SINA_OCO_CONTROL_TOKEN': control, 'TELEGRAM_BOT_TOKEN': bot},
            capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0)
        self.assertFalse(json.loads(run.stdout)['telegram_online_checked'])
        for secret in [view, control, bot]:
            self.assertNotIn(secret, run.stdout+run.stderr)

    def test_access_keys_with_control_characters_or_whitespace_are_rejected(self):
        for bad in ['x'*32+'\n', ' '*40, 'x'*16+'\t'+'y'*24, 'x'*31+'é']:
            with self.subTest(key_shape=repr(bad[-4:])):
                with self.assertRaises(GateError):
                    Hub(self.temporary.name, view_token=bad, control_token=secrets.token_urlsafe(32))

    def test_bad_port_is_a_controlled_cli_error(self):
        for port in ['not-a-port', '70000', '-2']:
            run = subprocess.run([sys.executable, '-m', 'oco.hub'], cwd=ROOT,
                                 env={**os.environ, 'PORT': port}, capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 2)
            self.assertNotIn('Traceback', run.stderr)

    def test_missing_windows_timezone_data_has_a_clear_startup_error(self):
        output = io.StringIO()
        with patch('oco.hub.ZoneInfo', side_effect=ZoneInfoNotFoundError()), patch('sys.stdout', output):
            self.assertEqual(hub_main(['--data-dir', self.temporary.name]), 2)
        self.assertIn('tzdata', output.getvalue())

    def test_short_telegram_calls_finish_before_dashboard_deadline(self):
        api = TelegramAPI('123456789:'+'x'*35)
        for method, payload, timeout in [('getMe', {}, 5), ('getUpdates', {'timeout': 20}, 25)]:
            response = io.BytesIO(b'{"ok":true,"result":[]}')
            with patch('oco.telegram_bot.urllib.request.build_opener') as build:
                build.return_value.open.return_value = response
                api.call(method, payload)
                self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], timeout)

    def test_telegram_safe_error_codes_keep_secrets_out_of_diagnostics(self):
        token = '123456789:'+'s'*35
        for number, expected in [(401, 'TELEGRAM_UNAUTHORIZED'), (409, 'TELEGRAM_POLLING_CONFLICT'), (429, 'TELEGRAM_RATE_LIMITED')]:
            api = TelegramAPI(token, transport=lambda *_: {'ok': False, 'error_code': number,
                'description': token, 'parameters': {'retry_after': 17}})
            with self.assertRaises(TelegramFailure) as caught:
                api.call('getMe', {})
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn(token, str(caught.exception))
            if number == 429:
                self.assertEqual(caught.exception.retry_after, 17)

    def test_http_error_body_is_redacted_but_retry_after_is_retained(self):
        token = '123456789:'+'s'*35
        body = io.BytesIO(json.dumps({'ok': False, 'description': token, 'parameters': {'retry_after': 23}}).encode())
        error = urllib.error.HTTPError('https://api.telegram.org/bot'+token+'/getUpdates', 429, token, {}, body)
        with patch('oco.telegram_bot.urllib.request.build_opener') as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(TelegramFailure) as caught:
                TelegramAPI(token).call('getUpdates', {'timeout': 20})
        self.assertEqual(caught.exception.retry_after, 23)
        self.assertNotIn(token, str(caught.exception))


if __name__ == '__main__':
    unittest.main()

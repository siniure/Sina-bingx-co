#!/usr/bin/env python3
"""Safe host preflight. Reads secrets from environment; never prints values/URLs.

Default is offline. --online-telegram uses only getMe/getWebhookInfo and sends
no messages, consumes no updates and changes no Telegram configuration.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import GateError
from oco.hub import Hub
from oco.telegram_bot import TelegramAPI, TelegramFailure


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online-telegram', action='store_true')
    parser.add_argument('--expect-bot', help='Optional public @username to compare; never a token')
    args = parser.parse_args(argv)
    view, control = os.getenv('SINA_OCO_VIEW_TOKEN', ''), os.getenv('SINA_OCO_CONTROL_TOKEN', '')
    result = {'access_keys_configured': Hub.valid_token(view) and Hub.valid_token(control) and view != control,
              'telegram_token_configured': False, 'telegram_online_checked': False,
              'market_feed_verified': False, 'live_publication': False}
    try:
        api = TelegramAPI(os.getenv('TELEGRAM_BOT_TOKEN', ''))
        result['telegram_token_configured'] = True
        if args.online_telegram:
            identity = api.call('getMe', {})
            if (not isinstance(identity, dict) or identity.get('is_bot') is not True or
                    not isinstance(identity.get('username'), str) or
                    not re.fullmatch(r'[A-Za-z0-9_]{5,32}', identity['username'])):
                raise GateError('bot identity was not verified')
            name = identity.get('username')
            if args.expect_bot and name != args.expect_bot.lstrip('@'):
                result['telegram_error'] = 'BOT_USERNAME_MISMATCH'
            else:
                webhook = api.call('getWebhookInfo', {})
                if not isinstance(webhook, dict) or not isinstance(webhook.get('url'), str):
                    raise GateError('webhook status was not verified')
                result.update(telegram_online_checked=True, bot_username=name,
                              webhook_active=bool(webhook['url']))
                if webhook['url']:
                    result['telegram_error'] = 'EXISTING_WEBHOOK_REQUIRES_OWNER_REVIEW'
    except TelegramFailure as exc:
        result.update(telegram_error=exc.code, retry_after_seconds=exc.retry_after)
    except GateError:
        result['telegram_error'] = 'TELEGRAM_CONFIG_OR_IDENTITY_UNVERIFIED'
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['access_keys_configured'] and result['telegram_token_configured'] and not result.get('telegram_error') else 2


if __name__ == '__main__':
    raise SystemExit(main())

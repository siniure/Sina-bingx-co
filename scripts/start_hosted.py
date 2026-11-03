#!/usr/bin/env python3
"""Render entry point. Host-injected secrets only; always starts paused."""
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from oco.hub import main

if __name__=='__main__':
    args=['--host','0.0.0.0','--port',os.environ.get('PORT','10000'),
          '--data-dir',os.environ.get('SINA_OCO_DATA_DIR','/var/data/oco'),'--trust-https-proxy']
    if os.environ.get('TELEGRAM_BOT_TOKEN'):
        args.append('--telegram')
    if os.environ.get('SINA_OCO_SNAPSHOT_FILE'):
        args += ['--snapshot',os.environ['SINA_OCO_SNAPSHOT_FILE']]
    raise SystemExit(main(args))

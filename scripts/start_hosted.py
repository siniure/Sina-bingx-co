#!/usr/bin/env python3
"""Render entry point for public BingX data and the authenticated dashboard."""
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from oco.hub import main

if __name__=='__main__':
    args=['--host','0.0.0.0','--port',os.environ.get('PORT','10000'),
          '--data-dir',os.environ.get('SINA_OCO_DATA_DIR','/var/data/oco'),
          '--trust-https-proxy','--perpetual']
    if os.environ.get('TELEGRAM_BOT_TOKEN'):
        args.append('--telegram')
    raise SystemExit(main(args))

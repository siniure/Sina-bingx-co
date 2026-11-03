#!/usr/bin/env python3
"""Start a local paused hub; generate private local keys without printing them."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=Path.home()/'SinaOCOData')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--configure-only',action='store_true')
    args=parser.parse_args()
    directory=args.data_dir.resolve()
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/'private-access-keys.json'
    if not path.exists():
        keys={'view_token':secrets.token_urlsafe(32),'control_token':secrets.token_urlsafe(32)}
        with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as handle:
            json.dump(keys,handle,indent=2)
    else:
        keys=json.loads(path.read_text(encoding='utf-8'))
    if (not all(isinstance(keys.get(field),str) and len(keys[field])>=32 for field in ('view_token','control_token'))
            or keys['view_token']==keys['control_token']):
        raise ValueError('Private local key file is malformed; refusing to start.')
    print('Local access keys are in: '+str(path),flush=True)
    print('Keep that file private. Do not upload it or paste keys into ChatGPT.',flush=True)
    if args.configure_only:return 0
    environment=os.environ.copy()
    environment.update(SINA_OCO_VIEW_TOKEN=keys['view_token'],SINA_OCO_CONTROL_TOKEN=keys['control_token'])
    print('Open http://127.0.0.1:'+str(args.port)+' and enter a local access key. Market publication stays blocked.',flush=True)
    return subprocess.call([sys.executable,'-m','oco.hub','--data-dir',str(directory),'--port',str(args.port)],cwd=ROOT,env=environment)


if __name__=='__main__':raise SystemExit(main())

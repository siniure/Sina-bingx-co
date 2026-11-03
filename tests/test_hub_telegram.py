import io
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from zipfile import ZipFile

from core import GateError, START, STOP
from oco.hub import Hub, Server, handler_for
from oco.journal import Journal
from oco.telegram_bot import OwnerBot, TelegramAPI

ROOT=Path(__file__).resolve().parents[1]


class HubAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.view=secrets.token_urlsafe(32)
        self.control=secrets.token_urlsafe(32)
        self.hub=Hub(self.temporary.name,view_token=self.view,control_token=self.control)
        self.server=Server(('127.0.0.1',0),handler_for(self.hub))
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        self.temporary.cleanup()

    def request(self,path,*,token=None,method='GET',body=None,origin=None):
        headers={}
        if token:headers['Authorization']='Bearer '+token
        if method=='POST':headers['Content-Type']='application/json'
        if origin:headers['Origin']=origin
        connection=HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        connection.request(method,path,body=body,headers=headers)
        response=connection.getresponse();data=response.read();status=response.status;result_headers=dict(response.getheaders())
        connection.close()
        return status,data,result_headers

    def test_public_shell_has_no_private_account_or_authentication_values(self):
        status,data,headers=self.request('/')
        self.assertEqual(status,200)
        self.assertNotIn(self.view.encode(),data);self.assertNotIn(self.control.encode(),data)
        self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertEqual(self.request('/healthz')[0],200)

    def test_all_private_get_routes_require_authentication(self):
        for path in ('/api/status','/api/evaluation','/api/records'):
            self.assertEqual(self.request(path)[0],401)
            self.assertEqual(self.request(path,token='invalid')[0],401)

    def test_view_token_cannot_stop_or_pair_or_start(self):
        for path in ('/api/control/stop','/api/control/paper-start','/api/telegram/pair'):
            self.assertEqual(self.request(path,token=self.view,method='POST',body='{}')[0],401)

    def test_authenticated_status_never_claims_live_readiness(self):
        status,data,headers=self.request('/api/status',token=self.view)
        result=json.loads(data)
        self.assertEqual(status,200);self.assertEqual(result['state'],'PAUSED')
        self.assertFalse(result['market_source_verified']);self.assertFalse(result['live_activation_available'])
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn(self.view,data.decode());self.assertNotIn(self.control,data.decode())

    def test_control_can_pause_paper_but_cannot_activate_live(self):
        status,data,_=self.request('/api/control/paper-start',token=self.control,method='POST',body='{}')
        self.assertEqual(status,200);self.assertEqual(json.loads(data)['state'],'REPLAY_RUNNING')
        status,data,_=self.request('/api/control/live-start',token=self.control,method='POST',body='{}')
        self.assertEqual(status,423);self.assertFalse(json.loads(data)['exchange_orders_submitted'])
        status,data,_=self.request('/api/control/stop',token=self.control,method='POST',body='{}')
        self.assertEqual(status,200);self.assertEqual(json.loads(data)['state'],'PAUSED')

    def test_restart_revokes_paper_enable_and_preserves_journal(self):
        self.hub.service.control('paper-start')
        restarted=Hub(self.temporary.name,view_token=self.view,control_token=self.control)
        self.assertEqual(restarted.status()['state'],'PAUSED')

    def test_cross_origin_control_and_arbitrary_commands_rejected(self):
        self.assertEqual(self.request('/api/control/stop',token=self.control,method='POST',body='{}',origin='https://different.example')[0],403)
        self.assertEqual(self.request('/api/control/stop',token=self.control,method='POST',body='{"command":"anything"}')[0],409)
        self.assertEqual(self.request('/api/control/stop',token=self.control,method='POST',body='bad json')[0],409)

    def test_file_traversal_and_secrets_routes_never_served(self):
        for path in ('/../core.py','/config/deployment_status.json','/runtime/journal.sqlite3','/.env','/clients/windows/Install.ps1'):
            self.assertEqual(self.request(path)[0],404)

    def test_unconfigured_telegram_is_not_reported_as_delivered(self):
        status,data,_=self.request('/api/telegram/test',token=self.control,method='POST',body='{}')
        self.assertEqual(status,409)
        self.assertFalse(self.hub.status()['telegram']['delivery_verified'])

    def test_weak_or_same_tokens_are_rejected(self):
        for view,control in [('short','another'),(self.view,self.view)]:
            with self.assertRaises(GateError):Hub(self.temporary.name,view_token=view,control_token=control)

    def test_non_ascii_authentication_is_refused_without_server_error(self):
        self.assertFalse(self.hub.authorized('Bearer '+('é'*40)))
        self.assertEqual(self.request('/api/status',token='é'*40)[0],401)

    def test_malformed_stored_report_fails_closed(self):
        (Path(self.temporary.name)/'latest_evaluation.json').write_text('{broken')
        status,data,_=self.request('/api/evaluation',token=self.view)
        self.assertEqual(status,200);self.assertIsNone(json.loads(data)['signal'])


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.journal=Journal(self.temporary.name)
        self.now=1_800_000_000;self.sent=[];self.stops=[]
        def transport(method,payload):
            if method=='getMe':return {'ok':True,'result':{'is_bot':True,'username':'SinaOCO_test_bot'}}
            if method=='getUpdates':return {'ok':True,'result':[]}
            self.sent.append(payload)
            return {'ok':True,'result':{'message_id':len(self.sent),'chat':{'id':payload['chat_id']}}}
        self.api=TelegramAPI('123456789:'+('x'*35),transport=transport)
        self.bot=OwnerBot(self.journal,self.api,lambda:{'state':'PAUSED'},lambda:self.stops.append(True),clock=lambda:self.now)

    def tearDown(self):self.temporary.cleanup()

    def update(self,text,identity=1001,update_id=1,**extra):
        message={'text':text,'chat':{'id':identity,'type':'private'},'from':{'id':identity,'is_bot':False},'date':self.now}
        message.update(extra)
        return {'update_id':update_id,'message':message}

    def pair(self):
        link=self.bot.pairing_link()
        code=parse_qs(urlsplit(link['url']).query)['start'][0]
        self.assertEqual(self.bot.process(self.update('/start '+code)),'PAIRED')
        return code

    def test_wrong_or_expired_pairing_does_not_send_to_unknown_recipient(self):
        link=self.bot.pairing_link()
        self.assertEqual(self.bot.process(self.update('/start wrong')),'IGNORED')
        code=parse_qs(urlsplit(link['url']).query)['start'][0]
        self.now+=301
        self.assertEqual(self.bot.process(self.update('/start '+code,update_id=2)),'IGNORED')
        self.assertEqual(self.sent,[]);self.assertFalse(self.bot.state()['owner_paired'])

    def test_pairing_requires_private_human_matching_sender_and_chat(self):
        link=self.bot.pairing_link();code=parse_qs(urlsplit(link['url']).query)['start'][0]
        self.bot.process(self.update('/start '+code,chat={'id':1001,'type':'group'}))
        self.assertEqual(self.sent,[])
        self.bot.process(self.update('/start '+code,update_id=2,**{'from':{'id':2002,'is_bot':False}}))
        self.assertEqual(self.sent,[])

    def test_verified_owner_and_real_receipt_are_persisted(self):
        self.pair()
        self.assertTrue(self.bot.state()['delivery_verified'])
        self.assertEqual(self.sent[0]['chat_id'],1001)
        self.assertEqual(self.bot.test_delivery()['status'],'NON_TRADING_TEST_DELIVERED')
        other=OwnerBot(self.journal,self.api,lambda:{'state':'PAUSED'},lambda:None,clock=lambda:self.now)
        self.assertTrue(other.state()['owner_paired'])

    def test_unpaired_test_delivery_does_not_send(self):
        with self.assertRaises(GateError):self.bot.test_delivery()
        self.assertEqual(self.sent,[])

    def test_only_owner_can_stop_and_duplicate_stop_is_not_replayed(self):
        self.pair()
        self.assertEqual(self.bot.process(self.update('/stop',identity=2002,update_id=2)),'IGNORED')
        self.assertEqual(self.bot.process(self.update('/stop',update_id=3)),'STOP')
        self.assertEqual(self.bot.process(self.update('/stop',update_id=3)),'DUPLICATE')
        self.assertEqual(len(self.stops),1)
        self.assertTrue(all(message['chat_id']==1001 for message in self.sent))

    def test_forwarded_or_old_text_never_stops_service(self):
        self.pair()
        self.bot.process(self.update('/stop',update_id=2,forward_origin={'type':'user'}))
        self.bot.process(self.update('/stop',update_id=3,date=self.now-121))
        self.assertEqual(self.stops,[])

    def test_live_start_is_blocked_even_for_owner(self):
        self.pair()
        self.assertEqual(self.bot.process(self.update(START,update_id=2)),'LIVE_BLOCKED')
        self.assertFalse(self.bot.state()['live_signal_publication'])
        self.assertEqual(self.stops,[])

    def test_wrong_chat_receipt_does_not_verify_delivery(self):
        bad=TelegramAPI('123456789:'+('x'*35),transport=lambda *_:{'ok':True,'result':{'message_id':1,'chat':{'id':999}}})
        self.pair()
        bot=OwnerBot(self.journal,bad,lambda:{'state':'PAUSED'},lambda:None,clock=lambda:self.now)
        with self.assertRaises(GateError):bot.test_delivery()

    def test_token_is_not_echoed_by_api_error(self):
        marker='123456789:'+('s'*35)
        api=TelegramAPI(marker,transport=lambda *_:{'ok':False,'description':marker})
        with self.assertRaises(GateError) as error:api.call('getMe',{})
        self.assertNotIn(marker,str(error.exception))
        with self.assertRaises(GateError):api.call('deleteWebhook',{})

    def test_changing_bot_credential_requires_owner_pairing_again(self):
        self.pair()
        changed=TelegramAPI('999999999:'+('z'*35),transport=lambda *_:{'ok':True,'result':[]})
        replacement=OwnerBot(self.journal,changed,lambda:{'state':'PAUSED'},lambda:None,clock=lambda:self.now)
        self.assertFalse(replacement.state()['owner_paired'])
        self.assertFalse(replacement.state()['delivery_verified'])


class ClientArtifactTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is optional for Python-only server deployment')
    def test_client_model_blocks_non_https_secret_urls_and_malformed_prices(self):
        script="""
const assert=require('assert');const model=require('./web/app.js');
assert.equal(model.serverURL('https://example.com/'),'https://example.com/');
for(const url of ['http://example.com','https://user:secret@example.com','https://example.com/?token=x','https://example.com/#key'])assert.throws(()=>model.serverURL(url));
assert.throws(()=>model.safeEvaluation({signal:{direction:'long',entry_reference:'100',sl:'110',tp:'120'}}));
assert.equal(model.safeEvaluation({live:true,signal:null}).live,false);
assert.throws(()=>model.safeEvaluation({signal:{direction:'long',entry_reference:'<script>',sl:'90',tp:'120'}}));
const report={provider:'BingX',product:'standard_futures',symbol:'BTCUSDT',price_kind:'index_price',data_mode:'synthetic',signal:{direction:'long',entry_reference:'100',sl:'90',tp:'120'}};
assert.equal(model.safeEvaluation(report).signal.entry_reference,'100');
for(const patch of [{symbol:'WLDUSDT'},{product:'perpetual_futures'},{price_kind:'last_price'},{data_mode:'live'}])assert.throws(()=>model.safeEvaluation({...report,...patch}));
assert.throws(()=>model.safeEvaluation({...report,signal:{...report.signal,sl:'0'}}));
"""
        subprocess.run(['node','-e',script],cwd=ROOT,check=True,capture_output=True,timeout=5)

    def test_android_permissions_and_ios_transport_security(self):
        import xml.etree.ElementTree as ET
        import plistlib
        manifest=ET.parse(ROOT/'clients/android/app/src/main/AndroidManifest.xml')
        ns='{http://schemas.android.com/apk/res/android}'
        self.assertEqual([item.attrib[ns+'name'] for item in manifest.findall('uses-permission')],['android.permission.INTERNET'])
        self.assertEqual(manifest.find('application').attrib[ns+'usesCleartextTraffic'],'false')
        info=plistlib.loads((ROOT/'clients/ios/SinaOCO/Info.plist').read_bytes())
        self.assertFalse(info['NSAppTransportSecurity']['NSAllowsArbitraryLoads'])

    def test_windows_payload_contains_only_flat_owned_viewer_files(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('windows_builder',ROOT/'scripts/build_windows_installer.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with ZipFile(io.BytesIO(module.payload_zip())) as archive:
            for name in archive.namelist():
                self.assertNotIn('/',name);self.assertNotIn('\\',name)
            self.assertIn('index.html',archive.namelist());self.assertIn('Uninstall.ps1',archive.namelist())
            self.assertFalse(any(name.endswith(('.py','.sqlite3','.env')) for name in archive.namelist()))

    def test_service_worker_does_not_cache_authenticated_api(self):
        worker=(ROOT/'web/sw.js').read_text()
        self.assertIn("url.pathname.startsWith('/api/')",worker)
        self.assertIn("event.request.headers.has('Authorization')",worker)

    def test_local_configuration_creates_private_keys_without_printing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            run=subprocess.run([sys.executable,'scripts/run_local_hub.py','--configure-only','--data-dir',directory],cwd=ROOT,check=True,capture_output=True,text=True,timeout=5)
            path=Path(directory)/'private-access-keys.json'
            keys=json.loads(path.read_text())
            self.assertNotEqual(keys['view_token'],keys['control_token'])
            for value in keys.values():
                self.assertGreaterEqual(len(value),32);self.assertNotIn(value,run.stdout)
            if sys.platform!='win32':self.assertEqual(path.stat().st_mode & 0o777,0o600)


if __name__=='__main__':unittest.main()

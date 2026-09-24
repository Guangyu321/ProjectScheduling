"""Tests exercise the real single-writer service and real HTTP handlers."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
try:
    from lab_orchestrator.service import PanelService
    from lab_orchestrator.server import create_server
except ImportError:
    PanelService=create_server=None
ROOT=Path(__file__).resolve().parents[1]

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(PanelService,'Local control service must be implemented')
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT);self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)

    def service(self):
        service=PanelService(self.path,auto_interval=None)
        self.addCleanup(service.close)
        return service

    def test_service_can_pause_resume_cancel_and_create_next_batch(self):
        service=self.service()
        first=service.call('create',key='first',scenario='normal',actor='operator')
        first_id=first['run_id']
        state=service.call('tick')
        state=service.call('pause',actor='operator',expected_version=state['version'])
        for _ in range(3):state=service.call('tick')
        self.assertEqual(state['state'],'paused')
        state=service.call('resume',actor='operator',expected_version=state['version'])
        state=service.call('cancel',actor='operator',reason='Next batch',expected_version=state['version'])
        self.assertEqual(state['state'],'cancelled')
        second=service.call('create',key='second',scenario='normal',actor='operator')
        self.assertNotEqual(second['run_id'],first_id)
        self.assertEqual(len(service.call('history')),2)

    def test_service_restart_holds_batch_paused_and_preserves_identity(self):
        service=self.service()
        first=service.call('create',key='restart',scenario='normal',actor='operator')
        service.call('tick');service.close()
        resumed=self.service()
        state=resumed.call('state')
        self.assertEqual(state['run_id'],first['run_id'])
        self.assertEqual(state['control_intent'],'pause')
        for _ in range(3):state=resumed.call('tick')
        self.assertEqual(state['state'],'paused')
        self.assertEqual(len(state['commands']),1)

    def test_service_refuses_replacement_of_an_unresolved_batch(self):
        service=self.service()
        service.call('create',key='unknown',scenario='unknown',actor='operator')
        state=service.call('tick')
        self.assertEqual(state['state'],'blocked')
        with self.assertRaises(ValueError):
            service.call('create',key='replacement',scenario='normal',actor='operator')
        self.assertEqual(service.call('state')['run_id'],state['run_id'])

    def test_real_http_control_and_stale_version_rejection(self):
        service=self.service()
        server=create_server(service,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def shutdown():server.shutdown();server.server_close();thread.join(5)
        self.addCleanup(shutdown)
        root=f'http://127.0.0.1:{server.server_address[1]}'
        token=json.load(urlopen(root+'/api/v1/bootstrap'))['control_token']
        def post(path,body,with_token=True):
            headers={'Content-Type':'application/json'}
            if with_token:headers['X-Control-Token']=token
            return json.load(urlopen(Request(root+path,data=json.dumps(body).encode(),headers=headers)))
        with self.assertRaises(HTTPError) as denied:
            post('/api/v1/runs',{'key':'web','scenario':'normal','actor':'operator'},False)
        self.assertEqual(denied.exception.code,403)
        state=post('/api/v1/runs',{'key':'web','scenario':'normal','actor':'operator'})
        url='/api/v1/runs/'+state['run_id']+'/pause'
        paused=post(url,{'actor':'operator','expected_version':state['version']})
        self.assertEqual(paused['state'],'paused')
        with self.assertRaises(HTTPError) as conflict:
            post(url,{'actor':'operator','expected_version':state['version']})
        self.assertEqual(conflict.exception.code,409)
        with self.assertRaises(HTTPError) as wrong:
            post('/api/v1/runs/not-this-batch/cancel',{'actor':'operator','reason':'test','expected_version':paused['version']})
        self.assertEqual(wrong.exception.code,404)
        with self.assertRaises(HTTPError) as missing_page:
            urlopen(root+'/')
        self.assertEqual(missing_page.exception.code,404)


    def test_http_cannot_pause_replacement_batch_when_requests_interleave(self):
        service=self.service()
        first=service.call('create',key='old',scenario='normal',actor='operator')
        original_call=service.call
        switched=[]
        def interleaved(action,**payload):
            if action=='pause' and not switched:
                original_call('cancel',actor='operator',reason='Replace old batch',expected_version=first['version'])
                switched.append(original_call('create',key='new',scenario='normal',actor='operator'))
            return original_call(action,**payload)
        service.call=interleaved
        server=create_server(service,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def shutdown():server.shutdown();server.server_close();thread.join(5)
        self.addCleanup(shutdown)
        root=f'http://127.0.0.1:{server.server_address[1]}'
        token=json.load(urlopen(root+'/api/v1/bootstrap'))['control_token']
        request=Request(root+'/api/v1/runs/'+first['run_id']+'/pause',
            data=json.dumps({'actor':'operator','expected_version':first['version']}).encode(),
            headers={'Content-Type':'application/json','X-Control-Token':token})
        with self.assertRaises(HTTPError) as rejected:
            urlopen(request)
        self.assertEqual(rejected.exception.code,404)
        self.assertEqual(original_call('state')['run_id'],switched[0]['run_id'])
        self.assertEqual(original_call('state')['state'],'running')

class SeparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.service=PanelService(self.tmp.name,auto_interval=None)
        self.addCleanup(self.service.close)
        self.server=create_server(self.service,port=0)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        def shutdown():self.server.shutdown();self.server.server_close();thread.join(5)
        self.addCleanup(shutdown)
        self.root=f'http://127.0.0.1:{self.server.server_address[1]}'
        self.origin='http://127.0.0.1:5173'

    def test_separate_frontend_can_preflight_and_control_with_token(self):
        request=Request(self.root+'/api/v1/runs',method='OPTIONS',headers={
            'Origin':self.origin,'Access-Control-Request-Method':'POST',
            'Access-Control-Request-Headers':'content-type,x-control-token'})
        with urlopen(request) as response:
            self.assertEqual(response.status,204)
            self.assertEqual(response.headers['Access-Control-Allow-Origin'],self.origin)
            self.assertIn('x-control-token',response.headers['Access-Control-Allow-Headers'].lower())
        with urlopen(Request(self.root+'/api/v1/bootstrap',headers={'Origin':self.origin})) as response:
            self.assertEqual(response.headers['Access-Control-Allow-Origin'],self.origin)
            token=json.load(response)['control_token']
        headers={'Origin':self.origin,'Content-Type':'application/json','X-Control-Token':token}
        data=json.dumps({'actor':'operator','key':'split','scenario':'normal'}).encode()
        with urlopen(Request(self.root+'/api/v1/runs',data=data,headers=headers)) as response:
            state=json.load(response)
            self.assertEqual(state['state'],'running')
        stale=json.dumps({'actor':'operator','expected_version':-1}).encode()
        with self.assertRaises(HTTPError) as conflict:
            urlopen(Request(self.root+'/api/v1/runs/'+state['run_id']+'/pause',data=stale,headers=headers))
        self.assertEqual(conflict.exception.code,409)
        self.assertEqual(conflict.exception.headers['Access-Control-Allow-Origin'],self.origin)
        del headers['X-Control-Token']
        with self.assertRaises(HTTPError) as no_token:
            urlopen(Request(self.root+'/api/v1/runs',data=data,headers=headers))
        self.assertEqual(no_token.exception.code,403)

    def test_untrusted_origin_cannot_read_token_or_preflight(self):
        for method,path in [('GET','/api/v1/bootstrap'),('OPTIONS','/api/v1/runs')]:
            with self.assertRaises(HTTPError) as denied:
                urlopen(Request(self.root+path,method=method,headers={'Origin':'https://untrusted.example',
                    'Access-Control-Request-Method':'POST'}))
            self.assertEqual(denied.exception.code,403)
            self.assertIsNone(denied.exception.headers.get('Access-Control-Allow-Origin'))
        self.assertEqual(self.service.call('state')['state'],'empty')

    def test_custom_frontend_origin_replaces_the_default_allowlist(self):
        origin='http://localhost:8181'
        server=create_server(self.service,port=0,frontend_origins=[origin])
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            root=f'http://127.0.0.1:{server.server_address[1]}'
            with urlopen(Request(root+'/api/v1/bootstrap',headers={'Origin':origin})) as response:
                self.assertEqual(response.headers['Access-Control-Allow-Origin'],origin)
            with self.assertRaises(HTTPError) as denied:
                urlopen(Request(root+'/api/v1/bootstrap',headers={'Origin':self.origin}))
            self.assertEqual(denied.exception.code,403)
        finally:server.shutdown();server.server_close();thread.join(5)
        with self.assertRaises(ValueError):
            create_server(self.service,port=0,frontend_origins=['http://localhost:8181/path'])

    def test_api_server_does_not_serve_frontend_files(self):
        for path in ('/','/index.html','/app.js','/style.css','/config.js'):
            with self.assertRaises(HTTPError) as missing:
                urlopen(self.root+path)
            self.assertEqual(missing.exception.code,404)

if __name__=='__main__':
    unittest.main()

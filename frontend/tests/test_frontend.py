"""Verify standalone frontend hosting and runtime API configuration."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]

class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT/'serve.py').exists(),'Standalone frontend server must exist')
        spec=importlib.util.spec_from_file_location('frontend_server',ROOT/'serve.py')
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.api='http://127.0.0.1:9876'
        self.server=self.module.create_server(port=0,api_base=self.api)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        def shutdown():self.server.shutdown();self.server.server_close();thread.join(5)
        self.addCleanup(shutdown)
        self.base=f'http://127.0.0.1:{self.server.server_address[1]}'

    def test_static_page_and_runtime_configuration_work_without_backend(self):
        with urlopen(self.base+'/') as response:
            html=response.read().decode()
            self.assertIn('text/html',response.headers['Content-Type'])
            self.assertIn(self.api,response.headers['Content-Security-Policy'])
            self.assertLess(html.index('/config.js'),html.index('/app.js'))
        with urlopen(self.base+'/config.js') as response:
            script=response.read().decode()
            config=json.loads(script.removeprefix('window.LAB_CONFIG = ').strip().removesuffix(';'))
            self.assertEqual(config['apiBase'],self.api)
        with urlopen(self.base+'/app.js') as response:
            self.assertIn('API_BASE+path',response.read().decode())

    def test_frontend_does_not_expose_api_or_server_files(self):
        for path in ('/api/v1/state','/serve.py','/../serve.py','/%2e%2e/serve.py','/tests/test_frontend.py'):
            with self.assertRaises(HTTPError) as missing:urlopen(self.base+path)
            self.assertEqual(missing.exception.code,404)
        with self.assertRaises(HTTPError) as wrong_host:
            urlopen(Request(self.base+'/',headers={'Host':'untrusted.example'}))
        self.assertEqual(wrong_host.exception.code,403)

    def test_duplicate_frontend_start_reports_occupied_port_without_traceback(self):
        result=subprocess.run([sys.executable,str(ROOT/'serve.py'),'--port',str(self.server.server_address[1])],
            capture_output=True,text=True,encoding='utf-8',timeout=15,
            env={**os.environ,'PYTHONIOENCODING':'utf-8'})
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertNotIn('Traceback',result.stderr)
        self.assertIn('端口',result.stderr)
        self.assertIn(str(self.server.server_address[1]),result.stderr)
        with urlopen(self.base+'/') as response:self.assertEqual(response.status,200)

    def test_invalid_api_configuration_is_rejected(self):
        for address in ('javascript:alert(1)','http://user:secret@localhost:8765','http://localhost:8765/api',
                        'http://localhost:8765/?secret=1','http://localhost:8765;bad'):
            with self.assertRaises(ValueError):self.module.create_server(port=0,api_base=address)

if __name__=='__main__':unittest.main()

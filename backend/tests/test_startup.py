"""Real CLI regressions for duplicate controllers and occupied ports."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from lab_orchestrator.service import PanelService

ROOT=Path(__file__).resolve().parents[1]

class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)

    def launch(self,*args):
        return subprocess.run([sys.executable,str(ROOT/'run.py'),'--data',str(self.path),*args],
            capture_output=True,text=True,encoding='utf-8',timeout=15,
            env={**os.environ,'PYTHONIOENCODING':'utf-8'})

    def test_duplicate_start_reports_busy_database_without_traceback_or_mutation(self):
        service=PanelService(self.path,auto_interval=None);self.addCleanup(service.close)
        before=service.call('create',key='preserve-me',actor='operator')
        result=self.launch('--port','0')
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertNotIn('Traceback',result.stderr)
        self.assertIn('另一个后台',result.stderr)
        self.assertIn(str(self.path),result.stderr)
        self.assertIn('Ctrl+C',result.stderr)
        self.assertEqual(service.call('state'),before)

    def test_port_conflict_reports_port_and_releases_database(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            port=listener.getsockname()[1]
            result=self.launch('--port',str(port))
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertNotIn('Traceback',result.stderr)
        self.assertIn(str(port),result.stderr)
        self.assertIn('端口',result.stderr)
        service=PanelService(self.path,auto_interval=None)
        try:self.assertEqual(service.call('state')['state'],'empty')
        finally:service.close()

if __name__=='__main__':unittest.main()

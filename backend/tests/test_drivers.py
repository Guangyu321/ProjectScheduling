"""Integration checks for device plugin routing."""
import json
from pathlib import Path
import tempfile
import unittest
from lab_orchestrator import Engine
try:
    from lab_orchestrator.drivers import DriverRegistry, DeviceRouter, default_registry
except ImportError:
    DriverRegistry=DeviceRouter=default_registry=None

ROOT=Path(__file__).resolve().parents[1]

class DriverTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(DeviceRouter,'Device plugin routing must be implemented')
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT);self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.cell=json.loads((ROOT/'examples/workcell.json').read_text(encoding='utf-8'))
        self.flow=json.loads((ROOT/'examples/workflow.json').read_text(encoding='utf-8'))

    def test_router_runs_multiple_devices_and_keeps_command_routes_after_reopen(self):
        router=DeviceRouter(self.cell,self.path/'devices',default_registry())
        self.addCleanup(router.close)
        engine=Engine(self.cell,self.path/'runtime.db',router);self.addCleanup(engine.close)
        engine.submit(self.flow,'routed');engine.tick(0)
        engine.close();router.close()
        router=DeviceRouter(self.cell,self.path/'devices',default_registry());self.addCleanup(router.close)
        engine=Engine(self.cell,self.path/'runtime.db',router);self.addCleanup(engine.close)
        engine.recover()
        result=engine.run_until_complete()
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(router.execution_count(),24)
        self.assertEqual(len(router.describe()),10)
        first=result['commands'][0]
        self.assertEqual(router.query(first['id'],result['sim_time'])['state'],'succeeded')
        self.assertEqual(router.query('missing',result['sim_time'])['state'],'unknown')

    def test_registry_rejects_duplicate_and_unknown_factories(self):
        registry=default_registry()
        with self.assertRaises(ValueError):
            registry.register('simulated',lambda config,path:None)
        with self.assertRaises(ValueError):
            registry.create('missing',{},self.path/'missing')

if __name__=='__main__':
    unittest.main()

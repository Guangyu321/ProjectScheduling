"""End-to-end behaviors for durable operator controls."""
import copy
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from lab_orchestrator import Engine, Simulator

ROOT=Path(__file__).resolve().parents[1]

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.cell=json.loads((ROOT/'examples/workcell.json').read_text(encoding='utf-8'))
        self.flow=json.loads((ROOT/'examples/workflow.json').read_text(encoding='utf-8'))

    def make(self,faults=None):
        sim=Simulator(self.path/'devices.db',faults=faults)
        self.addCleanup(sim.close)
        engine=Engine(self.cell,self.path/'runtime.db',sim)
        self.addCleanup(engine.close)
        return engine,sim

    def control(self,engine,name,*args,**kwargs):
        method=getattr(engine,name,None)
        self.assertIsNotNone(method,f'{name} control must be implemented')
        return method(*args,**kwargs)

    def test_pause_drains_then_survives_restart_without_new_dispatch(self):
        engine,sim=self.make()
        engine.submit(self.flow,'pause')
        engine.tick(0)
        self.control(engine,'pause',actor='operator')
        self.assertEqual(engine.snapshot()['state'],'pausing')
        engine.tick(2)
        self.assertEqual(engine.snapshot()['state'],'paused')
        self.assertEqual(sim.execution_count(),1)
        engine.close();sim.close()
        engine,sim=self.make()
        engine.recover();engine.tick(10)
        self.assertEqual(engine.snapshot()['state'],'paused')
        self.assertEqual(sim.execution_count(),1)
        self.control(engine,'resume',actor='operator')
        self.assertEqual(engine.run_until_complete()['state'],'succeeded')

    def test_cancel_drains_active_and_never_starts_pending_steps(self):
        engine,sim=self.make();engine.submit(self.flow,'cancel');engine.tick(0)
        self.control(engine,'cancel',actor='operator',reason='End simulation')
        self.assertEqual(engine.snapshot()['state'],'cancelling')
        engine.tick(2)
        result=engine.snapshot()
        self.assertEqual(result['state'],'cancelled')
        self.assertEqual(result['positions']['plate_01'],'scanner_1.slot1')
        self.assertEqual(result['locks'],[])
        self.assertEqual(sum(s['state']=='cancelled' for s in result['steps']),23)
        engine.tick(50)
        self.assertEqual(sim.execution_count(),1)
        with self.assertRaises(ValueError):
            engine.resume(actor='operator')

    def unknown(self):
        engine,sim=self.make({'plate_01.move_to_scan':'unknown'})
        engine.submit(self.flow,'unknown');engine.tick(0)
        return engine,sim,engine.snapshot()['commands'][0]['id']

    def evidence(self,location):
        return {'plate_id':'plate_01','location':location,'device_stopped':True,
                'observation':'Barcode and location independently checked in simulation'}

    def test_alarm_acknowledgement_does_not_resolve_or_release(self):
        engine,sim,command=self.unknown()
        before=engine.snapshot()
        self.control(engine,'acknowledge_alarm',command,actor='operator')
        engine.tick(1)
        after=engine.snapshot()
        self.assertEqual(after['state'],'blocked')
        self.assertEqual(after['locks'],before['locks'])
        self.assertEqual(after['alarms'][0]['state'],'acknowledged')
        self.assertEqual(after['recoveries'][0]['state'],'open')

    def test_manual_completed_requires_evidence_and_waits_for_resume(self):
        engine,sim,command=self.unknown()
        self.control(engine,'resolve',command,'completed',actor='operator',reason='Observed placement',
                     evidence=self.evidence('scanner_1.slot1'))
        result=engine.snapshot()
        self.assertEqual(result['state'],'paused')
        self.assertEqual(result['positions']['plate_01'],'scanner_1.slot1')
        self.assertEqual(result['locks'],[])
        self.assertEqual(result['recoveries'][0]['state'],'resolved')
        engine.tick(50)
        self.assertEqual(sim.execution_count(),1)
        engine.resume(actor='operator')
        self.assertEqual(engine.run_until_complete()['state'],'succeeded')
        self.assertEqual(sim.execution_count(),24)

    def test_not_started_retries_with_a_new_command_and_preserves_history(self):
        engine,sim,command=self.unknown()
        self.control(engine,'resolve',command,'not_started',actor='operator',reason='Confirmed not picked',
                     evidence=self.evidence('stack_1.slot1'))
        self.assertEqual(engine.snapshot()['state'],'paused')
        engine.resume(actor='operator')
        result=engine.run_until_complete()
        self.assertEqual(result['state'],'succeeded')
        attempts=[c for c in result['commands'] if c['step_id']=='plate_01.move_to_scan']
        self.assertEqual(len(attempts),2)
        self.assertEqual({c['state'] for c in attempts},{'not_started','succeeded'})
        self.assertEqual(len({c['id'] for c in attempts}),2)
        self.assertEqual({c['attempt'] for c in attempts},{1,2})

    def test_cancel_unknown_retains_locks_until_evidence_arrives(self):
        engine,sim,command=self.unknown()
        self.control(engine,'cancel',actor='operator',reason='End batch')
        result=engine.snapshot()
        self.assertEqual(result['state'],'blocked')
        self.assertEqual(result['control_intent'],'cancel')
        self.assertTrue(result['locks'])
        engine.close();sim.close()
        engine,sim=self.make()
        engine.recover()
        engine.resolve(command,'not_started',actor='operator',reason='Device stopped and source checked',
                       evidence=self.evidence('stack_1.slot1'))
        self.assertEqual(engine.snapshot()['state'],'cancelled')
        self.assertEqual(engine.snapshot()['locks'],[])
        self.assertEqual(sim.execution_count(),1)

    def test_invalid_or_stale_evidence_changes_nothing(self):
        engine,_,command=self.unknown()
        method=getattr(engine,'resolve',None)
        self.assertIsNotNone(method,'Manual recovery must exist')
        before=engine.snapshot()
        bads=[{},self.evidence('fridge_1.slot1'),dict(self.evidence('scanner_1.slot1'),device_stopped=False)]
        for evidence in bads:
            with self.assertRaises(ValueError):
                engine.resolve(command,'completed',actor='operator',reason='check',evidence=evidence)
            self.assertEqual(engine.snapshot(),before)
        with self.assertRaises(ValueError):
            engine.resolve(command,'completed',actor='operator',reason='check',evidence=self.evidence('scanner_1.slot1'),
                           expected_version=before['version']-1)
        self.assertEqual(engine.snapshot(),before)


    def test_execution_progress_does_not_stale_an_operator_control_version(self):
        engine,sim=self.make();engine.submit(self.flow,'control-version')
        version=engine.snapshot()['version']
        engine.tick(0);engine.tick(2)
        self.assertEqual(engine.snapshot()['version'],version)
        paused=engine.pause(actor='operator',expected_version=version)
        self.assertGreater(paused['version'],version)
        with self.assertRaises(ValueError):
            engine.cancel(actor='other operator',reason='Stale UI',expected_version=version)

    def test_v01_database_migration_preserves_uncertain_commands_and_locks(self):
        engine,sim,_=self.unknown()
        before=engine.snapshot()
        old_id=before['commands'][0]['id']
        engine.close();sim.close()
        # Recreate the actual v0.1 schema, which allowed only one command per step.
        with closing(sqlite3.connect(self.path/'runtime.db')) as db:
            db.executescript("""
                PRAGMA foreign_keys=OFF;
                DROP TABLE recovery_cases;
                DROP INDEX one_live_command_per_step;
                CREATE TABLE legacy_commands (
                    id TEXT PRIMARY KEY,step_id TEXT UNIQUE NOT NULL REFERENCES steps(id),
                    state TEXT NOT NULL,started REAL NOT NULL,ended REAL,
                    resources TEXT NOT NULL,result TEXT);
                INSERT INTO legacy_commands SELECT id,step_id,state,started,ended,resources,result FROM commands;
                DROP TABLE commands;
                ALTER TABLE legacy_commands RENAME TO commands;
            """)
        engine,sim=self.make({'plate_01.move_to_scan':'unknown'})
        engine.recover()
        migrated=engine.snapshot()
        self.assertEqual(migrated['locks'],before['locks'])
        self.assertEqual(migrated['positions'],before['positions'])
        self.assertEqual(migrated['certainty'],before['certainty'])
        self.assertEqual(migrated['events'][:len(before['events'])],before['events'])
        self.assertEqual(migrated['commands'][0]['id'],old_id)
        self.assertEqual(migrated['commands'][0]['attempt'],1)
        self.assertEqual(migrated['recoveries'][0]['state'],'open')
        self.assertEqual(engine.store.db.execute('PRAGMA foreign_key_check').fetchall(),[])
        engine.resolve(old_id,'not_started',actor='migration-test',reason='Observed still at source',
                       evidence={'device_stopped':True,'plate_id':'plate_01',
                                 'location':before['positions']['plate_01'],'observation':'Source plate verified'})
        engine.resume(actor='migration-test')
        result=engine.run_until_complete()
        self.assertEqual(result['state'],'succeeded')
        attempts=[c for c in result['commands'] if c['step_id']=='plate_01.move_to_scan']
        self.assertEqual({c['attempt'] for c in attempts},{1,2})
        self.assertEqual(next(c['state'] for c in attempts if c['id']==old_id),'not_started')


if __name__=='__main__':
    unittest.main()

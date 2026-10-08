"""Behavioral acceptance tests for the simulation kernel."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest

try:
    from lab_orchestrator import Engine, Simulator, validate
except ImportError:
    Engine = Simulator = validate = None

ROOT = Path(__file__).resolve().parents[1]

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Engine, 'The simulation kernel must be implemented')
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.cell = json.loads((ROOT/'examples/workcell.json').read_text(encoding='utf-8'))
        self.flow = json.loads((ROOT/'examples/workflow.json').read_text(encoding='utf-8'))

    def engine(self, faults=None, simulator_type=None):
        sim = (simulator_type or Simulator)(self.path/'device.db', faults=faults)
        self.addCleanup(sim.close)
        engine = Engine(self.cell, self.path/'run.db', sim)
        self.addCleanup(engine.close)
        return engine, sim

    def test_two_plates_finish_without_resource_overlap(self):
        engine, sim = self.engine()
        run_id = engine.submit(self.flow, 'batch-1')
        result = engine.run_until_complete()
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['run_id'], run_id)
        self.assertEqual(result['positions'], {'plate_01':'fridge_1.slot1','plate_02':'fridge_1.slot2'})
        self.assertEqual(len(result['commands']), 24)
        self.assertEqual(sim.execution_count(), 24)
        self.assertEqual(result['locks'], [])
        intervals = result['commands']
        overlap_seen = False
        for i, a in enumerate(intervals):
            for b in intervals[i+1:]:
                overlap = max(a['started'], b['started']) < min(a['ended'], b['ended'])
                if set(a['resources']) & set(b['resources']):
                    self.assertFalse(overlap, (a['step_id'], b['step_id']))
                else:
                    overlap_seen |= overlap
        self.assertTrue(overlap_seen, 'Different devices should process plates concurrently')
        self.assertTrue(all(s['state']=='succeeded' for s in result['steps']))

    def test_submit_is_idempotent_but_rejects_changed_payload(self):
        engine, _ = self.engine()
        first = engine.submit(self.flow, 'same')
        self.assertEqual(engine.submit(self.flow, 'same'), first)
        changed = copy.deepcopy(self.flow)
        changed['version'] = 2
        with self.assertRaises(ValueError):
            engine.submit(changed, 'same')

    def test_unknown_action_holds_resources_and_stops_dispatch(self):
        engine, sim = self.engine({'plate_01.move_to_scan':'unknown'})
        engine.submit(self.flow, 'unknown')
        result = engine.run_until_complete()
        self.assertEqual(result['state'], 'blocked')
        self.assertTrue(result['locks'])
        self.assertTrue(result['alarms'])
        self.assertEqual(result['certainty']['plate_01'], 'unknown')
        count = sim.execution_count()
        for now in (100,200,300):
            engine.tick(now)
        self.assertEqual(sim.execution_count(), count)
        self.assertNotEqual(result['positions']['plate_01'], 'scanner_1.slot1')

    def test_lost_ack_is_queried_without_repeating_action(self):
        engine, sim = self.engine({'plate_01.pipette':'lost_ack'})
        engine.submit(self.flow, 'lost-ack')
        result = engine.run_until_complete()
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(sim.execution_count(), 24)
        self.assertTrue(any(e['kind']=='reconciling' for e in result['events']))

    def test_restart_recovers_after_send_before_ack_saved(self):
        class CrashAfterSubmit(Simulator):
            def submit(self, command_id, step, now):
                super().submit(command_id, step, now)
                raise SystemExit('simulated process crash')
        engine, sim = self.engine(simulator_type=CrashAfterSubmit)
        engine.submit(self.flow, 'crash')
        with self.assertRaises(SystemExit):
            engine.tick(0)
        self.assertEqual(sim.execution_count(), 1)
        engine.close()
        sim.close()
        resumed, ledger = self.engine()
        resumed.recover()
        result = resumed.run_until_complete()
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(ledger.execution_count(), 24)

    def test_unknown_survives_reopening_and_does_not_release_locks(self):
        engine, sim = self.engine({'plate_01.move_to_scan':'unknown'})
        engine.submit(self.flow, 'persist')
        before = engine.run_until_complete()
        engine.close(); sim.close()
        reopened, ledger = self.engine()
        reopened.recover()
        after = reopened.snapshot()
        self.assertEqual(after['state'], 'blocked')
        self.assertEqual(after['locks'], before['locks'])
        self.assertEqual(ledger.execution_count(), 1)

    def test_second_controller_is_rejected(self):
        engine, sim = self.engine()
        with self.assertRaises(RuntimeError):
            other = Engine(self.cell, self.path/'run.db', sim)
            other.close()

    def test_changed_station_config_is_rejected_on_reopen(self):
        engine, sim = self.engine()
        engine.submit(self.flow, 'config')
        engine.close()
        changed = copy.deepcopy(self.cell)
        changed['devices'][0]['state'] = 'offline'
        with self.assertRaises(ValueError):
            other = Engine(changed, self.path/'run.db', sim)
            other.close()

    def test_configuration_rejects_invalid_inputs(self):
        cases=[]
        # A dependency on the other plate is now valid; this is a real same-plate cycle.
        flow=copy.deepcopy(self.flow); flow['steps'][0]['depends_on']=['plate_01.store']; cases.append((self.cell,flow))
        flow=copy.deepcopy(self.flow); flow['plates'][1]['initial_location']=flow['plates'][0]['initial_location']; cases.append((self.cell,flow))
        flow=copy.deepcopy(self.flow); flow['steps'][0]['device_id']='missing'; cases.append((self.cell,flow))
        flow=copy.deepcopy(self.flow); flow['steps'][0]['estimated_duration_s']=math.nan; cases.append((self.cell,flow))
        flow=copy.deepcopy(self.flow); flow['steps'][0]['estimated_duration_s']=0; cases.append((self.cell,flow))
        flow=copy.deepcopy(self.flow); flow['steps'][0]['source']='fridge_1.slot1'; cases.append((self.cell,flow))
        flow=copy.deepcopy(self.flow); flow['steps'][1]['capability']='plate.centrifuge'; cases.append((self.cell,flow))
        flow=copy.deepcopy(self.flow); flow['simulation_only']=False; cases.append((self.cell,flow))
        flow=copy.deepcopy(self.flow); flow['plates']=[]; cases.append((self.cell,flow))
        for cell, flow in cases:
            with self.subTest(flow=flow):
                with self.assertRaises(ValueError):
                    validate(cell, flow)

    def test_clock_cannot_move_backwards(self):
        engine, _ = self.engine()
        engine.submit(self.flow, 'clock')
        engine.tick(10)
        with self.assertRaises(ValueError):
            engine.tick(9)


    def test_demo_writes_a_completed_report(self):
        import lab_orchestrator
        demo = getattr(lab_orchestrator, 'run_demo', None)
        self.assertIsNotNone(demo, 'A callable demo must be provided')
        report = demo(self.path/'report')
        self.assertEqual(report['state'], 'succeeded')
        saved = json.loads((self.path/'report'/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['positions']['plate_02'], 'fridge_1.slot2')
        self.assertGreater((self.path/'report'/'timeline.html').stat().st_size, 1000)
        with self.assertRaises(FileExistsError):
            demo(self.path/'report')

    def test_simulator_rejects_command_identity_reuse_with_other_payload(self):
        _, sim = self.engine()
        step = self.flow['steps'][0]
        sim.submit('command-1', step, 0)
        sim.submit('command-1', step, 0)
        self.assertEqual(sim.execution_count(), 1)
        changed = copy.deepcopy(step)
        changed['target'] = 'fridge_1.slot1'
        with self.assertRaises(ValueError):
            sim.submit('command-1', changed, 0)

    def test_bad_completion_evidence_cannot_move_a_plate(self):
        class WrongPosition(Simulator):
            def query(self, command_id, now):
                result = super().query(command_id, now)
                if result['state'] == 'succeeded':
                    result['result']['confirmed_location'] = 'fridge_1.slot1'
                return result
        engine, sim = self.engine(simulator_type=WrongPosition)
        engine.submit(self.flow, 'bad-evidence')
        result = engine.run_until_complete()
        self.assertEqual(result['state'], 'blocked')
        self.assertEqual(result['positions']['plate_01'], 'stack_1.slot1')
        self.assertTrue(result['locks'])
        self.assertEqual(sim.execution_count(), 1)

    def test_missing_device_history_after_restart_blocks_instead_of_resending(self):
        engine, sim = self.engine()
        engine.submit(self.flow, 'missing-history')
        engine.tick(0)
        engine.close(); sim.close()
        replacement = Simulator(self.path/'new-device.db')
        self.addCleanup(replacement.close)
        resumed = Engine(self.cell, self.path/'run.db', replacement)
        self.addCleanup(resumed.close)
        resumed.recover()
        result = resumed.snapshot()
        self.assertEqual(result['state'], 'blocked')
        self.assertTrue(result['locks'])
        self.assertEqual(replacement.execution_count(), 0)


    def test_gripper_occupancy_must_be_cleared_before_another_pick(self):
        self.cell['transport']['reachable_locations'].append('robot_1.slot1')
        flow = copy.deepcopy(self.flow)
        flow['plates'][0]['final_location'] = 'scanner_1.slot1'
        flow['plates'][1]['initial_location'] = 'robot_1.slot1'
        first = copy.deepcopy(flow['steps'][0])
        first['id'] = 'a_move'
        second = copy.deepcopy(first)
        second.update(id='z_unload_gripper', plate_id='plate_02', source='robot_1.slot1', target='fridge_1.slot2')
        flow['steps'] = [first, second]
        engine, sim = self.engine()
        engine.submit(flow, 'gripper')
        engine.tick(0)
        states = {s['id']:s['state'] for s in engine.snapshot()['steps']}
        self.assertEqual(states['a_move'], 'pending')
        self.assertEqual(states['z_unload_gripper'], 'running')
        self.assertEqual(engine.run_until_complete()['state'], 'succeeded')
        self.assertEqual(sim.execution_count(), 2)

    def test_resource_ids_are_opaque_even_with_processing_suffix(self):
        self.cell['resources'].append({'id':'zone.processing','capacity':1})
        self.cell['transport']['shared_resources'].append('zone.processing')
        engine, _ = self.engine()
        engine.submit(self.flow, 'resource-name')
        try:
            result = engine.run_until_complete()
        except KeyError as exc:
            self.fail(f'An opaque resource id was interpreted as a device: {exc}')
        self.assertEqual(result['state'], 'succeeded')

    def test_time_at_tick_budget_exhaustion_survives_restart(self):
        engine, sim = self.engine()
        engine.submit(self.flow, 'tick-budget')
        with self.assertRaises(TimeoutError):
            engine.run_until_complete(max_ticks=1)
        before = engine.snapshot()['sim_time']
        engine.close(); sim.close()
        resumed, _ = self.engine()
        self.assertEqual(resumed.snapshot()['sim_time'], before)
        self.assertEqual(resumed.run_until_complete()['state'], 'succeeded')

if __name__ == '__main__':
    unittest.main()

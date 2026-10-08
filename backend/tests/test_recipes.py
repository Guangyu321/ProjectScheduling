"""Executable recipes, dependency safety, persistence and recovery contracts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from lab_orchestrator import Engine, Simulator
from lab_orchestrator.validation import validate
from lab_orchestrator.service import PanelService
from lab_orchestrator.controls import ConflictError
try:
    from lab_orchestrator.recipes import compile_recipe, preview_recipe, operation_catalog
except ImportError:
    compile_recipe = preview_recipe = operation_catalog = None

ROOT = Path(__file__).resolve().parents[1]


def recipe():
    return {
        'id': 'repeat_measure', 'name': '重复检测实验', 'version': 1,
        'execution_mode': 'serial',
        'plates': [
            {'id': 'sample_a', 'barcode': 'A001', 'type': 'SIM_96',
             'initial_location': 'stack_1.slot1', 'final_location': 'fridge_1.slot1'},
            {'id': 'sample_b', 'barcode': 'B001', 'type': 'SIM_96',
             'initial_location': 'stack_1.slot2', 'final_location': 'fridge_1.slot2'},
        ],
        'operations': [
            {'id': 'read_before', 'capability': 'fluorescence.measure', 'method_id': 'READ_A',
             'estimated_duration_s': 2, 'timeout_s': 10},
            {'id': 'mix', 'capability': 'plate.shake', 'method_id': 'MIX_A',
             'estimated_duration_s': 3, 'timeout_s': 10},
            {'id': 'read_after', 'capability': 'fluorescence.measure', 'method_id': 'READ_B',
             'estimated_duration_s': 2, 'timeout_s': 10},
        ],
    }


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.cell = json.loads((ROOT/'examples/workcell.json').read_text(encoding='utf-8'))
        self.assertIsNotNone(compile_recipe, 'A configurable recipe compiler is required')

    def test_reordered_repeated_operations_generate_valid_transports_and_finish(self):
        flow = compile_recipe(self.cell, recipe())
        operations = [s for s in flow['steps'] if s['kind'] == 'process']
        self.assertEqual([s['capability'] for s in operations[:3]],
                         ['fluorescence.measure', 'plate.shake', 'fluorescence.measure'])
        self.assertEqual(len(flow['steps']), 14)
        self.assertEqual(flow['steps'][0]['source'], 'stack_1.slot1')
        self.assertEqual(flow['steps'][0]['target'], 'reader_1.slot1')
        self.assertIn('sample_a.move_to_finish', flow['steps'][7]['depends_on'])
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            simulator = Simulator(Path(directory)/'device.db')
            engine = Engine(self.cell, Path(directory)/'run.db', simulator)
            try:
                engine.submit(flow, 'free-composition')
                result = engine.run_until_complete()
                self.assertEqual(result['state'], 'succeeded')
                self.assertEqual(result['positions'], {'sample_a': 'fridge_1.slot1', 'sample_b': 'fridge_1.slot2'})
                self.assertEqual(simulator.execution_count(), 14)
            finally:
                engine.close(); simulator.close()

    def test_device_binding_uses_capability_and_accepts_a_compatible_replacement(self):
        other = copy.deepcopy(next(d for d in self.cell['devices'] if d['id'] == 'reader_1'))
        other['id'] = 'reader_2'
        self.cell['devices'].append(other)
        self.cell['locations'].append({'id': 'reader_2.slot1', 'device_id': 'reader_2',
                                       'capacity': 1, 'plate_types': ['SIM_96']})
        self.cell['transport']['reachable_locations'].append('reader_2.slot1')
        self.cell['resources'] += [{'id': 'reader_2.processing', 'capacity': 1}, {'id': 'reader_2.port', 'capacity': 1}]
        data = recipe(); data['operations'][0]['device_id'] = 'reader_2'
        flow = compile_recipe(self.cell, data)
        self.assertEqual(flow['steps'][1]['device_id'], 'reader_2')
        self.assertEqual(flow['steps'][0]['target'], 'reader_2.slot1')
        self.cell['devices'] = [d for d in self.cell['devices'] if d['id'] != 'reader_1']
        self.cell['locations'] = [p for p in self.cell['locations'] if p['device_id'] != 'reader_1']
        self.cell['transport']['reachable_locations'].remove('reader_1.slot1')
        data = recipe()
        self.assertEqual(compile_recipe(self.cell, data)['steps'][1]['device_id'], 'reader_2')

    def test_adjacent_operations_at_same_station_need_no_extra_transport(self):
        data = recipe(); data['operations'].pop(1)
        flow = compile_recipe(self.cell, data)
        first = [s for s in flow['steps'] if s['plate_id'] == 'sample_a']
        self.assertEqual([s['kind'] for s in first], ['transport', 'process', 'process', 'transport'])
        self.assertEqual(first[2]['parameters']['method_id'], 'READ_B')

    def test_preview_rejects_occupancy_deadlock_but_serial_route_succeeds(self):
        data = recipe()
        report = preview_recipe(self.cell, data)
        self.assertEqual(report['summary']['steps'], 14)
        self.assertEqual(len(report['timeline']), 14)
        self.assertGreater(report['summary']['duration_s'], 0)
        data['execution_mode'] = 'parallel'
        with self.assertRaisesRegex(ValueError, '阻塞'):
            preview_recipe(self.cell, data)

    def test_invalid_recipe_is_rejected_before_execution(self):
        cases = []
        data = recipe(); data['operations'] = []; cases.append(data)
        data = recipe(); data['operations'][0]['capability'] = 'not.registered'; cases.append(data)
        data = recipe(); data['operations'][0]['device_id'] = 'shaker_1'; cases.append(data)
        data = recipe(); data['operations'][0]['estimated_duration_s'] = float('nan'); cases.append(data)
        data = recipe(); data['operations'][0]['timeout_s'] = 1; cases.append(data)
        data = recipe(); data['operations'][0]['method_id'] = ''; cases.append(data)
        data = recipe(); data['operations'][1]['id'] = data['operations'][0]['id']; cases.append(data)
        data = recipe(); data['plates'][1]['final_location'] = data['plates'][0]['final_location']; cases.append(data)
        data = recipe(); data['execution_mode'] = 'arbitrary'; cases.append(data)
        data = recipe(); data['operations'][0]['parameters'] = {'unexpected': 4}; cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                compile_recipe(self.cell, data)

    def test_catalog_includes_every_registered_process_capability(self):
        catalog = operation_catalog(self.cell)
        capabilities = {op['capability'] for op in catalog}
        self.assertTrue({'plate.peel', 'plate.centrifuge', 'stack.present', 'storage.retrieve'} <= capabilities)
        self.assertNotIn('plate.transport', capabilities)

    def test_preview_uses_same_one_second_dispatch_cadence_as_console(self):
        data = recipe(); data['plates'] = data['plates'][:1]; data['operations'] = data['operations'][:1]
        data['operations'][0]['estimated_duration_s'] = 0.5
        report = preview_recipe(self.cell, data)
        # Move 0–2, measure 2–2.5, next dispatch at tick 3, final move 3–5.
        self.assertEqual(report['summary']['duration_s'], 5)
        self.assertEqual(report['timeline'][-1]['started'], 3)


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.cell = json.loads((ROOT/'examples/workcell.json').read_text(encoding='utf-8'))
        self.flow = json.loads((ROOT/'examples/workflow.json').read_text(encoding='utf-8'))

    def test_definition_order_is_irrelevant_and_cross_plate_dependencies_work(self):
        self.flow['steps'][12]['depends_on'] = ['plate_01.store']
        self.flow['steps'].reverse()
        validate(self.cell, self.flow)

    def test_cycle_missing_dependency_and_unordered_same_plate_are_rejected(self):
        for change in ('cycle', 'missing', 'unordered'):
            data = copy.deepcopy(self.flow)
            if change == 'cycle': data['steps'][0]['depends_on'] = ['plate_01.store']
            elif change == 'missing': data['steps'][0]['depends_on'] = ['missing']
            else: data['steps'][3]['depends_on'] = ['plate_01.move_to_scan']
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(self.cell, data)


class RecipeServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.service = PanelService(self.tmp.name, auto_interval=None)
        self.addCleanup(lambda: self.service.close())

    def test_template_revisions_persist_and_running_snapshot_is_immutable(self):
        data = recipe()
        saved = self.service.call('save_recipe', recipe=data, actor='operator', expected_version=0)
        self.assertEqual(saved['recipe']['version'], 1)
        state = self.service.call('create', key='custom', recipe=saved['recipe'], actor='operator')
        data['operations'].pop(1)
        updated = self.service.call('save_recipe', recipe=data, actor='operator', expected_version=1)
        self.assertEqual(updated['recipe']['version'], 2)
        with self.assertRaises(ConflictError):
            self.service.call('save_recipe', recipe=data, actor='operator', expected_version=1)
        self.assertEqual(self.service.call('state')['workflow']['recipe']['version'], 1)
        self.assertEqual(len(self.service.call('state')['workflow']['recipe']['operations']), 3)
        self.service.close()
        self.service = PanelService(self.tmp.name, auto_interval=None)
        stored = [r for r in self.service.call('recipes') if r['recipe']['id'] == 'repeat_measure']
        self.assertEqual([r['recipe']['version'] for r in stored], [1, 2])
        self.assertEqual(self.service.call('state')['run_id'], state['run_id'])
        self.assertEqual(self.service.call('state')['workflow']['recipe']['name'], '重复检测实验')

    def test_preview_does_not_change_current_batch_or_history(self):
        before = self.service.call('state')
        report = self.service.call('preview_recipe', recipe=recipe())
        self.assertEqual(report['summary']['steps'], 14)
        self.assertEqual(self.service.call('state'), before)
        self.assertEqual(self.service.call('history'), [])

    def test_custom_fault_target_survives_restart_and_can_continue(self):
        data = recipe(); data['plates'] = data['plates'][:1]
        state = self.service.call('create', key='unknown-custom', recipe=data, actor='operator', scenario='unknown')
        repeated = self.service.call('create', key='unknown-custom', recipe=data, actor='operator', scenario='unknown')
        self.assertEqual(repeated['run_id'], state['run_id'])
        state = self.service.call('tick')
        self.assertEqual(state['state'], 'blocked')
        self.assertEqual(state['commands'][0]['step_id'], 'sample_a.move_to_read_before')
        command_id = state['commands'][0]['id']
        self.service.close()
        self.service = PanelService(self.tmp.name, auto_interval=None)
        state = self.service.call('state')
        self.assertEqual(state['state'], 'blocked')
        self.service.call('resolve', command_id=command_id, decision='not_started', actor='operator',
                          reason='核实尚未取板', evidence={'plate_id': 'sample_a', 'location': 'stack_1.slot1',
                          'device_stopped': True, 'observation': '板仍在堆栈'})
        self.service.call('resume', actor='operator')
        for _ in range(40):
            state = self.service.call('tick')
            if state['state'] == 'succeeded': break
        self.assertEqual(state['state'], 'succeeded')
        self.assertEqual(len(state['commands']), 8)
        self.assertEqual(state['positions']['sample_a'], 'fridge_1.slot1')

    def test_lost_ack_uses_actual_process_without_duplicate_execution(self):
        data = recipe(); data['plates'] = data['plates'][:1]
        self.service.call('create', key='ack-custom', recipe=data, actor='operator', scenario='lost_ack')
        for _ in range(40):
            state = self.service.call('tick')
            if state['state'] == 'succeeded': break
        self.assertEqual(state['state'], 'succeeded')
        reconciled = [e for e in state['events'] if e['kind'] == 'reconciling']
        self.assertEqual(reconciled[0]['subject'], 'sample_a.read_before')
        self.assertEqual(len(state['commands']), 7)

    def test_malformed_or_ambiguous_submissions_do_not_create_batches(self):
        for payload in ({'recipe': None}, {'recipe': recipe(), 'workflow': {}},
                        {'recipe': {'operations': []}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.call('create', key='invalid', actor='operator', **payload)
        self.assertEqual(self.service.call('history'), [])

    def test_http_template_preview_save_and_create(self):
        import threading
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from lab_orchestrator.server import create_server
        server = create_server(self.service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            root = f'http://127.0.0.1:{server.server_address[1]}'
            with urlopen(root+'/api/v1/bootstrap') as response: bootstrap = json.load(response)
            headers = {'Content-Type': 'application/json', 'X-Control-Token': bootstrap['control_token']}
            def post(path, body):
                with urlopen(Request(root+path, data=json.dumps(body).encode(), headers=headers)) as response:
                    return json.load(response)
            report = post('/api/v1/recipes/preview', {'recipe': recipe()})
            self.assertEqual(report['summary']['steps'], 14)
            saved = post('/api/v1/recipes', {'recipe': recipe(), 'actor': 'operator', 'expected_version': 0})
            self.assertEqual(saved['recipe']['name'], '重复检测实验')
            with urlopen(root+'/api/v1/recipes') as response:
                self.assertEqual(len(json.load(response)), 2)
            with self.assertRaises(HTTPError) as conflict:
                post('/api/v1/recipes', {'recipe': recipe(), 'actor': 'operator', 'expected_version': 0})
            self.assertEqual(conflict.exception.code, 409)
            conflict.exception.close()
            state = post('/api/v1/runs', {'recipe': recipe(), 'key': 'http-custom', 'actor': 'operator'})
            self.assertEqual(len(state['workflow']['steps']), 14)
            with self.assertRaises(HTTPError) as invalid:
                post('/api/v1/recipes/preview', {'recipe': [], 'unexpected': True})
            self.assertEqual(invalid.exception.code, 422)
            invalid.exception.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(5)


if __name__ == '__main__':
    unittest.main()
